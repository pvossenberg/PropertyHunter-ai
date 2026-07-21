from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from .permit import PermitRecord
from .transaction import PropertyTransaction


LISTING_STATUSES = {"active", "inactive", "under_offer", "sold_subject_to_contract", "withdrawn", "auction", "sold", "unknown"}


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
    postal_code: Optional[str] = None
    municipality: Optional[str] = None
    bag_id: Optional[str] = None
    bag_address_id: Optional[str] = None
    bag_verblijfsobject_id: Optional[str] = None
    bag_nummeraanduiding_id: Optional[str] = None
    bag_pand_id: Optional[str] = None
    bag_building_year: Optional[int] = None
    construction_year_bag: Optional[int] = None
    bag_usage_purpose: Optional[str] = None
    usage_purpose: Optional[str] = None
    bag_status: Optional[str] = None
    bag_official_floor_area_m2: Optional[float] = None
    official_floor_area_m2: Optional[float] = None
    bag_coordinates_rd: Optional[dict[str, float]] = None
    bag_coordinates_ll: Optional[dict[str, float]] = None
    bag_postcode: Optional[str] = None
    bag_municipality: Optional[str] = None
    bag_retrieval_date: Optional[str] = None
    bag_source: Optional[str] = None
    bag_confidence_score: Optional[int] = None
    bag_quality_flags: list[str] = field(default_factory=list)
    funda_living_area_m2: Optional[float] = None
    living_area_difference_m2: Optional[float] = None
    living_area_difference_percentage: Optional[float] = None
    calculation_area_m2: Optional[float] = None
    calculation_area_source: Optional[str] = None
    asking_price_per_m2: Optional[float] = None
    woz_value_per_m2: Optional[float] = None
    woz_object_number: Optional[int] = None
    latest_woz_value: Optional[float] = None
    woz_valuation_year: Optional[int] = None
    woz_historical_values: list[dict[str, Any]] = field(default_factory=list)
    neighborhood_m2_price_average: Optional[float] = None
    street_m2_price_average: Optional[float] = None
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
    first_seen_date: Optional[date] = None
    latest_seen_date: Optional[date] = None
    number_of_price_changes: Optional[int] = None
    reduction_frequency: Optional[float] = None
    recently_relisted: Optional[bool] = None
    relisted_date: Optional[date] = None
    price_history: list[dict[str, Any]] = field(default_factory=list)
    surface_m2: Optional[float] = None
    plot_size_m2: Optional[float] = None
    price_per_m2: Optional[float] = None
    bedrooms: Optional[int] = None
    annual_rent: Optional[float] = None
    property_type: Optional[str] = None
    construction_year: Optional[int] = None
    broker: Optional[str] = None
    photos: list[str] = field(default_factory=list)
    listing_id: Optional[str] = None
    scraped_at: Optional[datetime] = None
    source_timestamp: Optional[str] = None
    external_listing_id: Optional[str] = None
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
