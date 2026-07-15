from __future__ import annotations

from typing import Any
import requests

from deal_finder.models import NormalizedListing
from deal_finder.sources.base import ListingSourceAdapter, SourceRecordResult


class GenericFeedAdapter(ListingSourceAdapter):
    source_name = "generic_feed"
    source_type = "json_feed"

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        if not isinstance(configuration, dict):
            return False, ["Configuration must be an object."]

        warnings: list[str] = []
        feed_url = str(configuration.get("feed_url") or "").strip()
        if not feed_url:
            warnings.append("Missing required configuration: feed_url")
        elif not feed_url.startswith(("http://", "https://")):
            warnings.append("feed_url must start with http:// or https://")

        field_mapping = configuration.get("field_mapping")
        if field_mapping is not None and not isinstance(field_mapping, dict):
            warnings.append("field_mapping must be an object when provided")

        return len(warnings) == 0, warnings

    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        ok, warnings = self.validate_configuration(configuration)
        if not ok:
            raise ValueError("; ".join(warnings) or "Invalid configuration")

        feed_url = str(configuration.get("feed_url") or "").strip()
        timeout_seconds = _to_timeout(configuration.get("timeout_seconds"), default=8)

        response = requests.get(
            feed_url,
            headers={"Accept": "application/json", "User-Agent": "PropertyHunter-GenericFeed/1.0"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()

        payload = response.json()
        return _extract_feed_items(payload)

    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        return dict(listing_ref or {})

    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        if not isinstance(payload, dict):
            raise ValueError("Listing payload must be an object")

        source_name = str(payload.get("source_name") or self.source_name).strip() or self.source_name
        source_url = _to_str(payload.get("source_url"))
        if not source_url:
            raise ValueError("Missing required source_url field")

        listing_status = _to_str(payload.get("listing_status")) or _to_str(payload.get("status")) or "active"

        preserved_payload = dict(payload)
        preserved_payload["status"] = _to_str(payload.get("status")) or listing_status
        preserved_payload["listed_since"] = _to_str(payload.get("listed_since"))
        preserved_payload["image_urls"] = _to_str_list(payload.get("image_urls"))

        return NormalizedListing(
            source_name=source_name,
            external_listing_id=_to_str(payload.get("external_listing_id")),
            source_url=source_url,
            title=_to_str(payload.get("title")),
            address=_to_str(payload.get("address")),
            city=_to_str(payload.get("city")),
            asking_price=_to_float(payload.get("asking_price")),
            surface_m2=_to_float(payload.get("surface_m2")),
            property_type=_to_str(payload.get("property_type")),
            description=_to_str(payload.get("description")),
            listing_status=listing_status,
            raw_payload=preserved_payload,
        )

    def load_and_normalize_listings(self, configuration: dict[str, Any]) -> list[SourceRecordResult]:
        items = self.discover_listings(configuration)
        mapped_items: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                mapped_items.append(self._map_feed_item(item, configuration))
            else:
                mapped_items.append({"_adapter_record_error": "Record is not an object"})

        return self.normalize_records(mapped_items)

    def _map_feed_item(self, item: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        field_mapping = configuration.get("field_mapping") if isinstance(configuration, dict) else None
        mapping = field_mapping if isinstance(field_mapping, dict) else {}

        source_name = str(configuration.get("source_name") or self.source_name).strip() if isinstance(configuration, dict) else self.source_name
        if not source_name:
            source_name = self.source_name

        mapped = {
            "source_name": source_name,
            "external_listing_id": _first_value(item, mapping.get("external_listing_id"), ["external_listing_id", "external_id", "id", "listing_id"]),
            "source_url": _first_value(item, mapping.get("source_url"), ["source_url", "url", "link", "listing_url"]),
            "title": _first_value(item, mapping.get("title"), ["title", "name", "headline"]),
            "address": _first_value(item, mapping.get("address"), ["address", "street", "location"]),
            "city": _first_value(item, mapping.get("city"), ["city", "town", "municipality"]),
            "asking_price": _first_value(item, mapping.get("asking_price"), ["asking_price", "price", "vraagprijs"]),
            "surface_m2": _first_value(item, mapping.get("surface_m2"), ["surface_m2", "area_m2", "living_area", "size"]),
            "property_type": _first_value(item, mapping.get("property_type"), ["property_type", "type", "category"]),
            "listing_status": _first_value(item, mapping.get("listing_status"), ["listing_status", "status"]),
            "status": _first_value(item, mapping.get("status"), ["status", "listing_status"]),
            "listed_since": _first_value(item, mapping.get("listed_since"), ["listed_since", "listed_at", "publish_date", "date_listed"]),
            "description": _first_value(item, mapping.get("description"), ["description", "summary", "body"]),
            "image_urls": _coerce_image_urls(_first_value(item, mapping.get("image_urls"), ["image_urls", "images", "photos"])),
        }

        merged_payload = dict(item)
        merged_payload.update({k: v for k, v in mapped.items() if v is not None})
        return merged_payload


def _extract_feed_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("items", "results", "listings", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first_value(item: dict[str, Any], mapped_key: Any, fallback_keys: list[str]) -> Any:
    if isinstance(mapped_key, str) and mapped_key in item:
        return item.get(mapped_key)

    if isinstance(mapped_key, list):
        for key in mapped_key:
            if isinstance(key, str) and key in item:
                return item.get(key)

    for key in fallback_keys:
        if key in item:
            return item.get(key)
    return None


def _to_timeout(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(60, parsed))


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _coerce_image_urls(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
