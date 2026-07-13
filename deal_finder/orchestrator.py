from __future__ import annotations

from typing import Any

from deal_finder.deduplication import match_listing
from deal_finder.models import NormalizedListing
from deal_finder.ranking import rank_listing
from deal_finder.sources.manual_import import ManualImportAdapter
from services.database import DatabaseService

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


class DealFinderOrchestrator:
    def __init__(self, database_service: DatabaseService):
        self.database_service = database_service
        self.manual_adapter = ManualImportAdapter()

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
            configuration={"mode": "manual_url_import", "network_fetch": False},
        )

    def _ingest_manual_listings(
        self,
        listings: list[NormalizedListing],
        warnings: list[str],
        source_name: str,
        source_type: str = "manual_import",
        configuration: dict[str, Any] | None = None,
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

        existing_rows = self.database_service.list_raw_listings(limit=5000)
        for listing in listings:
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
                continue
            ingested_ids.append(str(listing_id))

            snapshot_result = self.database_service.add_listing_snapshot_if_changed(
                listing_id=str(listing_id),
                snapshot={
                    "asking_price": listing.asking_price,
                    "listing_status": listing.listing_status,
                    "title": listing.title,
                    "description": listing.description,
                    "surface_m2": listing.surface_m2,
                    "features": {},
                    "raw_payload": listing.raw_payload,
                },
            )

            if snapshot_result.get("change_type") == "new_listing":
                created += 1
            elif snapshot_result.get("changed"):
                changed += 1

            ranking = rank_listing(
                listing,
                context={
                    "price_per_m2": (listing.asking_price / listing.surface_m2) if listing.asking_price and listing.surface_m2 else None,
                    "days_on_market": None,
                    "price_reduction_count": None,
                    "investment_score": None,
                },
            )
            self.database_service.create_or_update_deal_candidate(
                listing_id=str(listing_id),
                property_id=dedupe.matched_property_id,
                investment_score=None,
                hidden_value_score=ranking.candidate_score,
                priority=ranking.priority,
                reasons=ranking.reason_codes,
                review_status="new",
            )

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
        }
