from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from models.property import Property


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _normalize_status(value: Any) -> str:
    text = " ".join(str(value or "").lower().split())
    if text in {"sold_subject_to_contract", "under contract", "under offer"}:
        return "under_offer"
    if text in {"sold", "auction"}:
        return "sold"
    if text in {"withdrawn", "revoked", "lapsed"}:
        return "withdrawn"
    if text == "under_offer":
        return "under_offer"
    return "active" if text in {"", "unknown", "active"} else text


def _snapshot_price(snapshot: dict[str, Any]) -> float | None:
    return _safe_float(snapshot.get("asking_price"))


def _snapshot_date(snapshot: dict[str, Any]) -> date | None:
    return _parse_date(
        snapshot.get("observed_at")
        or snapshot.get("created_at")
        or snapshot.get("first_seen_at")
        or snapshot.get("latest_seen_at")
    )


@dataclass(frozen=True)
class ListingHistoryPricePoint:
    observed_date: date
    asking_price: float | None
    listing_status: str
    source: str = "listing_snapshots"
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_date": self.observed_date.isoformat(),
            "asking_price": self.asking_price,
            "listing_status": self.listing_status,
            "source": self.source,
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class ListingHistoryResult:
    first_seen_date: date | None
    latest_seen_date: date | None
    days_on_market: int | None
    original_asking_price: float | None
    current_asking_price: float | None
    total_price_reduction: float | None
    total_price_reduction_percentage: float | None
    number_of_price_changes: int
    reduction_frequency: float | None
    listing_status: str
    price_history: list[ListingHistoryPricePoint] = field(default_factory=list)
    recently_relisted: bool = False
    relisted_date: date | None = None
    source: str = "listing_snapshots"
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_seen_date": self.first_seen_date.isoformat() if self.first_seen_date else None,
            "latest_seen_date": self.latest_seen_date.isoformat() if self.latest_seen_date else None,
            "days_on_market": self.days_on_market,
            "original_asking_price": self.original_asking_price,
            "current_asking_price": self.current_asking_price,
            "total_price_reduction": self.total_price_reduction,
            "total_price_reduction_percentage": self.total_price_reduction_percentage,
            "number_of_price_changes": self.number_of_price_changes,
            "reduction_frequency": self.reduction_frequency,
            "listing_status": self.listing_status,
            "price_history": [point.to_dict() for point in self.price_history],
            "recently_relisted": self.recently_relisted,
            "relisted_date": self.relisted_date.isoformat() if self.relisted_date else None,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def empty(cls, listing_status: str = "unknown") -> "ListingHistoryResult":
        return cls(
            first_seen_date=None,
            latest_seen_date=None,
            days_on_market=None,
            original_asking_price=None,
            current_asking_price=None,
            total_price_reduction=None,
            total_price_reduction_percentage=None,
            number_of_price_changes=0,
            reduction_frequency=None,
            listing_status=listing_status,
            price_history=[],
        )


class ListingHistoryEngine:
    def __init__(self, recent_relist_window_days: int = 180) -> None:
        self.recent_relist_window_days = max(1, int(recent_relist_window_days))

    def build(self, listing: Property | dict[str, Any], snapshots: Iterable[dict[str, Any]]) -> ListingHistoryResult:
        snapshot_list = [dict(item) for item in snapshots if isinstance(item, dict)]
        snapshot_list.sort(key=lambda item: _snapshot_date(item) or date.min)

        if not snapshot_list:
            listing_status = _normalize_status(self._get_value(listing, "listing_status") or "unknown")
            return ListingHistoryResult.empty(listing_status=listing_status)

        price_history: list[ListingHistoryPricePoint] = []
        price_changes = 0
        previous_price: float | None = None
        first_seen_date: date | None = None
        latest_seen_date: date | None = None
        status_dates: list[tuple[date, str]] = []

        for snapshot in snapshot_list:
            observed_date = _snapshot_date(snapshot)
            if observed_date is None:
                continue

            if first_seen_date is None:
                first_seen_date = observed_date
            latest_seen_date = observed_date

            asking_price = _snapshot_price(snapshot)
            listing_status = _normalize_status(snapshot.get("listing_status") or self._get_value(listing, "listing_status") or "active")
            price_history.append(
                ListingHistoryPricePoint(
                    observed_date=observed_date,
                    asking_price=asking_price,
                    listing_status=listing_status,
                    source="listing_snapshots",
                    source_url=self._get_value(listing, "source_url"),
                )
            )
            status_dates.append((observed_date, listing_status))

            if asking_price is not None and previous_price is not None and asking_price != previous_price:
                price_changes += 1
            if asking_price is not None:
                previous_price = asking_price

        original_asking_price = next(
            (point.asking_price for point in price_history if point.asking_price is not None),
            _safe_float(self._get_value(listing, "original_asking_price")),
        )
        current_asking_price = next(
            (point.asking_price for point in reversed(price_history) if point.asking_price is not None),
            _safe_float(self._get_value(listing, "current_asking_price")),
        )

        if first_seen_date is None:
            first_seen_date = _parse_date(self._get_value(listing, "first_seen_date") or self._get_value(listing, "first_seen_at"))
        if latest_seen_date is None:
            latest_seen_date = _parse_date(self._get_value(listing, "latest_seen_date") or self._get_value(listing, "last_seen_at"))

        if first_seen_date and latest_seen_date and latest_seen_date >= first_seen_date:
            days_on_market = (latest_seen_date - first_seen_date).days
        else:
            days_on_market = None

        total_price_reduction = None
        total_price_reduction_percentage = None
        if original_asking_price is not None and current_asking_price is not None:
            reduction = round(max(0.0, original_asking_price - current_asking_price), 2)
            total_price_reduction = reduction if reduction > 0 else 0.0
            if original_asking_price > 0:
                total_price_reduction_percentage = round((total_price_reduction / original_asking_price) * 100.0, 2)

        reduction_frequency = None
        if days_on_market is not None and days_on_market >= 0:
            reduction_frequency = round((price_changes / max(days_on_market, 1)) * 30.0, 2)

        listing_status = _normalize_status(self._get_value(listing, "listing_status") or (price_history[-1].listing_status if price_history else "active"))
        recently_relisted, relisted_date = self._detect_relisting(status_dates)

        return ListingHistoryResult(
            first_seen_date=first_seen_date,
            latest_seen_date=latest_seen_date,
            days_on_market=days_on_market,
            original_asking_price=original_asking_price,
            current_asking_price=current_asking_price,
            total_price_reduction=total_price_reduction,
            total_price_reduction_percentage=total_price_reduction_percentage,
            number_of_price_changes=price_changes,
            reduction_frequency=reduction_frequency,
            listing_status=listing_status,
            price_history=price_history,
            recently_relisted=recently_relisted,
            relisted_date=relisted_date,
        )

    def _detect_relisting(self, status_dates: list[tuple[date, str]]) -> tuple[bool, date | None]:
        if len(status_dates) < 2:
            return False, None

        latest_date, latest_status = status_dates[-1]
        if latest_status != "active":
            return False, None

        for observed_date, status in reversed(status_dates[:-1]):
            if status in {"withdrawn", "sold"}:
                if (latest_date - observed_date).days <= self.recent_relist_window_days:
                    return True, latest_date
                break
        return False, None

    def _get_value(self, listing: Property | dict[str, Any], key: str) -> Any:
        if isinstance(listing, Property):
            return getattr(listing, key, None)
        if isinstance(listing, dict):
            return listing.get(key)
        return None
