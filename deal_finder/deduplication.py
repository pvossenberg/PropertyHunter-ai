from __future__ import annotations

import re
from urllib.parse import urlparse

from deal_finder.models import DeduplicationResult, NormalizedListing

_POSTCODE_RE = re.compile(r"\b([1-9][0-9]{3})\s*([A-Za-z]{2})\b")
_HOUSE_NUMBER_RE = re.compile(r"\b(\d+)([A-Za-z0-9\-]*)\b")


def normalize_url(url: str) -> str:
    if not isinstance(url, str):
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = re.sub(r"/+$", "", parsed.path or "")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return re.sub(r"[^a-z0-9\s-]", "", normalized)


def parse_address_components(address: str) -> dict[str, str | None]:
    clean = normalize_text(address)
    postcode = None
    house_number = None
    addition = None

    postcode_match = _POSTCODE_RE.search(address or "") if isinstance(address, str) else None
    if postcode_match:
        postcode = f"{postcode_match.group(1)}{postcode_match.group(2).upper()}"

    number_match = _HOUSE_NUMBER_RE.search(clean)
    if number_match:
        house_number = number_match.group(1)
        addition = number_match.group(2) or None

    return {
        "normalized_address": clean,
        "postcode": postcode,
        "house_number": house_number,
        "addition": addition,
    }


def match_listing(incoming: NormalizedListing, existing_records: list[dict]) -> DeduplicationResult:
    warnings: list[str] = []
    incoming_url = normalize_url(incoming.source_url)
    incoming_components = parse_address_components(incoming.address or "")

    for record in existing_records:
        source_name = str(record.get("source_name") or "")
        external_id = str(record.get("external_listing_id") or "")
        if incoming.external_listing_id and source_name == incoming.source_name and external_id == incoming.external_listing_id:
            return DeduplicationResult(
                matched_listing_id=str(record.get("id") or "") or None,
                matched_property_id=str(record.get("property_id") or "") or None,
                match_method="source_external_listing_id",
                confidence=1.0,
                warnings=warnings,
            )

    for record in existing_records:
        existing_url = normalize_url(str(record.get("source_url") or ""))
        if incoming_url and incoming_url == existing_url:
            return DeduplicationResult(
                matched_listing_id=str(record.get("id") or "") or None,
                matched_property_id=str(record.get("property_id") or "") or None,
                match_method="normalized_source_url",
                confidence=0.98,
                warnings=warnings,
            )

    if incoming_components.get("postcode") and incoming_components.get("house_number"):
        for record in existing_records:
            existing_components = parse_address_components(str(record.get("address") or ""))
            if (
                incoming_components.get("postcode") == existing_components.get("postcode")
                and incoming_components.get("house_number") == existing_components.get("house_number")
                and (incoming_components.get("addition") or "") == (existing_components.get("addition") or "")
            ):
                return DeduplicationResult(
                    matched_listing_id=str(record.get("id") or "") or None,
                    matched_property_id=str(record.get("property_id") or "") or None,
                    match_method="postcode_house_number",
                    confidence=0.95,
                    warnings=warnings,
                )

    incoming_address = normalize_text(incoming.address or "")
    if incoming_address:
        for record in existing_records:
            if incoming_address == normalize_text(str(record.get("address") or "")):
                return DeduplicationResult(
                    matched_listing_id=str(record.get("id") or "") or None,
                    matched_property_id=str(record.get("property_id") or "") or None,
                    match_method="normalized_address",
                    confidence=0.9,
                    warnings=warnings,
                )

    if incoming_address and incoming.surface_m2 is not None:
        for record in existing_records:
            existing_address = normalize_text(str(record.get("address") or ""))
            existing_surface = record.get("surface_m2")
            try:
                existing_surface_value = float(existing_surface) if existing_surface is not None else None
            except (TypeError, ValueError):
                existing_surface_value = None

            if existing_address and existing_address == incoming_address and existing_surface_value is not None:
                if abs(existing_surface_value - float(incoming.surface_m2)) <= 1.0:
                    warnings.append("Matched using lower-confidence address+surface fallback.")
                    return DeduplicationResult(
                        matched_listing_id=str(record.get("id") or "") or None,
                        matched_property_id=str(record.get("property_id") or "") or None,
                        match_method="address_surface_fallback",
                        confidence=0.75,
                        warnings=warnings,
                    )

    return DeduplicationResult(matched_listing_id=None, matched_property_id=None, match_method="none", confidence=0.0, warnings=warnings)
