from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .permit import PermitRecord
from .transaction import PropertyTransaction


LISTING_STATUSES = {"active", "under_offer", "sold_subject_to_contract", "withdrawn", "auction", "sold", "unknown"}


@dataclass
class Property:
    source_url: Optional[str] = None
    title: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    asking_price: Optional[float] = None
    asking_price_status: str = "unknown"
    asking_price_text: Optional[str] = None
    listed_since: Optional[date] = None
    days_on_market: Optional[int] = None
    listing_status: str = "unknown"
    original_asking_price: Optional[float] = None
    current_asking_price: Optional[float] = None
    price_reduction_count: int = 0
    last_price_reduction_date: Optional[date] = None
    total_price_reduction_amount: Optional[float] = None
    total_price_reduction_percentage: Optional[float] = None
    listing_history_source: Optional[str] = None
    listing_history_confidence: str = "unknown"
    surface_m2: Optional[float] = None
    price_per_m2: Optional[float] = None
    annual_rent: Optional[float] = None
    property_type: Optional[str] = None
    current_use: Optional[str] = None
    zoning: Optional[str] = None
    energy_label: Optional[str] = None
    description: Optional[str] = None
    raw_text: Optional[str] = None
    previous_transactions: list[PropertyTransaction] = field(default_factory=list)
    permits_last_10_years: list[PermitRecord] = field(default_factory=list)
    active_permits: list[PermitRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.listing_status not in LISTING_STATUSES:
            self.listing_status = "unknown"
        if not isinstance(self.listing_history_confidence, str) or not self.listing_history_confidence.strip():
            self.listing_history_confidence = "unknown"

        self.previous_transactions = [PropertyTransaction.from_dict(item) for item in (self.previous_transactions or [])]
        self.permits_last_10_years = [PermitRecord.from_dict(item) for item in (self.permits_last_10_years or [])]
        self.active_permits = [PermitRecord.from_dict(item) for item in (self.active_permits or [])]
