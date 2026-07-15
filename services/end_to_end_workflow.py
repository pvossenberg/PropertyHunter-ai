from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from deal_finder.orchestrator import DealFinderOrchestrator
from models.property import Property
from services.database import DatabaseService
from services.investment_intelligence import InvestmentIntelligenceEngine
from services.opportunity_intelligence import OpportunityIntelligenceEngine


@dataclass(frozen=True)
class EndToEndRow:
    listing_id: str
    address: str
    asking_price: float | None
    deal_score: int
    investment_score: int
    listing_history: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "address": self.address,
            "asking_price": self.asking_price,
            "deal_score": self.deal_score,
            "investment_score": self.investment_score,
            "listing_history": self.listing_history,
            "source": self.source,
        }


@dataclass(frozen=True)
class EndToEndRunResult:
    ok: bool
    listings_found: int
    new_listings: int
    processed_listings: int
    top_rows: list[EndToEndRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "listings_found": self.listings_found,
            "new_listings": self.new_listings,
            "processed_listings": self.processed_listings,
            "top_rows": [row.to_dict() for row in self.top_rows],
            "warnings": list(self.warnings),
            "error": self.error,
        }


class PropertyHunterEndToEndWorkflow:
    def __init__(
        self,
        *,
        orchestrator: DealFinderOrchestrator,
        database_service: DatabaseService,
        investment_engine: InvestmentIntelligenceEngine | None = None,
        opportunity_engine: OpportunityIntelligenceEngine | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.database_service = database_service
        self.investment_engine = investment_engine or InvestmentIntelligenceEngine()
        self.opportunity_engine = opportunity_engine or OpportunityIntelligenceEngine(investment_engine=self.investment_engine)
        self.logger = logger or logging.getLogger(__name__)

    def run_funda(self, *, start_url: str, max_pages: int, timeout_seconds: float, top_n: int = 10) -> EndToEndRunResult:
        self.logger.info("Step 1/5: starting Funda scan")
        import_result = self.orchestrator.import_from_source(
            "funda",
            {
                "start_url": start_url,
                "max_pages": max(1, int(max_pages)),
                "timeout_seconds": float(timeout_seconds),
            },
        )

        if not import_result.get("ok"):
            error = str(import_result.get("error") or "Unknown source import error")
            self.logger.error("Funda scan failed: %s", error)
            return EndToEndRunResult(
                ok=False,
                listings_found=0,
                new_listings=0,
                processed_listings=0,
                warnings=list(import_result.get("warnings") or []),
                error=error,
            )

        listings_found = int(import_result.get("listings_found") or 0)
        new_listing_ids = [str(item) for item in (import_result.get("new_listing_ids") or []) if str(item)]
        self.logger.info("Step 2/5: found %d listings, %d new", listings_found, len(new_listing_ids))

        if not new_listing_ids:
            self.logger.info("No new listings to process")
            return EndToEndRunResult(
                ok=True,
                listings_found=listings_found,
                new_listings=0,
                processed_listings=0,
                top_rows=[],
                warnings=list(import_result.get("warnings") or []),
                error=None,
            )

        portfolio = self._build_portfolio()
        self.logger.info("Step 3/5: computing listing history/investment/opportunity for new listings")

        rows: list[EndToEndRow] = []
        warnings = list(import_result.get("warnings") or [])
        for listing_id in new_listing_ids:
            detail = self.database_service.get_listing_detail(listing_id)
            listing = detail.get("listing") if isinstance(detail, dict) else {}
            source = detail.get("source") if isinstance(detail, dict) else {}
            if not isinstance(listing, dict) or not listing:
                warnings.append(f"Listing {listing_id} not found after import")
                continue

            property_obj = self._listing_to_property(listing)
            investment_result = self.investment_engine.evaluate(property_obj, portfolio=portfolio)
            opportunity_result = self.opportunity_engine.evaluate(property_obj, portfolio=portfolio, investment_result=investment_result)

            self.database_service.create_or_update_deal_candidate(
                listing_id=listing_id,
                property_id=str(listing.get("property_id") or "") or None,
                investment_score=int(investment_result.overall_score),
                hidden_value_score=int(opportunity_result.opportunity_score),
                priority=self._priority_from_score(int(opportunity_result.opportunity_score)),
                reasons=[item.opportunity_type for item in opportunity_result.detected_opportunities[:3]],
                review_status="new",
            )

            rows.append(
                EndToEndRow(
                    listing_id=listing_id,
                    address=self._listing_address(listing),
                    asking_price=self._safe_float(listing.get("asking_price")),
                    deal_score=int(opportunity_result.opportunity_score),
                    investment_score=int(investment_result.overall_score),
                    listing_history=self._listing_history_summary(listing),
                    source=self._listing_source(source, listing),
                )
            )

        self.logger.info("Step 4/5: persisted intelligence for %d listings", len(rows))
        top_rows = sorted(rows, key=lambda item: item.deal_score, reverse=True)[: max(1, int(top_n))]
        self.logger.info("Step 5/5: prepared top %d overview rows", len(top_rows))

        return EndToEndRunResult(
            ok=True,
            listings_found=listings_found,
            new_listings=len(new_listing_ids),
            processed_listings=len(rows),
            top_rows=top_rows,
            warnings=warnings,
            error=None,
        )

    def _build_portfolio(self) -> list[Property]:
        rows = self.database_service.list_raw_listings(limit=5000)
        portfolio: list[Property] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            portfolio.append(self._listing_to_property(row))
        return portfolio

    def _listing_to_property(self, listing: dict[str, Any]) -> Property:
        return Property(
            source_url=str(listing.get("source_url") or ""),
            title=listing.get("title"),
            address=listing.get("address"),
            city=listing.get("city"),
            asking_price=self._safe_float(listing.get("asking_price")),
            surface_m2=self._safe_float(listing.get("surface_m2")),
            bedrooms=self._safe_int(listing.get("bedrooms")),
            property_type=listing.get("property_type"),
            construction_year=self._safe_int(listing.get("construction_year")),
            energy_label=listing.get("energy_label"),
            plot_size_m2=self._safe_float(listing.get("plot_size_m2")),
            listing_status=str(listing.get("listing_status") or "unknown"),
            description=listing.get("description"),
            raw_text=listing.get("description"),
        )

    def _listing_address(self, listing: dict[str, Any]) -> str:
        address = str(listing.get("address") or "").strip()
        city = str(listing.get("city") or "").strip()
        if address and city and city.lower() not in address.lower():
            return f"{address}, {city}"
        if address:
            return address
        if city:
            return city
        return "Onbekend"

    def _listing_history_summary(self, listing: dict[str, Any]) -> str:
        status = str(listing.get("listing_status") or "unknown")
        days_on_market = listing.get("days_on_market")
        reduction_pct = self._safe_float(listing.get("total_price_reduction_percentage"))
        if reduction_pct is not None:
            return f"status={status}, {int(reduction_pct)}% prijsdaling"
        if days_on_market not in (None, ""):
            return f"status={status}, {days_on_market} dagen"
        return f"status={status}"

    def _listing_source(self, source_row: dict[str, Any], listing: dict[str, Any]) -> str:
        if isinstance(source_row, dict):
            name = str(source_row.get("name") or "").strip()
            if name:
                return name
        return str(listing.get("source_url") or "Onbekend")

    def _priority_from_score(self, score: int) -> str:
        if score >= 80:
            return "high"
        if score >= 60:
            return "medium"
        return "low"

    def _safe_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _safe_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
