from __future__ import annotations

import logging
from typing import Any

from deal_finder.deduplication import match_listing
from deal_finder.extraction import ListingExtractionResult, extract_listing_metadata
from deal_finder.models import NormalizedListing
from deal_finder.ranking import rank_listing
from deal_finder.sources.base import SourceRecordResult
from deal_finder.sources.manual_import import ManualImportAdapter
from deal_finder.sources.registry import SourceAdapterRegistry, build_default_source_registry
from services.database import DatabaseService
from services.listing_history import ListingHistoryEngine

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

                ranking = self._rank_listing_from_row(listing_row=listing_row, source_name=source_name)
                self.database_service.create_or_update_deal_candidate(
                    listing_id=str(listing_id),
                    property_id=dedupe.matched_property_id,
                    investment_score=None,
                    hidden_value_score=ranking.candidate_score,
                    priority=ranking.priority,
                    reasons=ranking.reason_codes,
                    review_status="new",
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

        ranking = self._rank_listing_from_row(listing_row, source_name=(detail.get("source") or {}).get("name") or "manual")
        existing_candidate = detail.get("candidate") or {}
        self.database_service.create_or_update_deal_candidate(
            listing_id=str(listing_row.get("id")),
            property_id=listing_row.get("property_id"),
            investment_score=None,
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

                ranking = self._rank_listing_from_row(listing_row=listing_row, source_name=source_name)
                self.database_service.create_or_update_deal_candidate(
                    listing_id=str(listing_id),
                    property_id=dedupe.matched_property_id,
                    investment_score=None,
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

    def _rank_listing_from_row(self, listing_row: dict[str, Any], source_name: str):
        listing = NormalizedListing(
            source_name=source_name,
            source_url=str(listing_row.get("source_url") or ""),
            external_listing_id=listing_row.get("external_listing_id"),
            title=listing_row.get("title"),
            address=listing_row.get("address"),
            city=listing_row.get("city"),
            asking_price=listing_row.get("asking_price"),
            surface_m2=listing_row.get("surface_m2"),
            property_type=listing_row.get("property_type"),
            description=listing_row.get("description"),
            listing_status=listing_row.get("listing_status") or "active",
            raw_payload=listing_row.get("raw_payload") or {},
        )
        return rank_listing(
            listing,
            context={
                "price_per_m2": (listing.asking_price / listing.surface_m2) if listing.asking_price and listing.surface_m2 else None,
                "days_on_market": None,
                "price_reduction_count": None,
                "investment_score": None,
            },
        )

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
