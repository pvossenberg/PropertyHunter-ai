from __future__ import annotations

from typing import Any

from deal_finder.models import NormalizedListing
from deal_finder.sources.base import ListingSourceAdapter


class GenericFeedAdapter(ListingSourceAdapter):
    source_name = "generic_feed"

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        if not isinstance(configuration, dict):
            return False, ["Configuration must be an object."]
        return True, []

    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        # Placeholder adapter: no live network calls in foundation version.
        return []

    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        return dict(listing_ref or {})

    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        return NormalizedListing(
            source_name=str(payload.get("source_name") or self.source_name),
            external_listing_id=payload.get("external_listing_id"),
            source_url=str(payload.get("source_url") or "").strip(),
            title=payload.get("title"),
            address=payload.get("address"),
            city=payload.get("city"),
            asking_price=_to_float(payload.get("asking_price")),
            surface_m2=_to_float(payload.get("surface_m2")),
            property_type=payload.get("property_type"),
            description=payload.get("description"),
            listing_status=str(payload.get("listing_status") or "active"),
            raw_payload=dict(payload),
        )


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
