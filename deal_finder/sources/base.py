from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable

from urllib.parse import urlparse

from deal_finder.models import NormalizedListing
from models.property import Property


@dataclass
class SourceAdapterInfo:
    source_name: str
    source_type: str = "unknown"
    is_enabled: bool = True


@dataclass
class SourceRecordResult:
    record_index: int
    success: bool
    listing: NormalizedListing | None = None
    error: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(ABC):
    source_name: str = "unknown"
    source_type: str = "unknown"
    is_enabled: bool = True

    @abstractmethod
    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        pass

    @abstractmethod
    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        pass

    @abstractmethod
    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        pass

    def to_property_model(self, payload: dict[str, Any]) -> Property:
        listing = self.normalize_listing(payload)
        raw_payload = dict(listing.raw_payload or {}) if isinstance(listing.raw_payload, dict) else {}
        merged_payload = {**raw_payload, **(payload if isinstance(payload, dict) else {})}
        merged_payload.setdefault("source_url", listing.source_url)
        merged_payload.setdefault("title", listing.title)
        merged_payload.setdefault("address", listing.address)
        merged_payload.setdefault("city", listing.city)
        merged_payload.setdefault("asking_price", listing.asking_price)
        merged_payload.setdefault("surface_m2", listing.surface_m2)
        merged_payload.setdefault("property_type", listing.property_type)
        merged_payload.setdefault("description", listing.description)
        merged_payload.setdefault("listing_status", listing.listing_status)

        photos = merged_payload.get("photos") or merged_payload.get("image_urls") or []
        if not isinstance(photos, list):
            photos = [photos] if photos else []

        previous_transactions = merged_payload.get("previous_transactions") or []
        permits_last_10_years = merged_payload.get("permits_last_10_years") or []
        active_permits = merged_payload.get("active_permits") or []

        return Property(
            source_url=listing.source_url,
            title=listing.title,
            address=listing.address,
            city=listing.city,
            country=_as_optional_str(merged_payload.get("country")),
            asking_price=listing.asking_price,
            asking_price_status="known" if listing.asking_price is not None else "unknown",
            asking_price_text=_as_optional_str(merged_payload.get("asking_price_text")),
            postal_code=_as_optional_str(merged_payload.get("postal_code")),
            municipality=_as_optional_str(merged_payload.get("municipality")),
            bag_id=_as_optional_str(merged_payload.get("bag_id")),
            bag_nummeraanduiding_id=_as_optional_str(merged_payload.get("bag_nummeraanduiding_id")),
            bag_pand_id=_as_optional_str(merged_payload.get("bag_pand_id")),
            bag_building_year=_to_int(merged_payload.get("bag_building_year")),
            bag_usage_purpose=_as_optional_str(merged_payload.get("bag_usage_purpose")),
            bag_official_floor_area_m2=_to_float(merged_payload.get("bag_official_floor_area_m2")),
            bag_coordinates_rd=merged_payload.get("bag_coordinates_rd"),
            bag_coordinates_ll=merged_payload.get("bag_coordinates_ll"),
            bag_postcode=_as_optional_str(merged_payload.get("bag_postcode")),
            bag_municipality=_as_optional_str(merged_payload.get("bag_municipality")),
            woz_object_number=_to_int(merged_payload.get("woz_object_number")),
            latest_woz_value=_to_float(merged_payload.get("latest_woz_value")),
            woz_valuation_year=_to_int(merged_payload.get("woz_valuation_year")),
            woz_historical_values=merged_payload.get("woz_historical_values") or [],
            neighborhood_m2_price_average=_to_float(merged_payload.get("neighborhood_m2_price_average")),
            street_m2_price_average=_to_float(merged_payload.get("street_m2_price_average")),
            listed_since=merged_payload.get("listed_since"),
            days_on_market=_to_int(merged_payload.get("days_on_market")),
            listing_status=listing.listing_status,
            original_asking_price=_to_float(merged_payload.get("original_asking_price")),
            current_asking_price=_to_float(merged_payload.get("current_asking_price")),
            price_reduction_count=_to_int(merged_payload.get("price_reduction_count")) or 0,
            last_price_reduction_date=merged_payload.get("last_price_reduction_date"),
            total_price_reduction_amount=_to_float(merged_payload.get("total_price_reduction_amount")),
            total_price_reduction_percentage=_to_float(merged_payload.get("total_price_reduction_percentage")),
            listing_history_source=_as_optional_str(merged_payload.get("listing_history_source")),
            listing_history_confidence=_as_optional_str(merged_payload.get("listing_history_confidence")) or "unknown",
            surface_m2=listing.surface_m2,
            plot_size_m2=_to_float(merged_payload.get("plot_size_m2")),
            price_per_m2=_to_float(merged_payload.get("price_per_m2")),
            bedrooms=_to_int(merged_payload.get("bedrooms")),
            annual_rent=_to_float(merged_payload.get("annual_rent")),
            property_type=listing.property_type,
            construction_year=_to_int(merged_payload.get("construction_year")),
            broker=_as_optional_str(merged_payload.get("broker")),
            photos=[str(item).strip() for item in photos if str(item).strip()],
            listing_id=_as_optional_str(merged_payload.get("listing_id")) or listing.external_listing_id,
            scraped_at=merged_payload.get("scraped_at"),
            source_timestamp=_as_optional_str(merged_payload.get("source_timestamp")),
            external_listing_id=listing.external_listing_id,
            current_use=_as_optional_str(merged_payload.get("current_use")),
            zoning=_as_optional_str(merged_payload.get("zoning")),
            energy_label=_as_optional_str(merged_payload.get("energy_label")),
            description=listing.description,
            raw_text=_as_optional_str(merged_payload.get("raw_text")) or listing.description,
            previous_transactions=previous_transactions,
            permits_last_10_years=permits_last_10_years,
            active_permits=active_permits,
        )

    def get_source_info(self) -> SourceAdapterInfo:
        return SourceAdapterInfo(
            source_name=str(getattr(self, "source_name", "unknown") or "unknown"),
            source_type=str(getattr(self, "source_type", "unknown") or "unknown"),
            is_enabled=bool(getattr(self, "is_enabled", True)),
        )

    def get_last_fetch_stats(self) -> dict[str, int]:
        raw_stats = getattr(self, "_last_fetch_stats", {})
        if not isinstance(raw_stats, dict):
            return {
                "listings_found": 0,
                "listings_imported": 0,
                "duplicates_skipped": 0,
                "failed_listings": 0,
            }

        return {
            "listings_found": int(raw_stats.get("listings_found") or raw_stats.get("records_found") or 0),
            "listings_imported": int(raw_stats.get("listings_imported") or raw_stats.get("records_imported") or 0),
            "duplicates_skipped": int(raw_stats.get("duplicates_skipped") or raw_stats.get("records_skipped") or 0),
            "failed_listings": int(raw_stats.get("failed_listings") or raw_stats.get("records_failed") or 0),
        }

    def load_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        # Backward-compatible alias for discover_listings used by source-specific adapters.
        return self.discover_listings(configuration)

    def load_and_normalize_listings(self, configuration: dict[str, Any]) -> list[SourceRecordResult]:
        return self.normalize_records(self.discover_listings(configuration))

    def extract_external_listing_id(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("external_listing_id")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def extract_source_url(self, payload: dict[str, Any]) -> str:
        value = payload.get("source_url")
        return str(value or "").strip()

    def ensure_stable_identity(self, payload: dict[str, Any]) -> tuple[str | None, str]:
        external_id = self.extract_external_listing_id(payload)
        source_url = self.extract_source_url(payload)
        if external_id:
            return external_id, source_url

        parsed = urlparse(source_url)
        if not parsed.scheme or not parsed.netloc:
            return None, source_url

        path = (parsed.path or "").rstrip("/")
        stable_url = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
        return stable_url or None, source_url

    def normalize_records(self, payloads: Iterable[dict[str, Any]]) -> list[SourceRecordResult]:
        results: list[SourceRecordResult] = []
        for record_index, payload in enumerate(payloads, start=1):
            safe_payload = payload if isinstance(payload, dict) else {}
            try:
                listing = self.normalize_listing(safe_payload)
                stable_external_id, source_url = self.ensure_stable_identity(safe_payload)
                if stable_external_id and not listing.external_listing_id:
                    listing.external_listing_id = stable_external_id
                if source_url and not listing.source_url:
                    listing.source_url = source_url

                if not listing.source_url:
                    raise ValueError("Missing required source_url")

                results.append(
                    SourceRecordResult(
                        record_index=record_index,
                        success=True,
                        listing=listing,
                        error=None,
                        payload=safe_payload,
                    )
                )
            except Exception as error:
                results.append(
                    SourceRecordResult(
                        record_index=record_index,
                        success=False,
                        listing=None,
                        error=f"{type(error).__name__}: {error}",
                        payload=safe_payload,
                    )
                )
        return results


ListingSourceAdapter = SourceAdapter


class EmptySourceAdapter(SourceAdapter):
    default_start_url: str = ""

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        return True, []

    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        return dict(listing_ref or {})

    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        source_url = _as_optional_str(payload.get("source_url") or payload.get("url") or payload.get("link"))
        if not source_url:
            raise ValueError("Missing required source_url")

        source_name = _as_optional_str(payload.get("source_name")) or self.source_name
        listing_id = _as_optional_str(payload.get("external_listing_id") or payload.get("listing_id"))

        return NormalizedListing(
            source_name=source_name,
            external_listing_id=listing_id,
            source_url=source_url,
            title=_as_optional_str(payload.get("title")),
            address=_as_optional_str(payload.get("address")),
            city=_as_optional_str(payload.get("city")),
            asking_price=_to_float(payload.get("asking_price")),
            surface_m2=_to_float(payload.get("surface_m2") or payload.get("living_area")),
            property_type=_as_optional_str(payload.get("property_type")),
            description=_as_optional_str(payload.get("description")),
            listing_status=_as_optional_str(payload.get("listing_status")) or "active",
            raw_payload=dict(payload),
        )


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
