from __future__ import annotations

from datetime import date, datetime, timezone
import logging
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from deal_finder.models import NormalizedListing
from deal_finder.sources.base import ListingSourceAdapter, SourceRecordResult
from scrapers.base import extract_json_ld_blocks, extract_meta_content, find_first_jsonld_value, parse_price
from services.klusvastgoed_service import KlusvastgoedService, normalize_klusvastgoed_municipality_slug


LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_MAX_PAGES = 3
DEFAULT_SOURCE_MODE = "municipal"


class KlusvastgoedAdapter(ListingSourceAdapter):
    source_name = "klusvastgoed.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.klusvastgoed.nl/kluswoning-amsterdam"

    def __init__(self, service: KlusvastgoedService | None = None):
        self.service = service or KlusvastgoedService()
        self._last_fetch_stats = {
            "listings_found": 0,
            "listings_imported": 0,
            "duplicates_skipped": 0,
            "failed_listings": 0,
        }

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        if not isinstance(configuration, dict):
            return False, ["Configuration must be an object."]

        warnings: list[str] = []
        mode = str(configuration.get("mode") or DEFAULT_SOURCE_MODE).strip().lower()
        if mode not in {"municipal", "national"}:
            warnings.append("mode must be municipal or national")

        start_url = str(configuration.get("start_url") or self.default_start_url).strip()
        if mode != "national" and not start_url:
            warnings.append("start_url is required")
        elif start_url:
            parsed = urlparse(start_url)
            host = (parsed.hostname or "").lower()
            if not parsed.scheme.startswith("http") or not host:
                warnings.append("start_url must be a valid http(s) URL")
            elif "klusvastgoed.nl" not in host:
                warnings.append("start_url must point to klusvastgoed.nl")
            elif mode != "national" and "/kluswoning-" not in parsed.path:
                warnings.append("start_url must point to a municipality page like /kluswoning-amsterdam")

        try:
            if int(configuration.get("max_pages", DEFAULT_MAX_PAGES)) <= 0:
                warnings.append("max_pages must be > 0")
        except (TypeError, ValueError):
            warnings.append("max_pages must be an integer")

        try:
            if float(configuration.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)) <= 0:
                warnings.append("timeout_seconds must be > 0")
        except (TypeError, ValueError):
            warnings.append("timeout_seconds must be numeric")

        return len(warnings) == 0, warnings

    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        ok, warnings = self.validate_configuration(configuration)
        if not ok:
            raise ValueError("; ".join(warnings) or "Invalid configuration")

        mode = str(configuration.get("mode") or DEFAULT_SOURCE_MODE).strip().lower()
        start_url = str(configuration.get("start_url") or self.default_start_url).strip()
        max_pages = _to_int(configuration.get("max_pages"), default=DEFAULT_MAX_PAGES, minimum=1, maximum=25)
        timeout_seconds = _to_float(configuration.get("timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS, minimum=1.0, maximum=60.0)
        if mode == "national":
            return self.service.fetch_national_listing_cards(timeout_seconds=timeout_seconds, max_pages=max_pages)
        return self.service.fetch_listing_cards(start_url, timeout_seconds=timeout_seconds, max_pages=max_pages)

    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        source_url = str((listing_ref or {}).get("source_url") or "").strip()
        if not source_url:
            raise ValueError("Missing source_url")
        timeout_seconds = _to_float(configuration.get("timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS, minimum=1.0, maximum=60.0)
        mode = str(configuration.get("mode") or DEFAULT_SOURCE_MODE).strip().lower()
        referer = None
        if mode == "national":
            referer = self.service.build_city_index_url()
        else:
            referer = str(configuration.get("start_url") or self.default_start_url).strip() or None
        html = self.service.fetch_listing_detail_html(source_url, timeout_seconds=timeout_seconds, referer=referer)
        return self._extract_listing_record(source_url=source_url, html=html, listing_ref=listing_ref)

    def load_and_normalize_listings(self, configuration: dict[str, Any]) -> list[SourceRecordResult]:
        listing_refs = self.discover_listings(configuration)
        results: list[SourceRecordResult] = []
        imported = 0
        failed = 0
        cities = {str(ref.get("city") or "").strip() for ref in listing_refs if isinstance(ref, dict) and str(ref.get("city") or "").strip()}
        municipalities = {
            str(ref.get("municipality") or ref.get("city") or "").strip()
            for ref in listing_refs
            if isinstance(ref, dict) and str(ref.get("municipality") or ref.get("city") or "").strip()
        }
        provinces = {str(ref.get("province") or "").strip() for ref in listing_refs if isinstance(ref, dict) and str(ref.get("province") or "").strip()}

        for index, listing_ref in enumerate(listing_refs, start=1):
            try:
                payload = self.fetch_listing_details(listing_ref, configuration)
                listing = self.normalize_listing(payload)
                results.append(SourceRecordResult(record_index=index, success=True, listing=listing, payload=payload))
                imported += 1
            except Exception as error:
                failed += 1
                source_url = (listing_ref or {}).get("source_url") if isinstance(listing_ref, dict) else None
                LOGGER.warning("%s listing failed url=%s error=%s", self.source_name, source_url, error)
                results.append(
                    SourceRecordResult(
                        record_index=index,
                        success=False,
                        listing=None,
                        error=f"{type(error).__name__}: {error}",
                        payload=dict(listing_ref or {}),
                    )
                )

        self._last_fetch_stats = {
            "listings_found": len(listing_refs),
            "listings_imported": imported,
            "duplicates_skipped": max(0, len(listing_refs) - imported - failed),
            "failed_listings": failed,
            "cities_found": len(cities),
            "municipalities_found": len(municipalities),
            "provinces_found": len(provinces),
        }
        LOGGER.info(
            "%s fetch stats listings_found=%s listings_imported=%s duplicates_skipped=%s failed_listings=%s cities_found=%s municipalities_found=%s provinces_found=%s",
            self.source_name,
            self._last_fetch_stats["listings_found"],
            self._last_fetch_stats["listings_imported"],
            self._last_fetch_stats["duplicates_skipped"],
            self._last_fetch_stats["failed_listings"],
            self._last_fetch_stats["cities_found"],
            self._last_fetch_stats["municipalities_found"],
            self._last_fetch_stats["provinces_found"],
        )
        return results

    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        if not isinstance(payload, dict):
            raise ValueError("Listing payload must be an object")

        canonical_url = _as_optional_str(payload.get("canonical_url") or payload.get("source_url"))
        source_url = canonical_url
        if not source_url:
            raise ValueError("Missing required source_url")

        title = _as_optional_str(payload.get("title")) or _title_from_address_city(payload.get("address"), payload.get("city")) or "Klusvastgoed listing"
        address = _as_optional_str(payload.get("address"))
        city = _as_optional_str(payload.get("city"))
        municipality = _as_optional_str(payload.get("municipality")) or city
        province = _as_optional_str(payload.get("province"))
        postal_code = _as_optional_str(payload.get("postal_code"))
        description = _as_optional_str(payload.get("description"))
        asking_price = _to_float(payload.get("asking_price"))
        living_area = _to_float(payload.get("living_area"))
        property_type = _as_optional_str(payload.get("property_type")) or "kluswoning"
        external_listing_id = _extract_listing_id(source_url)
        publication_date = _parse_publication_date(payload.get("publication_date") or payload.get("listed_since"))
        image_url = _as_optional_str(payload.get("image_url"))

        raw_payload = dict(payload)
        raw_payload.setdefault("source_name", self.source_name)
        raw_payload.setdefault("listing_status", "active")
        raw_payload.setdefault("asking_price_status", "known" if asking_price is not None else "unknown")
        raw_payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        raw_payload.setdefault("source_timestamp", raw_payload.get("timestamp"))
        raw_payload.setdefault("external_listing_id", external_listing_id)
        raw_payload.setdefault("canonical_url", canonical_url)
        raw_payload.setdefault("municipality", municipality)
        raw_payload.setdefault("province", province)
        raw_payload.setdefault("postal_code", postal_code)
        raw_payload.setdefault("publication_date", publication_date)
        raw_payload.setdefault("listed_since", publication_date)
        raw_payload.setdefault("image_url", image_url)
        raw_payload.setdefault("source_name", self.source_name)

        return NormalizedListing(
            source_name=self.source_name,
            external_listing_id=external_listing_id,
            source_url=source_url,
            title=title,
            address=address,
            city=city,
            description=description,
            asking_price=asking_price,
            surface_m2=living_area,
            property_type=property_type,
            listing_status=_as_optional_str(payload.get("listing_status")) or "active",
            raw_payload=raw_payload,
        )

    def build_start_url_for_municipality(self, municipality: str) -> str:
        return self.service.build_start_url(municipality)

    def _extract_listing_record(self, *, source_url: str, html: str, listing_ref: dict[str, Any] | None = None) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        blocks = extract_json_ld_blocks(soup)
        detail = dict(listing_ref or {})

        canonical_url = extract_meta_content(soup, "og:url") or _canonical_url(soup) or source_url
        meta_title = extract_meta_content(soup, "og:title") or _page_title(soup)
        meta_description = extract_meta_content(soup, "og:description") or extract_meta_content(soup, "description")
        h1 = _first_text(soup.select_one("h1"))
        title = _clean_title(h1 or meta_title)

        listing_block = _find_real_estate_listing_block(blocks)
        jsonld_address = _as_dict(listing_block.get("address")) if listing_block else {}
        jsonld_offer = _as_dict(listing_block.get("offers")) if listing_block else {}

        address = _coalesce(
            _extract_address_from_title(title),
            _address_from_jsonld(jsonld_address),
            detail.get("address"),
        )
        city = _coalesce(
            _extract_city_from_title(title),
            _as_optional_str(jsonld_address.get("addressLocality")),
            detail.get("city"),
        )
        municipality = _coalesce(detail.get("municipality"), city)
        province = _coalesce(detail.get("province"), _as_optional_str(jsonld_address.get("addressRegion")))
        postal_code = _coalesce(detail.get("postal_code"), _as_optional_str(jsonld_address.get("postalCode")))
        if address and not detail.get("address"):
            detail["address"] = address
        if city and not detail.get("city"):
            detail["city"] = city
        if municipality and not detail.get("municipality"):
            detail["municipality"] = municipality
        if province and not detail.get("province"):
            detail["province"] = province
        if postal_code and not detail.get("postal_code"):
            detail["postal_code"] = postal_code

        asking_price = _coalesce_number(_extract_detail_price(soup), _price_from_jsonld(jsonld_offer), detail.get("asking_price"))
        living_area = _coalesce_number(_extract_metric_value(soup, labels=("leefruimte", "woonoppervlakte")), detail.get("living_area"))
        plot_area = _coalesce_number(_extract_metric_value(soup, labels=("perceel",)), detail.get("plot_area"))
        bedrooms = _to_int(_extract_metric_value(soup, labels=("slaapkamer",)))
        construction_year = _to_int(_extract_year_from_text(_metric_text(soup, labels=("gebouwd in",))))
        publication_date = _parse_publication_date(
            find_first_jsonld_value(blocks, ["datePublished", "dateCreated"])
            or extract_meta_content(soup, "article:published_time")
        )
        property_type = _coalesce(
            _extract_property_type_from_intro(soup),
            detail.get("property_type"),
            _extract_property_type_from_title(title),
            _property_type_from_jsonld(listing_block),
            "kluswoning",
        )
        description = _coalesce(_first_text(soup.select_one("p.kz-intro")), meta_description, detail.get("description"))
        image_url = _coalesce(extract_meta_content(soup, "og:image"), detail.get("image_url"), _first_image_url(soup), _first_jsonld_image(listing_block))

        photos = []
        for candidate in [
            image_url,
            *([_image_src(node) for node in soup.select("img.listing-thumb, #style-thumbnails img, .slider_cont img")]),
            *(_jsonld_images(listing_block)),
        ]:
            normalized = _normalize_photo(candidate)
            if normalized and normalized not in photos:
                photos.append(normalized)

        missing_fields = [
            field_name
            for field_name, value in {
                "title": title,
                "address": address,
                "city": city,
                "asking_price": asking_price,
                "living_area": living_area,
                "plot_area": plot_area,
                "property_type": property_type,
                "description": description,
            }.items()
            if value in (None, "")
        ]

        parser_warnings = []
        if missing_fields:
            parser_warnings.append(f"Missing fields: {', '.join(missing_fields)}")

        payload = {
            **detail,
            "source_name": self.source_name,
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "detail_url": source_url,
            "external_listing_id": _extract_listing_id(canonical_url),
            "title": title,
            "address": address,
            "city": city,
            "municipality": municipality,
            "province": province,
            "postal_code": postal_code,
            "asking_price": asking_price,
            "living_area": living_area,
            "plot_size": plot_area,
            "plot_size_m2": plot_area,
            "plot_area": plot_area,
            "property_type": property_type,
            "description": description,
            "listing_status": "active",
            "asking_price_status": "known" if asking_price is not None else "unknown",
            "image_url": image_url,
            "photos": photos,
            "bedrooms": bedrooms,
            "construction_year": construction_year,
            "publication_date": publication_date,
            "listed_since": publication_date,
            "missing_fields": missing_fields,
            "parser_warnings": parser_warnings,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scraped_at": datetime.now(timezone.utc),
        }
        payload["source_timestamp"] = payload["timestamp"]
        return payload


def build_klusvastgoed_start_url(municipality: str) -> str:
    slug = normalize_klusvastgoed_municipality_slug(municipality)
    if not slug:
        raise ValueError("Selecteer een gemeente.")
    return f"https://www.klusvastgoed.nl/kluswoning-{slug}"


def build_klusvastgoed_national_url() -> str:
    return "https://www.klusvastgoed.nl/kluswoningen-per-stad"


def _extract_listing_id(source_url: str) -> str | None:
    parsed = urlparse(source_url)
    path = str(parsed.path or "").strip("/")
    if not path:
        return None
    return path.replace("/", "::")


def _extract_address_from_title(title: str | None) -> str | None:
    text = _as_optional_str(title)
    if not text:
        return None
    match = re.match(r"^(.*?)\s+[A-Z][A-Za-zÀ-ÿ' -]+\s+kluswoning te koop$", text)
    if match:
        return _as_optional_str(match.group(1))
    return None


def _extract_city_from_title(title: str | None) -> str | None:
    text = _as_optional_str(title)
    if not text:
        return None
    match = re.match(r"^.*?\s+([A-Z][A-Za-zÀ-ÿ' -]+)\s+kluswoning te koop$", text)
    if match:
        return _as_optional_str(match.group(1))
    return None


def _canonical_url(soup: BeautifulSoup) -> str | None:
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        return str(link.get("href") or "").strip() or None
    return None


def _find_real_estate_listing_block(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("@type") or "").lower() == "realestatelisting":
            return block
    return {}


def _address_from_jsonld(address_value: dict[str, Any]) -> str | None:
    if not address_value:
        return None
    street = _as_optional_str(address_value.get("streetAddress"))
    postal_code = _as_optional_str(address_value.get("postalCode"))
    city = _as_optional_str(address_value.get("addressLocality"))
    parts = [part for part in [street, postal_code, city] if part]
    return ", ".join(parts) if parts else None


def _price_from_jsonld(offers_value: dict[str, Any]) -> float | None:
    if not offers_value:
        return None
    price = offers_value.get("price")
    if price in (None, ""):
        return None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def _property_type_from_jsonld(listing_block: dict[str, Any]) -> str | None:
    if not listing_block:
        return None
    value = listing_block.get("additionalType") or listing_block.get("propertyType") or listing_block.get("@type")
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_jsonld_image(listing_block: dict[str, Any]) -> str | None:
    if not listing_block:
        return None
    image_value = listing_block.get("image")
    if isinstance(image_value, str):
        return image_value.strip() or None
    if isinstance(image_value, list):
        for item in image_value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _jsonld_images(listing_block: dict[str, Any]) -> list[str]:
    if not listing_block:
        return []
    image_value = listing_block.get("image")
    if isinstance(image_value, str):
        return [image_value]
    if isinstance(image_value, list):
        return [str(item).strip() for item in image_value if isinstance(item, str) and str(item).strip()]
    return []


def _extract_detail_price(soup: BeautifulSoup) -> float | None:
    for node in soup.select(".kvg-price-now, .price, .listing-price"):
        amount, _ = parse_price(_first_text(node))
        if amount is not None:
            return amount
    return None


def _metric_text(soup: BeautifulSoup, *, labels: tuple[str, ...]) -> str | None:
    label_set = tuple(label.casefold() for label in labels)
    for node in soup.select("div.grid > div, .kvg-metric, .w-full.grid > div"):
        text = _first_text(node)
        if not text:
            continue
        lowered = text.casefold()
        if any(label in lowered for label in label_set):
            return text
    return None


def _extract_metric_value(soup: BeautifulSoup, *, labels: tuple[str, ...]) -> float | None:
    text = _metric_text(soup, labels=labels)
    if not text:
        return None
    match = re.search(r"([0-9][0-9\.,\s]*)", text)
    if not match:
        return None
    cleaned = match.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_year_from_text(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _extract_property_type_from_intro(soup: BeautifulSoup) -> str | None:
    intro = _first_text(soup.select_one("p.kz-intro"))
    if not intro:
        return None
    match = re.match(r"Deze\s+([a-zA-ZÀ-ÿ0-9\- ]+?)\s+in\s", intro, flags=re.IGNORECASE)
    if not match:
        return None
    return _as_optional_str(match.group(1).strip(" ,."))


def _extract_property_type_from_title(title: str | None) -> str | None:
    text = _as_optional_str(title)
    if not text:
        return None
    if "kluswoning" in text.casefold():
        return "kluswoning"
    return None


def _page_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return _as_optional_str(soup.title.string)
    return None


def _clean_title(value: str | None) -> str | None:
    text = _as_optional_str(value)
    if not text:
        return None
    return text.replace("  ", " ").strip()


def _first_text(node: object) -> str | None:
    getter = getattr(node, "get_text", None)
    if callable(getter):
        return _as_optional_str(str(getter(" ", strip=True)))
    return None


def _first_image_url(soup: BeautifulSoup) -> str | None:
    node = soup.select_one("img[src]")
    return _image_src(node)


def _image_src(node: object) -> str | None:
    getter = getattr(node, "get", None)
    if callable(getter):
        return _as_optional_str(str(getter("src") or ""))
    return None


def _normalize_photo(value: str | None) -> str | None:
    text = _as_optional_str(value)
    if not text:
        return None
    if text.startswith("//"):
        return f"https:{text}"
    return text


def _parse_publication_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _coalesce(*values: Any) -> Any:
    for value in values:
        normalized = _as_optional_str(value) if isinstance(value, str) else value
        if normalized not in (None, ""):
            return normalized
    return None


def _coalesce_number(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None:
            return parsed
    return None


def _title_from_address_city(address: Any, city: Any) -> str | None:
    address_text = _as_optional_str(address)
    city_text = _as_optional_str(city)
    if address_text and city_text:
        return f"{address_text} {city_text}"
    return address_text or city_text


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_int(value: Any, *, default: int | None = None, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _to_float(value: Any, *, default: float | None = None, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed