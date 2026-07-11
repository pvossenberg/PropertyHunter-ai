from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class SaleRecord:
    """Represents a historic sale-like transaction for a property."""

    transaction_date: date
    transaction_type: str
    transaction_price: float | None
    source_name: str
    source_url: str | None
    confidence: str
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dictionary representation."""
        payload = asdict(self)
        payload["transaction_date"] = self.transaction_date.isoformat()
        return payload


@dataclass(frozen=True)
class ListingEvent:
    """Represents a listing history event such as listing, update, or status change."""

    event_date: date
    listing_status: str
    asking_price: float | None
    source_name: str
    source_url: str | None
    confidence: str
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dictionary representation."""
        payload = asdict(self)
        payload["event_date"] = self.event_date.isoformat()
        return payload


@dataclass(frozen=True)
class PriceReduction:
    """Represents one detected price reduction event in listing history."""

    reduction_date: date
    old_price: float
    new_price: float
    reduction_amount: float
    reduction_percentage: float
    source_name: str
    source_url: str | None
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dictionary representation."""
        payload = asdict(self)
        payload["reduction_date"] = self.reduction_date.isoformat()
        return payload


class PropertyHistoryProvider(Protocol):
    """Provider contract for property history data sources.

    Real implementations can later query Kadaster, Funda history, auction sites,
    or commercial datasets without changing the public service interface.
    """

    def get_sale_history(self, normalized_address: str) -> list[SaleRecord]:
        """Return all known sale history records for the normalized address."""

    def get_listing_history(self, normalized_address: str) -> list[ListingEvent]:
        """Return all known listing history records for the normalized address."""


class MockPropertyHistoryProvider:
    """Mock provider with realistic sample data for local development.

    This provider never claims factual correctness and marks confidence as low/medium.
    """

    def __init__(self) -> None:
        self._sales: dict[str, list[SaleRecord]] = {
            "keizersgracht 123 amsterdam": [
                SaleRecord(
                    transaction_date=date(2017, 4, 21),
                    transaction_type="sale",
                    transaction_price=635000.0,
                    source_name="Mock Kadaster-like dataset",
                    source_url=None,
                    confidence="medium",
                    notes="Mock record for integration testing.",
                ),
                SaleRecord(
                    transaction_date=date(2012, 9, 14),
                    transaction_type="sale",
                    transaction_price=420000.0,
                    source_name="Mock archival transaction dataset",
                    source_url=None,
                    confidence="low",
                    notes="Older mock transfer; verify against official registry in production.",
                ),
            ],
            "coolsingel 50 rotterdam": [
                SaleRecord(
                    transaction_date=date(2019, 11, 7),
                    transaction_type="transfer",
                    transaction_price=None,
                    source_name="Mock corporate transfer feed",
                    source_url=None,
                    confidence="low",
                    notes="Price unavailable in source-like mock data.",
                )
            ],
        }

        self._listings: dict[str, list[ListingEvent]] = {
            "keizersgracht 123 amsterdam": [
                ListingEvent(
                    event_date=date(2025, 1, 10),
                    listing_status="active",
                    asking_price=795000.0,
                    source_name="Mock Funda history",
                    source_url=None,
                    confidence="medium",
                    notes="Initial listing snapshot.",
                ),
                ListingEvent(
                    event_date=date(2025, 3, 18),
                    listing_status="active",
                    asking_price=775000.0,
                    source_name="Mock Funda history",
                    source_url=None,
                    confidence="medium",
                    notes="Price updated.",
                ),
                ListingEvent(
                    event_date=date(2025, 5, 2),
                    listing_status="under_offer",
                    asking_price=765000.0,
                    source_name="Mock Funda history",
                    source_url=None,
                    confidence="medium",
                    notes="Object under offer.",
                ),
            ],
            "coolsingel 50 rotterdam": [
                ListingEvent(
                    event_date=date(2025, 2, 1),
                    listing_status="active",
                    asking_price=None,
                    source_name="Mock broker publication archive",
                    source_url=None,
                    confidence="low",
                    notes="Price on request in source-like mock data.",
                )
            ],
        }

    def get_sale_history(self, normalized_address: str) -> list[SaleRecord]:
        return list(self._sales.get(normalized_address, []))

    def get_listing_history(self, normalized_address: str) -> list[ListingEvent]:
        return list(self._listings.get(normalized_address, []))


class PropertyHistoryService:
    """Service for retrieving property sale and listing history.

    Public methods always return structured dictionaries and never fabricate facts.
    Unknown data is represented as None, empty lists, or explicit warnings.
    """

    def __init__(self, provider: PropertyHistoryProvider | None = None) -> None:
        self._provider = provider or MockPropertyHistoryProvider()

    def get_last_sale(self, address: str) -> dict[str, Any]:
        """Return the most recent sale/transfer entry for a property address.

        Args:
            address: Human-readable property address.

        Returns:
            A structured dictionary with metadata and the last transaction if known.
            When no history is known, ``last_sale`` is None.
        """
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_input_response(address, "Invalid or empty address")

        records = self._sorted_sales(normalized)
        last_sale = records[0].to_dict() if records else None

        return self._base_response(
            address=address,
            data={"last_sale": last_sale},
            warnings=[] if records else ["No sale history available in current provider data."],
        )

    def get_sale_history(self, address: str) -> dict[str, Any]:
        """Return full known sale history for a property address.

        Args:
            address: Human-readable property address.

        Returns:
            A structured dictionary containing a list of sale-like transactions.
            Missing data is returned as an empty list.
        """
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_input_response(address, "Invalid or empty address")

        records = self._sorted_sales(normalized)
        return self._base_response(
            address=address,
            data={
                "transactions": [record.to_dict() for record in records],
                "count": len(records),
            },
            warnings=[] if records else ["No sale history available in current provider data."],
        )

    def get_days_on_market(self, address: str) -> dict[str, Any]:
        """Return days-on-market metrics based on listing history.

        Args:
            address: Human-readable property address.

        Returns:
            A structured dictionary with listed_since, latest_event_date and
            days_on_market. If listing history is missing, days_on_market is None.
        """
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_input_response(address, "Invalid or empty address")

        listing_history = self._sorted_listings(normalized, reverse=False)
        if not listing_history:
            return self._base_response(
                address=address,
                data={
                    "listed_since": None,
                    "latest_event_date": None,
                    "days_on_market": None,
                },
                warnings=["No listing history available in current provider data."],
            )

        listed_since = listing_history[0].event_date
        latest_event = listing_history[-1].event_date
        days_on_market = (latest_event - listed_since).days if latest_event >= listed_since else None

        return self._base_response(
            address=address,
            data={
                "listed_since": listed_since.isoformat(),
                "latest_event_date": latest_event.isoformat(),
                "days_on_market": days_on_market,
            },
            warnings=[],
        )

    def get_price_reductions(self, address: str) -> dict[str, Any]:
        """Return detected price reduction events from listing history.

        Args:
            address: Human-readable property address.

        Returns:
            A structured dictionary with detected reductions and aggregate totals.
            If no comparable price snapshots exist, returns an empty list and None totals.
        """
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_input_response(address, "Invalid or empty address")

        listing_history = self._sorted_listings(normalized, reverse=False)
        reductions: list[PriceReduction] = []

        previous_price: float | None = None
        for item in listing_history:
            if item.asking_price is None:
                continue
            if previous_price is not None and item.asking_price < previous_price:
                amount = round(previous_price - item.asking_price, 2)
                percentage = round((amount / previous_price) * 100, 2) if previous_price > 0 else 0.0
                reductions.append(
                    PriceReduction(
                        reduction_date=item.event_date,
                        old_price=previous_price,
                        new_price=item.asking_price,
                        reduction_amount=amount,
                        reduction_percentage=percentage,
                        source_name=item.source_name,
                        source_url=item.source_url,
                        confidence=item.confidence,
                    )
                )
            previous_price = item.asking_price

        total_amount = round(sum(reduction.reduction_amount for reduction in reductions), 2) if reductions else None

        return self._base_response(
            address=address,
            data={
                "reductions": [reduction.to_dict() for reduction in reductions],
                "reduction_count": len(reductions),
                "total_reduction_amount": total_amount,
            },
            warnings=[] if listing_history else ["No listing history available in current provider data."],
        )

    def get_listing_history(self, address: str) -> dict[str, Any]:
        """Return listing status/price snapshots for a property address.

        Args:
            address: Human-readable property address.

        Returns:
            A structured dictionary containing chronologically sorted listing events.
            Missing history is represented by an empty list.
        """
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_input_response(address, "Invalid or empty address")

        records = self._sorted_listings(normalized, reverse=False)
        return self._base_response(
            address=address,
            data={
                "events": [record.to_dict() for record in records],
                "count": len(records),
            },
            warnings=[] if records else ["No listing history available in current provider data."],
        )

    def _normalize_address(self, address: str) -> str | None:
        if not isinstance(address, str) or not address.strip():
            return None
        return " ".join(address.lower().split())

    def _sorted_sales(self, normalized_address: str) -> list[SaleRecord]:
        return sorted(
            self._provider.get_sale_history(normalized_address),
            key=lambda item: item.transaction_date,
            reverse=True,
        )

    def _sorted_listings(self, normalized_address: str, reverse: bool) -> list[ListingEvent]:
        return sorted(
            self._provider.get_listing_history(normalized_address),
            key=lambda item: item.event_date,
            reverse=reverse,
        )

    def _base_response(self, address: str, data: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
        return {
            "address": address,
            "provider": self._provider.__class__.__name__,
            "is_mock": True,
            "confidence": "low_to_medium",
            "warnings": [
                "Mock data only; verify with real providers before using as factual evidence.",
                *warnings,
            ],
            "data": data,
        }

    def _invalid_input_response(self, address: str, message: str) -> dict[str, Any]:
        return {
            "address": address,
            "provider": self._provider.__class__.__name__,
            "is_mock": True,
            "confidence": "unknown",
            "warnings": [
                "Mock data only; verify with real providers before using as factual evidence.",
                message,
            ],
            "data": None,
        }
