from __future__ import annotations

import logging
from statistics import median
from typing import Any

from deal_finder.deduplication import match_listing
from deal_finder.extraction import ListingExtractionResult, extract_listing_metadata
from deal_finder.models import NormalizedListing
from deal_finder.ranking import rank_listing
from deal_finder.sources.base import SourceRecordResult
from deal_finder.sources.manual_import import ManualImportAdapter
from deal_finder.sources.registry import SourceAdapterRegistry, build_default_source_registry
from models.property import Property
from services.database import DatabaseService
from services.listing_history import ListingHistoryEngine
from services.property_enrichment import PropertyEnrichmentEngine

LOGGER = logging.getLogger(__name__)

DEFAULT_SOURCES = [
    {"name": "Funda", "source_type": "portal", "base_url": "https://www.funda.nl", "is_enabled": False},
    {"name": "Funda in Business", "source_type": "portal", "base_url": "https://www.fundainbusiness.nl", "is_enabled": False},
    {"name": "Pararius", "source_type": "portal", "base_url": "https://www.pararius.nl", "is_enabled": False},
    {"name": "Jaap", "source_type": "portal", "base_url": "https://www.jaap.nl", "is_enabled": False},
    {"name": "Huislijn", "source_type": "portal", "base_url": "https://www.huislijn.nl", "is_enabled": False},
    {"name": "Hormax", "source_type": "broker", "base_url": None, "is_enabled": False},
    {"name": "Horecahuis", "source_type": "broker", "base_url": "https://www.horecahuis.nl", "is_enabled": False},
    {"name": "Local broker websites", "source_type": "broker", "base_url": None, "is_enabled": False},
]

MISSING_TEXT_VALUES = {"", "unknown", "onbekend", "n.v.t.", "nvt", "none", "null"}


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _is_missing_text(value: Any) -> bool:
    return _normalize_text(value).lower() in MISSING_TEXT_VALUES


def _is_generic_title(value: Any, source_name: str) -> bool:
    normalized = _normalize_text(value).lower()
    source_normalized = _normalize_text(source_name).lower()
    if not normalized:
        return True
    if normalized == source_normalized:
        return True
    if normalized in {"makelaar", "makelaars", "broker listing", "listing"}:
        return True
    return normalized.endswith(" makelaars") and len(normalized.split()) <= 3


def _choose_text_value(field_name: str, existing: Any, extracted: Any, source_name: str) -> Any:
    if _is_missing_text(extracted):
        return existing
    if _is_missing_text(existing):
        return extracted
    if field_name == "title" and _is_generic_title(existing, source_name):
        return extracted
    return existing


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _clamp_score(value: float | int) -> int:
    return max(0, min(100, int(round(float(value)))))


def _priority_from_score(score: int) -> str:
    if score >= 90:
        return "urgent"
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _updated_metadata_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for key in ("title", "address", "postal_code", "city", "asking_price", "surface_m2", "property_type"):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


class DealFinderOrchestrator:
    def __init__(
        self,
        database_service: DatabaseService,
        metadata_extractor=extract_listing_metadata,
        source_registry: SourceAdapterRegistry | None = None,
    ):
        self.database_service = database_service
        self.manual_adapter = ManualImportAdapter()
        self.metadata_extractor = metadata_extractor
        self.source_registry = source_registry or build_default_source_registry()
        self.listing_history_engine = ListingHistoryEngine()
        self.property_enrichment_engine = PropertyEnrichmentEngine()

    def seed_default_sources(self) -> list[dict[str, Any]]:
        seeded: list[dict[str, Any]] = []
        for source in DEFAULT_SOURCES:
            row = self.database_service.upsert_listing_source(
                name=source["name"],
                source_type=source["source_type"],
                base_url=source.get("base_url"),
                is_enabled=bool(source.get("is_enabled", False)),
                scan_frequency_minutes=source.get("scan_frequency_minutes"),
                configuration={"mode": "placeholder", "notes": "No live scraping configured in v0.6 foundation."},
            )
            if row:
                seeded.append(row)
        return seeded

    def import_csv(self, csv_text: str) -> dict[str, Any]:
        listings, warnings = self.manual_adapter.import_csv(csv_text)
        return self._ingest_manual_listings(listings, warnings, source_name="manual_csv")

    def import_json(self, json_text: str) -> dict[str, Any]:
        listings, warnings = self.manual_adapter.import_json(json_text)
        return self._ingest_manual_listings(listings, warnings, source_name="manual_json")

    def import_urls(self, urls_text: str) -> dict[str, Any]:
        listings, warnings = self.manual_adapter.import_urls(urls_text, source_name="Local broker websites")
        return self._ingest_manual_listings(
            listings,
            warnings,
            source_name="Local broker websites",
            source_type="broker",
            configuration={"mode": "manual_url_import", "network_fetch": True},
            enrich_metadata=True,
        )

    def import_from_source(self, source_name: str, configuration: dict[str, Any] | None = None) -> dict[str, Any]:
        config = configuration if isinstance(configuration, dict) else {}
        adapter = self.source_registry.resolve(source_name)
        if adapter is None:
            return {
                "ok": False,
                "error": f"Unknown source adapter: {source_name}",
                "warnings": ["No registered adapter found for the requested source."],
                "listings_found": 0,
                "listings_imported": 0,
                "duplicates_skipped": 0,
                "failed_listings": 0,
            }

        is_valid, validation_warnings = adapter.validate_configuration(config)
        if not is_valid:
            return {
                "ok": False,
                "error": "Invalid source configuration.",
                "warnings": validation_warnings,
                "listings_found": 0,
                "listings_imported": 0,
                "duplicates_skipped": 0,
                "failed_listings": 0,
            }

        source_info = adapter.get_source_info()
        source_row = self.database_service.upsert_listing_source(
            name=source_info.source_name,
            source_type=source_info.source_type,
            base_url=(config.get("start_url") or config.get("feed_url") or config.get("base_url")),
            is_enabled=source_info.is_enabled,
            scan_frequency_minutes=None,
            configuration=config,
        )

        source_id = source_row.get("id") if source_row else None
        scan_id = self.database_service.create_scan_run(
            source_id=str(source_id) if source_id else None,
            status="running",
            metadata={"import_type": source_info.source_name, "source": source_name},
        )

        try:
            results = adapter.load_and_normalize_listings(config)
        except Exception as error:
            error_message = f"{type(error).__name__}: {error}"
            self.database_service.complete_scan_run(
                scan_run_id=scan_id,
                status="failed",
                items_found=0,
                items_new=0,
                items_changed=0,
                error_message=error_message,
                metadata={"warnings": validation_warnings},
            )
            return {
                "ok": False,
                "error": error_message,
                "warnings": validation_warnings,
                "listings_found": 0,
                "listings_imported": 0,
                "duplicates_skipped": 0,
                "failed_listings": 1,
                "record_results": [],
                "listing_ids": [],
            }

        ingestion = self._ingest_source_results(
            source_id=str(source_id) if source_id else None,
            source_name=source_info.source_name,
            results=results,
        )
        adapter_stats = adapter.get_last_fetch_stats()

        listings_found = max(int(adapter_stats.get("listings_found") or 0), ingestion["found"])
        duplicates_skipped = int(adapter_stats.get("duplicates_skipped") or 0)
        failed_listings = int(adapter_stats.get("failed_listings") or 0) + ingestion["failed"]

        scan_metadata = {
            "warnings": [*validation_warnings, *ingestion["warnings"]],
            "match_logs": ingestion["match_logs"],
            "record_results": ingestion["record_results"],
            "source_stats": {
                "listings_found": listings_found,
                "listings_imported": ingestion["imported"],
                "duplicates_skipped": duplicates_skipped,
                "failed_listings": failed_listings,
            },
        }

        self.database_service.complete_scan_run(
            scan_run_id=scan_id,
            status="completed",
            items_found=listings_found,
            items_new=ingestion["new"],
            items_changed=ingestion["changed"],
            error_message=None,
            metadata=scan_metadata,
        )

        return {
            "ok": True,
            "source": source_info.source_name,
            "warnings": scan_metadata["warnings"],
            "listings_found": listings_found,
            "listings_imported": ingestion["imported"],
            "duplicates_skipped": duplicates_skipped,
            "failed_listings": failed_listings,
            "new": ingestion["new"],
            "changed": ingestion["changed"],
            "listing_ids": ingestion["listing_ids"],
            "new_listing_ids": ingestion["new_listing_ids"],
            "record_results": ingestion["record_results"],
        }

    def _ingest_source_results(
        self,
        *,
        source_id: str | None,
        source_name: str,
        results: list[SourceRecordResult],
    ) -> dict[str, Any]:
        existing_rows = self.database_service.list_raw_listings(limit=5000)
        warnings: list[str] = []
        match_logs: list[dict[str, Any]] = []
        record_results: list[dict[str, Any]] = []
        listing_ids: list[str] = []
        new_listing_ids: list[str] = []

        imported = 0
        created = 0
        changed = 0
        failed = 0

        for result in results:
            if not result.success or result.listing is None:
                failed += 1
                record_results.append(
                    {
                        "record_index": result.record_index,
                        "success": False,
                        "error": result.error,
                        "source_url": (result.payload or {}).get("source_url"),
                    }
                )
                continue

            listing = result.listing
            try:
                dedupe = match_listing(listing, existing_rows)
                match_logs.append(
                    {
                        "source_url": listing.source_url,
                        "match_method": dedupe.match_method,
                        "confidence": dedupe.confidence,
                        "warnings": dedupe.warnings,
                    }
                )

                listing_row = self.database_service.upsert_listing(
                    source_id=source_id,
                    external_listing_id=listing.external_listing_id,
                    source_url=listing.source_url,
                    title=listing.title,
                    address=listing.address,
                    city=listing.city,
                    asking_price=listing.asking_price,
                    surface_m2=listing.surface_m2,
                    property_type=listing.property_type,
                    listing_status=listing.listing_status,
                    raw_payload=listing.raw_payload,
                    dedupe_match=dedupe,
                )

                listing_id = listing_row.get("id") if listing_row else None
                if not listing_id:
                    failed += 1
                    error_message = "Listing could not be stored."
                    warnings.append(f"Record {result.record_index}: {error_message}")
                    record_results.append(
                        {
                            "record_index": result.record_index,
                            "success": False,
                            "error": error_message,
                            "source_url": listing.source_url,
                        }
                    )
                    continue

                snapshot_result = self.database_service.add_listing_snapshot_if_changed(
                    listing_id=str(listing_id),
                    snapshot={
                        "asking_price": listing_row.get("asking_price"),
                        "listing_status": listing_row.get("listing_status") or listing.listing_status,
                        "title": listing_row.get("title") or listing.title,
                        "description": listing_row.get("description") or listing.description,
                        "surface_m2": listing_row.get("surface_m2") if listing_row else listing.surface_m2,
                        "features": {},
                        "raw_payload": (listing_row or {}).get("raw_payload") or listing.raw_payload,
                    },
                )

                if snapshot_result.get("change_type") == "new_listing":
                    created += 1
                    new_listing_ids.append(str(listing_id))
                elif snapshot_result.get("changed"):
                    changed += 1

                snapshots = self.database_service.get_listing_snapshots(str(listing_id))
                history_result = self.listing_history_engine.build(
                    listing={
                        "source_url": listing.source_url,
                        "listing_status": listing_row.get("listing_status") or listing.listing_status,
                        "first_seen_date": listing_row.get("first_seen_date"),
                        "latest_seen_date": listing_row.get("latest_seen_date"),
                        "original_asking_price": listing_row.get("original_asking_price"),
                        "current_asking_price": listing_row.get("current_asking_price"),
                    },
                    snapshots=snapshots,
                )
                self.database_service.update_listing_history(str(listing_id), history_result.to_dict())

                ranking = self._rank_listing_from_row(listing_row=listing_row, source_name=source_name, all_rows=existing_rows)
                self.database_service.create_or_update_deal_candidate(
                    listing_id=str(listing_id),
                    property_id=dedupe.matched_property_id,
                    investment_score=ranking.component_scores.get("investment_score"),
                    hidden_value_score=ranking.candidate_score,
                    priority=ranking.priority,
                    reasons=ranking.reason_codes,
                    review_status="new",
                )

                if "funda" in _safe_text(source_name).lower():
                    try:
                        listing_row, enrichment_warnings = self._enrich_listing_with_public_data(
                            listing_row=listing_row,
                            source_name=source_name,
                        )
                        warnings.extend(enrichment_warnings)
                    except Exception as error:
                        warnings.append(
                            f"{listing.source_url}: WOZ enrichment failed: {type(error).__name__}: {error}"
                        )

                imported += 1
                listing_ids.append(str(listing_id))
                record_results.append(
                    {
                        "record_index": result.record_index,
                        "success": True,
                        "error": None,
                        "listing_id": str(listing_id),
                        "source_url": listing.source_url,
                    }
                )
            except Exception as error:
                failed += 1
                error_message = f"{type(error).__name__}: {error}"
                warnings.append(f"Record {result.record_index}: {error_message}")
                record_results.append(
                    {
                        "record_index": result.record_index,
                        "success": False,
                        "error": error_message,
                        "source_url": listing.source_url,
                    }
                )

        return {
            "found": len(results),
            "imported": imported,
            "failed": failed,
            "new": created,
            "changed": changed,
            "warnings": warnings,
            "listing_ids": listing_ids,
            "new_listing_ids": new_listing_ids,
            "match_logs": match_logs,
            "record_results": record_results,
        }

    def _enrich_listing_with_public_data(
        self,
        *,
        listing_row: dict[str, Any],
        source_name: str,
        persist: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        source_url = str(listing_row.get("source_url") or "").strip()
        if not source_url:
            return listing_row, ["WOZ enrichment skipped: missing source_url."]

        raw_payload = listing_row.get("raw_payload") if isinstance(listing_row.get("raw_payload"), dict) else {}
        property_payload = {
            "source_url": source_url,
            "title": listing_row.get("title") or raw_payload.get("title"),
            "address": listing_row.get("address") or raw_payload.get("address"),
            "city": listing_row.get("city") or raw_payload.get("city"),
            "asking_price": listing_row.get("asking_price") or raw_payload.get("asking_price"),
            "surface_m2": listing_row.get("surface_m2") or raw_payload.get("surface_m2") or raw_payload.get("living_area"),
            "property_type": listing_row.get("property_type") or raw_payload.get("property_type"),
            "listing_status": listing_row.get("listing_status") or raw_payload.get("listing_status") or "active",
            "postal_code": raw_payload.get("postal_code"),
            "municipality": raw_payload.get("municipality"),
            "raw_extracted_data": raw_payload,
        }
        property_row: dict[str, Any] = {}
        property_id = str(listing_row.get("property_id") or "").strip()
        if persist:
            property_row = self.database_service.upsert_property(property_payload)
            property_id = str(property_row.get("id") or "").strip()
            if not property_id:
                warnings.append(f"{source_url}: BAG/WOZ enrichment unavailable, property row could not be persisted.")
                return listing_row, warnings

        enrichment_result = self.property_enrichment_engine.enrich(
            Property(
                source_url=source_url,
                listing_id=property_id or str(listing_row.get("id") or "").strip() or None,
                title=property_payload.get("title"),
                address=property_payload.get("address"),
                city=property_payload.get("city"),
                postal_code=property_payload.get("postal_code"),
                municipality=property_payload.get("municipality"),
                asking_price=_safe_float(property_payload.get("asking_price")),
                surface_m2=_safe_float(property_payload.get("surface_m2")),
                property_type=property_payload.get("property_type"),
                listing_status=property_payload.get("listing_status") or "active",
                raw_text=str(raw_payload.get("raw_text") or ""),
                description=str(raw_payload.get("description") or listing_row.get("description") or ""),
            )
        )

        if persist:
            self.database_service.upsert_property_enrichment_group(
                property_id=property_id,
                status="completed",
                started_at=enrichment_result.started_at,
                completed_at=enrichment_result.completed_at,
                source=source_name,
                warning_count=sum(1 for item in enrichment_result.items if not item.success),
                error_count=sum(1 for item in enrichment_result.items if not item.success),
                summary={"enrichment_count": len(enrichment_result.items)},
            )

            self.database_service.batch_upsert_property_enrichments(
                property_id=property_id,
                enrichments=[item.to_dict() for item in enrichment_result.items],
            )

        items_by_key = {item.enrichment_key: item for item in enrichment_result.items}
        woz_item = items_by_key.get("latest_woz_value")
        valuation_year_item = items_by_key.get("woz_valuation_year")
        bag_id_item = items_by_key.get("bag_id")
        bag_address_item = items_by_key.get("bag_address_id")
        bag_vbo_item = items_by_key.get("bag_verblijfsobject_id")
        bag_pand_item = items_by_key.get("bag_pand_id")
        bag_building_year_item = items_by_key.get("bag_building_year")
        bag_usage_item = items_by_key.get("bag_usage_purpose")
        bag_status_item = items_by_key.get("bag_status")
        bag_floor_item = items_by_key.get("bag_official_floor_area_m2")
        bag_confidence_item = items_by_key.get("bag_confidence_score")
        bag_source_item = items_by_key.get("bag_source")
        bag_retrieval_item = items_by_key.get("bag_retrieval_date")
        bag_quality_item = items_by_key.get("bag_quality_flags")
        funda_area_item = items_by_key.get("funda_living_area_m2")
        diff_m2_item = items_by_key.get("living_area_difference_m2")
        diff_pct_item = items_by_key.get("living_area_difference_percentage")
        calc_area_item = items_by_key.get("calculation_area_m2")
        calc_area_source_item = items_by_key.get("calculation_area_source")
        asking_price_per_m2_item = items_by_key.get("asking_price_per_m2")
        woz_per_m2_item = items_by_key.get("woz_value_per_m2")

        woz_value = _safe_float(woz_item.value) if woz_item else None
        if woz_value is None:
            warnings.append(f"{source_url}: WOZ data unavailable for this listing.")

        updated_raw_payload = {
            **raw_payload,
            "bag_id": bag_id_item.value if bag_id_item else raw_payload.get("bag_id"),
            "bag_address_id": bag_address_item.value if bag_address_item else raw_payload.get("bag_address_id"),
            "bag_verblijfsobject_id": bag_vbo_item.value if bag_vbo_item else raw_payload.get("bag_verblijfsobject_id"),
            "bag_pand_id": bag_pand_item.value if bag_pand_item else raw_payload.get("bag_pand_id"),
            "bag_building_year": _safe_int(bag_building_year_item.value) if bag_building_year_item else raw_payload.get("bag_building_year"),
            "construction_year_bag": _safe_int(bag_building_year_item.value) if bag_building_year_item else raw_payload.get("construction_year_bag") or raw_payload.get("bag_building_year"),
            "bag_usage_purpose": bag_usage_item.value if bag_usage_item else raw_payload.get("bag_usage_purpose"),
            "usage_purpose": bag_usage_item.value if bag_usage_item else raw_payload.get("usage_purpose") or raw_payload.get("bag_usage_purpose"),
            "bag_status": bag_status_item.value if bag_status_item else raw_payload.get("bag_status"),
            "bag_official_floor_area_m2": _safe_float(bag_floor_item.value) if bag_floor_item else raw_payload.get("bag_official_floor_area_m2"),
            "official_floor_area_m2": _safe_float(bag_floor_item.value) if bag_floor_item else raw_payload.get("official_floor_area_m2") or raw_payload.get("bag_official_floor_area_m2"),
            "coordinates": {
                "rd": raw_payload.get("bag_coordinates_rd"),
                "ll": raw_payload.get("bag_coordinates_ll"),
            },
            "bag_confidence_score": _safe_int(bag_confidence_item.value) if bag_confidence_item else raw_payload.get("bag_confidence_score"),
            "confidence_score": _safe_int(bag_confidence_item.value) if bag_confidence_item else raw_payload.get("confidence_score") or raw_payload.get("bag_confidence_score"),
            "bag_source": bag_source_item.value if bag_source_item else raw_payload.get("bag_source"),
            "source": bag_source_item.value if bag_source_item else raw_payload.get("source") or raw_payload.get("bag_source"),
            "bag_retrieval_date": bag_retrieval_item.value if bag_retrieval_item else raw_payload.get("bag_retrieval_date"),
            "retrieval_date": bag_retrieval_item.value if bag_retrieval_item else raw_payload.get("retrieval_date") or raw_payload.get("bag_retrieval_date"),
            "bag_quality_flags": bag_quality_item.value if bag_quality_item else raw_payload.get("bag_quality_flags") or [],
            "funda_living_area_m2": _safe_float(funda_area_item.value) if funda_area_item else raw_payload.get("funda_living_area_m2"),
            "living_area_difference_m2": _safe_float(diff_m2_item.value) if diff_m2_item else raw_payload.get("living_area_difference_m2"),
            "living_area_difference_percentage": _safe_float(diff_pct_item.value) if diff_pct_item else raw_payload.get("living_area_difference_percentage"),
            "calculation_area_m2": _safe_float(calc_area_item.value) if calc_area_item else raw_payload.get("calculation_area_m2"),
            "calculation_area_source": calc_area_source_item.value if calc_area_source_item else raw_payload.get("calculation_area_source"),
            "asking_price_per_m2": _safe_float(asking_price_per_m2_item.value) if asking_price_per_m2_item else raw_payload.get("asking_price_per_m2"),
            "woz_value_per_m2": _safe_float(woz_per_m2_item.value) if woz_per_m2_item else raw_payload.get("woz_value_per_m2"),
            "latest_woz_value": woz_value,
            "woz_valuation_year": _safe_int(valuation_year_item.value) if valuation_year_item else raw_payload.get("woz_valuation_year"),
            "woz_retrieval_date": woz_item.retrieval_date if woz_item else None,
            "woz_source": woz_item.source if woz_item else "Kadaster WOZ-waardeloket",
            "woz_confidence_score": woz_item.confidence_score if woz_item else 0,
            "woz_historical_values": (items_by_key.get("woz_historical_values").value if items_by_key.get("woz_historical_values") else []),
        }

        transient_listing = {
            **listing_row,
            "property_id": property_id or listing_row.get("property_id"),
            "raw_payload": updated_raw_payload,
            "latest_woz_value": woz_value,
            "woz_valuation_year": _safe_int(valuation_year_item.value) if valuation_year_item else listing_row.get("woz_valuation_year"),
        }

        if not persist:
            return transient_listing, warnings

        updated_listing = self.database_service.upsert_listing(
            listing_id=str(listing_row.get("id") or "") or None,
            source_id=listing_row.get("source_id"),
            external_listing_id=listing_row.get("external_listing_id"),
            source_url=source_url,
            title=listing_row.get("title"),
            address=listing_row.get("address"),
            city=listing_row.get("city"),
            asking_price=listing_row.get("asking_price"),
            surface_m2=listing_row.get("surface_m2"),
            property_type=listing_row.get("property_type"),
            listing_status=listing_row.get("listing_status") or "active",
            raw_payload=updated_raw_payload,
            dedupe_match=type("Dedupe", (), {"matched_property_id": property_id})(),
        )

        return (updated_listing or transient_listing), warnings

    def refresh_listing_metadata(self, listing_id: str) -> dict[str, Any]:
        detail = self.database_service.get_listing_detail(listing_id)
        listing = detail.get("listing") or {}
        if not listing:
            return {"ok": False, "error": "Listing not found."}

        listing_row, extraction_result = self._enrich_listing_row(
            listing_row=listing,
            source_id=listing.get("source_id"),
            external_listing_id=listing.get("external_listing_id"),
            source_name=(detail.get("source") or {}).get("name") or "Local broker websites",
            dedupe_property_id=listing.get("property_id"),
        )

        if not listing_row.get("id"):
            return {
                "ok": False,
                "error": "Listing metadata refresh failed.",
                "extraction": extraction_result.to_dict(),
            }

        snapshot_result = self.database_service.add_listing_snapshot_if_changed(
            listing_id=str(listing_row.get("id")),
            snapshot={
                "asking_price": listing_row.get("asking_price"),
                "listing_status": listing_row.get("listing_status") or "active",
                "title": listing_row.get("title"),
                "description": listing_row.get("description"),
                "surface_m2": listing_row.get("surface_m2"),
                "features": {},
                "raw_payload": listing_row.get("raw_payload") or {},
            },
        )

        ranking = self._rank_listing_from_row(
            listing_row,
            source_name=(detail.get("source") or {}).get("name") or "manual",
            all_rows=self.database_service.list_raw_listings(limit=5000),
        )
        existing_candidate = detail.get("candidate") or {}
        self.database_service.create_or_update_deal_candidate(
            listing_id=str(listing_row.get("id")),
            property_id=listing_row.get("property_id"),
            investment_score=ranking.component_scores.get("investment_score"),
            hidden_value_score=ranking.candidate_score,
            priority=ranking.priority,
            reasons=ranking.reason_codes,
            review_status=existing_candidate.get("review_status") or "new",
        )

        refreshed_detail = self.database_service.get_listing_detail(str(listing_row.get("id")))

        return {
            "ok": True,
            "listing_id": str(listing_row.get("id")),
            "snapshot_changed": bool(snapshot_result.get("changed")),
            "snapshot_change_type": snapshot_result.get("change_type"),
            "extraction": extraction_result.to_dict(),
            "listing": refreshed_detail.get("listing") or listing_row,
            "candidate": refreshed_detail.get("candidate") or existing_candidate,
            "latest_snapshot": refreshed_detail.get("latest_snapshot") or {},
        }

    def _ingest_manual_listings(
        self,
        listings: list[NormalizedListing],
        warnings: list[str],
        source_name: str,
        source_type: str = "manual_import",
        configuration: dict[str, Any] | None = None,
        enrich_metadata: bool = False,
    ) -> dict[str, Any]:
        source_row = self.database_service.upsert_listing_source(
            name=source_name,
            source_type=source_type,
            base_url=None,
            is_enabled=True,
            scan_frequency_minutes=None,
            configuration=configuration or {"mode": "manual_import"},
        )
        if not source_row:
            warnings = [*warnings, "Source could not be resolved or created for import."]

        source_id = source_row.get("id") if source_row else None
        scan_id = self.database_service.create_scan_run(source_id=str(source_id) if source_id else None, status="running", metadata={"import_type": source_name})

        found = len(listings)
        created = 0
        changed = 0
        ingested_ids: list[str] = []
        match_logs: list[dict[str, Any]] = []
        enrichment_results: list[dict[str, Any]] = []
        record_results: list[dict[str, Any]] = []

        existing_rows = self.database_service.list_raw_listings(limit=5000)
        for record_index, listing in enumerate(listings, start=1):
            try:
                dedupe = match_listing(listing, existing_rows)
                match_logs.append(
                    {
                        "source_url": listing.source_url,
                        "match_method": dedupe.match_method,
                        "confidence": dedupe.confidence,
                        "warnings": dedupe.warnings,
                    }
                )

                listing_row = self.database_service.upsert_listing(
                    source_id=str(source_id) if source_id else None,
                    external_listing_id=listing.external_listing_id,
                    source_url=listing.source_url,
                    title=listing.title,
                    address=listing.address,
                    city=listing.city,
                    asking_price=listing.asking_price,
                    surface_m2=listing.surface_m2,
                    property_type=listing.property_type,
                    listing_status=listing.listing_status,
                    raw_payload=listing.raw_payload,
                    dedupe_match=dedupe,
                )

                listing_id = listing_row.get("id") if listing_row else None
                if not listing_id:
                    error_message = "Listing could not be stored."
                    warnings.append(f"Record {record_index}: {error_message}")
                    record_results.append(
                        {
                            "record_index": record_index,
                            "success": False,
                            "error": error_message,
                            "source_url": listing.source_url,
                        }
                    )
                    continue
                ingested_ids.append(str(listing_id))

                if enrich_metadata:
                    listing_row, extraction_result = self._enrich_listing_row(
                        listing_row=listing_row,
                        source_id=str(source_id) if source_id else None,
                        external_listing_id=listing.external_listing_id,
                        source_name=source_name,
                        dedupe_property_id=dedupe.matched_property_id,
                    )
                    enrichment_results.append(
                        {
                            "listing_id": str(listing_row.get("id") or listing_id),
                            "source_url": listing.source_url,
                            "success": extraction_result.success,
                            "extraction_method": extraction_result.extraction_method,
                            "confidence": extraction_result.confidence,
                            "warnings": extraction_result.warnings,
                        }
                    )
                    if extraction_result.warnings:
                        warnings.extend([f"{listing.source_url}: {item}" for item in extraction_result.warnings])
                    listing_id = listing_row.get("id") if listing_row else listing_id
                    if not listing_id:
                        error_message = "Listing metadata enrichment did not return a listing id."
                        warnings.append(f"Record {record_index}: {error_message}")
                        record_results.append(
                            {
                                "record_index": record_index,
                                "success": False,
                                "error": error_message,
                                "source_url": listing.source_url,
                            }
                        )
                        continue

                snapshot_result = self.database_service.add_listing_snapshot_if_changed(
                    listing_id=str(listing_id),
                    snapshot={
                        "asking_price": listing_row.get("asking_price"),
                        "listing_status": listing_row.get("listing_status") or listing.listing_status,
                        "title": listing_row.get("title") or listing.title,
                        "description": listing_row.get("description") or listing.description,
                        "surface_m2": listing_row.get("surface_m2") if listing_row else listing.surface_m2,
                        "features": {},
                        "raw_payload": (listing_row or {}).get("raw_payload") or listing.raw_payload,
                    },
                )

                if snapshot_result.get("change_type") == "new_listing":
                    created += 1
                elif snapshot_result.get("changed"):
                    changed += 1

                ranking = self._rank_listing_from_row(listing_row=listing_row, source_name=source_name, all_rows=existing_rows)
                self.database_service.create_or_update_deal_candidate(
                    listing_id=str(listing_id),
                    property_id=dedupe.matched_property_id,
                    investment_score=ranking.component_scores.get("investment_score"),
                    hidden_value_score=ranking.candidate_score,
                    priority=ranking.priority,
                    reasons=ranking.reason_codes,
                    review_status="new",
                )

                record_results.append(
                    {
                        "record_index": record_index,
                        "success": True,
                        "error": None,
                        "listing_id": str(listing_id),
                        "source_url": listing.source_url,
                    }
                )
            except Exception as error:
                error_message = f"{type(error).__name__}: {error}"
                warnings.append(f"Record {record_index}: {error_message}")
                record_results.append(
                    {
                        "record_index": record_index,
                        "success": False,
                        "error": error_message,
                        "source_url": getattr(listing, "source_url", None),
                    }
                )

                if isinstance(error, RuntimeError):
                    raise
                continue

        self.database_service.complete_scan_run(
            scan_run_id=scan_id,
            status="completed",
            items_found=found,
            items_new=created,
            items_changed=changed,
            error_message=None,
            metadata={"warnings": warnings, "match_logs": match_logs},
        )

        return {
            "found": found,
            "new": created,
            "changed": changed,
            "warnings": warnings,
            "listing_ids": ingested_ids,
            "enrichment": enrichment_results,
            "record_results": record_results,
        }

    def _rank_listing_from_row(self, listing_row: dict[str, Any], source_name: str, all_rows: list[dict[str, Any]] | None = None):
        enriched = self._listing_data_with_fallbacks(listing_row)
        city_benchmarks = self._city_benchmarks(enriched.get("city"), all_rows)
        investment_score = self._investment_score(enriched, city_benchmarks)
        opportunity_score = self._opportunity_score(enriched, city_benchmarks, investment_score)

        listing = NormalizedListing(
            source_name=source_name,
            source_url=str(listing_row.get("source_url") or ""),
            external_listing_id=listing_row.get("external_listing_id"),
            title=enriched.get("title"),
            address=enriched.get("address"),
            city=enriched.get("city"),
            asking_price=enriched.get("asking_price"),
            surface_m2=enriched.get("surface_m2"),
            property_type=enriched.get("property_type"),
            description=listing_row.get("description"),
            listing_status=listing_row.get("listing_status") or "active",
            raw_payload=listing_row.get("raw_payload") or {},
        )
        ranking_result = rank_listing(
            listing,
            context={
                "price_per_m2": enriched.get("price_per_m2"),
                "days_on_market": _safe_int(enriched.get("days_on_market")),
                "price_reduction_count": _safe_int(enriched.get("price_reduction_count")),
                "investment_score": investment_score,
            },
        )

        return type(ranking_result)(
            candidate_score=opportunity_score,
            priority=_priority_from_score(opportunity_score),
            reason_codes=ranking_result.reason_codes,
            missing_data_warnings=ranking_result.missing_data_warnings,
            component_scores={
                **(ranking_result.component_scores or {}),
                "investment_score": investment_score,
                "opportunity_score": opportunity_score,
            },
        )

    def _listing_data_with_fallbacks(self, listing_row: dict[str, Any]) -> dict[str, Any]:
        raw_payload = listing_row.get("raw_payload") if isinstance(listing_row.get("raw_payload"), dict) else {}

        def pick(*keys: str):
            for key in keys:
                if key in listing_row and listing_row.get(key) not in (None, ""):
                    return listing_row.get(key)
                if key in raw_payload and raw_payload.get(key) not in (None, ""):
                    return raw_payload.get(key)
            return None

        asking_price = _safe_float(pick("asking_price", "current_asking_price"))
        surface_m2 = _safe_float(pick("calculation_area_m2", "surface_m2", "living_area"))
        price_per_m2 = _safe_float(pick("asking_price_per_m2", "price_per_m2"))
        if price_per_m2 is None and asking_price not in (None, 0) and surface_m2 not in (None, 0):
            price_per_m2 = round(float(asking_price) / float(surface_m2), 2)

        return {
            "title": pick("title"),
            "address": pick("address"),
            "city": pick("city"),
            "property_type": pick("property_type"),
            "asking_price": asking_price,
            "surface_m2": surface_m2,
            "calculation_area_m2": _safe_float(pick("calculation_area_m2")),
            "calculation_area_source": _safe_text(pick("calculation_area_source")),
            "energy_label": _safe_text(pick("energy_label")).upper(),
            "construction_year": _safe_int(pick("construction_year", "bag_building_year")),
            "bag_building_year": _safe_int(pick("bag_building_year")),
            "usage_purpose": _safe_text(pick("bag_usage_purpose", "usage_purpose")),
            "plot_size_m2": _safe_float(pick("plot_size_m2", "plot_size")),
            "bedrooms": _safe_int(pick("bedrooms")),
            "days_on_market": _safe_int(pick("days_on_market")),
            "price_reduction_count": _safe_int(pick("price_reduction_count")),
            "price_per_m2": price_per_m2,
        }

    def _city_benchmarks(self, city: Any, all_rows: list[dict[str, Any]] | None = None) -> dict[str, float | None]:
        city_name = _safe_text(city)
        rows = list(all_rows or self.database_service.list_raw_listings(limit=5000))

        city_prices: list[float] = []
        city_ppm2: list[float] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            enriched = self._listing_data_with_fallbacks(row)
            if city_name and _safe_text(enriched.get("city")).lower() != city_name.lower():
                continue
            asking_price = _safe_float(enriched.get("asking_price"))
            if asking_price is not None:
                city_prices.append(asking_price)
            price_per_m2 = _safe_float(enriched.get("price_per_m2"))
            if price_per_m2 is not None:
                city_ppm2.append(price_per_m2)

        return {
            "avg_price_per_m2": round(sum(city_ppm2) / len(city_ppm2), 2) if city_ppm2 else None,
            "median_asking_price": round(float(median(city_prices)), 2) if city_prices else None,
        }

    def _investment_score(self, data: dict[str, Any], benchmarks: dict[str, float | None]) -> int:
        score = 0

        price_per_m2 = _safe_float(data.get("price_per_m2"))
        avg_city_ppm2 = _safe_float(benchmarks.get("avg_price_per_m2"))
        if price_per_m2 is not None and avg_city_ppm2 is not None and price_per_m2 < avg_city_ppm2:
            score += 25

        living_area = _safe_float(data.get("surface_m2"))
        if living_area is not None and living_area > 100:
            score += 20

        if _safe_text(data.get("energy_label")).upper() in {"A", "A+", "A++", "A+++", "A++++", "B"}:
            score += 15

        construction_year = _safe_int(data.get("construction_year"))
        if construction_year is not None and construction_year > 1995:
            score += 15

        asking_price = _safe_float(data.get("asking_price"))
        median_asking_price = _safe_float(benchmarks.get("median_asking_price"))
        if asking_price is not None and median_asking_price is not None and asking_price < median_asking_price:
            score += 15

        plot_size = _safe_float(data.get("plot_size_m2"))
        if plot_size is not None and plot_size > 150:
            score += 10

        return _clamp_score(score)

    def _opportunity_score(self, data: dict[str, Any], benchmarks: dict[str, float | None], investment_score: int) -> int:
        price_per_m2 = _safe_float(data.get("price_per_m2"))
        avg_city_ppm2 = _safe_float(benchmarks.get("avg_price_per_m2"))

        percentile_component = 0.0
        if price_per_m2 is not None and avg_city_ppm2 is not None and avg_city_ppm2 > 0:
            ratio = (avg_city_ppm2 - price_per_m2) / avg_city_ppm2
            percentile_component = max(0.0, min(20.0, ratio * 100.0 * 0.4))

        tracked_fields = [
            data.get("surface_m2"),
            data.get("energy_label"),
            data.get("construction_year"),
            data.get("plot_size_m2"),
            data.get("bedrooms"),
            data.get("price_per_m2"),
        ]
        filled_fields = sum(1 for value in tracked_fields if value not in (None, "", 0))
        completeness_component = (filled_fields / len(tracked_fields)) * 20.0

        upside_component = 0.0
        if int(_safe_int(data.get("price_reduction_count")) or 0) > 0:
            upside_component += 8.0
        if _safe_int(data.get("construction_year")) is not None and int(_safe_int(data.get("construction_year")) or 0) < 1990:
            upside_component += 4.0
        if _safe_text(data.get("energy_label")).upper() in {"D", "E", "F", "G"}:
            upside_component += 3.0

        score = (float(investment_score) * 0.55) + percentile_component + completeness_component + upside_component
        return _clamp_score(score)

    def _enrich_listing_row(
        self,
        *,
        listing_row: dict[str, Any],
        source_id: str | None,
        external_listing_id: str | None,
        source_name: str,
        dedupe_property_id: str | None,
    ) -> tuple[dict[str, Any], ListingExtractionResult]:
        source_url = str(listing_row.get("source_url") or "")
        extraction_result = self.metadata_extractor(source_url)

        raw_payload = listing_row.get("raw_payload") if isinstance(listing_row.get("raw_payload"), dict) else {}
        existing_postal_code = raw_payload.get("postal_code")

        resolved_title = _choose_text_value("title", listing_row.get("title"), extraction_result.title, source_name)
        resolved_address = _choose_text_value("address", listing_row.get("address"), extraction_result.address, source_name)
        resolved_postal_code = _choose_text_value("postal_code", existing_postal_code, extraction_result.postal_code, source_name)
        resolved_city = _choose_text_value("city", listing_row.get("city"), extraction_result.city, source_name)
        resolved_asking_price = extraction_result.asking_price if extraction_result.asking_price is not None else listing_row.get("asking_price")
        resolved_surface_m2 = extraction_result.surface_m2 if extraction_result.surface_m2 is not None else listing_row.get("surface_m2")
        resolved_property_type = _choose_text_value("property_type", listing_row.get("property_type"), extraction_result.property_type, source_name)

        updated_raw_payload = {
            **raw_payload,
            "postal_code": resolved_postal_code,
            "metadata_extraction": {
                "success": extraction_result.success,
                "extraction_method": extraction_result.extraction_method,
                "confidence": extraction_result.confidence,
                "warnings": extraction_result.warnings,
            },
            "metadata": extraction_result.to_dict(),
        }

        merged_before = {
            "title": listing_row.get("title"),
            "address": listing_row.get("address"),
            "postal_code": existing_postal_code,
            "city": listing_row.get("city"),
            "asking_price": listing_row.get("asking_price"),
            "surface_m2": listing_row.get("surface_m2"),
            "property_type": listing_row.get("property_type"),
        }
        merged_after = {
            "title": resolved_title,
            "address": resolved_address,
            "postal_code": resolved_postal_code,
            "city": resolved_city,
            "asking_price": resolved_asking_price,
            "surface_m2": resolved_surface_m2,
            "property_type": resolved_property_type,
        }

        updated_listing = self.database_service.upsert_listing(
            listing_id=str(listing_row.get("id") or "") or None,
            source_id=source_id,
            external_listing_id=external_listing_id,
            source_url=source_url,
            title=resolved_title,
            address=resolved_address,
            city=resolved_city,
            asking_price=resolved_asking_price,
            surface_m2=resolved_surface_m2,
            property_type=resolved_property_type,
            listing_status=listing_row.get("listing_status") or "active",
            raw_payload=updated_raw_payload,
            dedupe_match=type("Dedupe", (), {"matched_property_id": dedupe_property_id})(),
        )

        LOGGER.info(
            "Listing metadata refresh listing_id=%s source=%s extraction_success=%s updated_fields=%s",
            listing_row.get("id"),
            source_name,
            extraction_result.success,
            _updated_metadata_fields(merged_before, merged_after),
        )

        return (updated_listing or listing_row), extraction_result
