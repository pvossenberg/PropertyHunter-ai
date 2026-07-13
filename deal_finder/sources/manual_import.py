from __future__ import annotations

import csv
import io
import json
from typing import Any
from urllib.parse import urlparse

from deal_finder.models import NormalizedListing
from deal_finder.sources.base import ListingSourceAdapter


class ManualImportAdapter(ListingSourceAdapter):
    source_name = "manual_import"

    REQUIRED_FIELDS = {"source_name", "source_url"}

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        if not isinstance(configuration, dict):
            return False, ["Configuration must be an object."]
        return True, []

    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        return dict(listing_ref or {})

    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        source_url = str(payload.get("source_url") or "").strip()
        source_name = str(payload.get("source_name") or self.source_name).strip() or self.source_name
        return NormalizedListing(
            source_name=source_name,
            external_listing_id=_as_optional_str(payload.get("external_listing_id")),
            source_url=source_url,
            title=_as_optional_str(payload.get("title")),
            address=_as_optional_str(payload.get("address")),
            city=_as_optional_str(payload.get("city")),
            asking_price=_to_float(payload.get("asking_price")),
            surface_m2=_to_float(payload.get("surface_m2")),
            property_type=_as_optional_str(payload.get("property_type")),
            description=_as_optional_str(payload.get("description")),
            listing_status=_as_optional_str(payload.get("listing_status")) or "active",
            raw_payload=dict(payload),
        )

    def import_csv(self, csv_text: str) -> tuple[list[NormalizedListing], list[str]]:
        if not isinstance(csv_text, str) or not csv_text.strip():
            return [], ["CSV input is empty."]

        warnings: list[str] = []
        rows: list[NormalizedListing] = []
        reader = csv.DictReader(io.StringIO(csv_text))
        if not reader.fieldnames:
            return [], ["CSV has no headers."]

        for index, raw in enumerate(reader, start=2):
            payload = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in (raw or {}).items() if isinstance(k, str)}
            missing = [field for field in self.REQUIRED_FIELDS if not payload.get(field)]
            if missing:
                warnings.append(f"Row {index}: missing required fields: {', '.join(sorted(missing))}")
                continue
            rows.append(self.normalize_listing(payload))

        return rows, warnings

    def import_json(self, json_text: str) -> tuple[list[NormalizedListing], list[str]]:
        if not isinstance(json_text, str) or not json_text.strip():
            return [], ["JSON input is empty."]

        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            return [], [f"Invalid JSON: {exc.msg}"]

        raw_items: list[dict[str, Any]]
        if isinstance(payload, dict):
            raw_items = [payload]
        elif isinstance(payload, list):
            raw_items = [item for item in payload if isinstance(item, dict)]
        else:
            return [], ["JSON must be an object or list of objects."]

        warnings: list[str] = []
        listings: list[NormalizedListing] = []
        for index, item in enumerate(raw_items, start=1):
            missing = [field for field in self.REQUIRED_FIELDS if not item.get(field)]
            if missing:
                warnings.append(f"Item {index}: missing required fields: {', '.join(sorted(missing))}")
                continue
            listings.append(self.normalize_listing(item))

        return listings, warnings

    def import_urls(self, urls_text: str, source_name: str = "manual_url") -> tuple[list[NormalizedListing], list[str]]:
        if not isinstance(urls_text, str) or not urls_text.strip():
            return [], ["URL input is empty."]

        warnings: list[str] = []
        listings: list[NormalizedListing] = []
        for index, line in enumerate(urls_text.splitlines(), start=1):
            url = line.strip()
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                warnings.append(f"Line {index}: invalid URL '{url}'.")
                continue
            listings.append(
                NormalizedListing(
                    source_name=source_name,
                    source_url=url,
                    external_listing_id=None,
                    title=None,
                    address=None,
                    city=None,
                    asking_price=None,
                    surface_m2=None,
                    property_type=None,
                    description=None,
                    raw_payload={"source_name": source_name, "source_url": url},
                )
            )

        return listings, warnings


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
