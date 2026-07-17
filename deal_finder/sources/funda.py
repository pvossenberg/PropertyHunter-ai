from __future__ import annotations

from datetime import date, datetime, timezone
import logging
import re
from typing import Any
import unicodedata
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from deal_finder.models import NormalizedListing
from deal_finder.sources.paginated_html import PaginatedHtmlListingAdapter
from models.property import Property
from scrapers.base import (
    extract_json_ld_blocks,
    extract_meta_content,
    find_first_jsonld_value,
    parse_address_from_jsonld,
    parse_area_sqm,
    parse_price,
)


LOGGER = logging.getLogger(__name__)


class FundaAdapter(PaginatedHtmlListingAdapter):
    source_name = "funda.nl"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://www.funda.nl/zoeken/koop"

    def __init__(self):
        super().__init__()
        self._last_parser_report: dict[str, Any] = {
            "imported_listings": 0,
            "skipped_listings": 0,
            "missing_fields_per_listing": [],
            "parser_warnings": [],
        }

    def load_and_normalize_listings(self, configuration: dict[str, Any]):
        results = super().load_and_normalize_listings(configuration)

        imported_listings = sum(1 for item in results if item.success)
        skipped_listings = int(self._last_fetch_stats.get("records_skipped") or 0)
        missing_fields_per_listing: list[dict[str, Any]] = []
        parser_warnings: list[str] = []

        for item in results:
            payload = item.payload if isinstance(item.payload, dict) else {}
            source_url = _as_optional_str(payload.get("source_url"))
            listing_id = _as_optional_str(payload.get("listing_id")) or _extract_listing_id_from_url(source_url or "")

            missing_fields = payload.get("missing_fields")
            if isinstance(missing_fields, list):
                normalized_missing = [str(field).strip() for field in missing_fields if str(field).strip()]
            else:
                normalized_missing = []

            if normalized_missing:
                missing_fields_per_listing.append(
                    {
                        "record_index": item.record_index,
                        "listing_id": listing_id,
                        "source_url": source_url,
                        "missing_fields": normalized_missing,
                    }
                )

            warnings = payload.get("parser_warnings")
            if isinstance(warnings, list):
                for warning in warnings:
                    warning_text = str(warning).strip()
                    if warning_text:
                        parser_warnings.append(warning_text)

            if item.error:
                parser_warnings.append(f"record {item.record_index}: {item.error}")

        self._last_parser_report = {
            "imported_listings": imported_listings,
            "skipped_listings": skipped_listings,
            "missing_fields_per_listing": missing_fields_per_listing,
            "parser_warnings": parser_warnings,
        }

        LOGGER.info(
            "%s parser final report imported_listings=%s skipped_listings=%s listings_with_missing_fields=%s parser_warnings=%s",
            self.source_name,
            imported_listings,
            skipped_listings,
            len(missing_fields_per_listing),
            len(parser_warnings),
        )

        for item in missing_fields_per_listing:
            LOGGER.warning(
                "%s missing fields listing_id=%s source_url=%s fields=%s",
                self.source_name,
                item.get("listing_id"),
                item.get("source_url"),
                ", ".join(item.get("missing_fields") or []),
            )

        for warning in parser_warnings:
            LOGGER.warning("%s parser warning %s", self.source_name, warning)

        return results

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        ok, warnings = super().validate_configuration(configuration)
        if not ok:
            return ok, warnings

        start_url = str(configuration.get("start_url") or self.default_start_url).strip()
        host = (urlparse(start_url).hostname or "").lower()
        if host and "funda.nl" not in host:
            warnings.append("start_url must point to funda.nl")
        return len(warnings) == 0, warnings

    def normalize_listing(self, payload: dict[str, Any]) -> NormalizedListing:
        if not isinstance(payload, dict):
            raise ValueError("Listing payload must be an object")

        source_url = _as_optional_str(payload.get("source_url") or payload.get("url") or payload.get("link"))
        if not source_url:
            raise ValueError("Missing required source_url")

        listing_id = _as_optional_str(payload.get("listing_id")) or _extract_listing_id_from_url(source_url)
        living_area = _to_float(payload.get("living_area"))
        plot_size = _to_float(payload.get("plot_size"))
        bedrooms = _to_int(payload.get("bedrooms"))
        asking_price = _to_float(payload.get("asking_price"))
        asking_price_status = _as_optional_str(payload.get("asking_price_status")) or ("known" if asking_price is not None else "unknown")
        asking_price_text = _as_optional_str(payload.get("asking_price_text"))
        listed_since = _to_date(payload.get("listed_since")) or _to_date(payload.get("listed_at")) or _to_date(payload.get("date_listed"))
        last_price_reduction_date = _to_date(payload.get("last_price_reduction_date"))
        price_reduction_count = _to_int(payload.get("price_reduction_count")) or 0
        original_asking_price = _to_float(payload.get("original_asking_price"))
        current_asking_price = _to_float(payload.get("current_asking_price"))
        if current_asking_price is None and asking_price is not None:
            current_asking_price = asking_price
        total_price_reduction_amount = _to_float(payload.get("total_price_reduction_amount"))
        total_price_reduction_percentage = _to_float(payload.get("total_price_reduction_percentage"))
        price_per_m2 = _to_float(payload.get("price_per_m2"))
        if price_per_m2 is None and asking_price is not None and living_area not in (None, 0):
            price_per_m2 = round(asking_price / living_area, 2)

        listing_history_source = _as_optional_str(payload.get("listing_history_source"))
        listing_history_confidence = _as_optional_str(payload.get("listing_history_confidence")) or "unknown"
        broker = _as_optional_str(payload.get("broker"))
        photos = _to_str_list(payload.get("photos"))
        source_timestamp = _as_optional_str(payload.get("source_timestamp")) or _as_optional_str(payload.get("timestamp"))
        scraped_at = payload.get("scraped_at") if isinstance(payload.get("scraped_at"), datetime) else None

        if listing_history_source is None and (listed_since is not None or price_reduction_count or original_asking_price is not None):
            listing_history_source = "funda"
        if listing_history_confidence == "unknown" and (listed_since is not None or price_reduction_count or original_asking_price is not None):
            listing_history_confidence = "medium"

        raw_payload = dict(payload)
        raw_payload.update(
            {
                "listing_id": listing_id,
                "timestamp": source_timestamp or _utc_now_iso(),
                "scraped_at": scraped_at or datetime.now(timezone.utc),
                "source_timestamp": source_timestamp or _utc_now_iso(),
                "photos": photos,
                "plot_size": plot_size,
                "plot_size_m2": plot_size,
                "bedrooms": bedrooms,
                "energy_label": _as_optional_str(payload.get("energy_label")),
                "construction_year": _to_int(payload.get("construction_year")),
                "broker": broker,
                "asking_price_text": asking_price_text,
                "asking_price_status": asking_price_status,
                "listed_since": listed_since,
                "days_on_market": _to_int(payload.get("days_on_market")),
                "original_asking_price": original_asking_price,
                "current_asking_price": current_asking_price,
                "price_reduction_count": price_reduction_count,
                "last_price_reduction_date": last_price_reduction_date,
                "total_price_reduction_amount": total_price_reduction_amount,
                "total_price_reduction_percentage": total_price_reduction_percentage,
                "listing_history_source": listing_history_source,
                "listing_history_confidence": listing_history_confidence,
                "price_per_m2": price_per_m2,
                "current_use": _as_optional_str(payload.get("current_use")),
                "zoning": _as_optional_str(payload.get("zoning")),
                "annual_rent": _to_float(payload.get("annual_rent")),
                "missing_fields": _to_str_list(payload.get("missing_fields")),
                "parser_warnings": _to_str_list(payload.get("parser_warnings")),
            }
        )

        return NormalizedListing(
            source_name=self.source_name,
            external_listing_id=listing_id,
            source_url=source_url,
            title=_as_optional_str(payload.get("title")),
            address=_as_optional_str(payload.get("address")),
            city=_as_optional_str(payload.get("city")),
            asking_price=asking_price,
            surface_m2=living_area,
            property_type=_as_optional_str(payload.get("property_type")),
            description=_as_optional_str(payload.get("description")),
            listing_status=_as_optional_str(payload.get("listing_status")) or "active",
            raw_payload=raw_payload,
        )

    def to_property_model(self, payload: dict[str, Any]) -> Property:
        return super().to_property_model(payload)

    def _extract_listing_urls(self, *, index_url: str, soup: BeautifulSoup) -> list[str]:
        urls: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = anchor.get("href")
            resolved = self._absolute_url(index_url, href)
            if resolved and _looks_like_funda_listing_url(resolved):
                urls.add(resolved)
        return sorted(urls)

    def _extract_next_page_url(self, *, index_url: str, soup: BeautifulSoup) -> str | None:
        selectors = [
            "a[rel='next']",
            "a[aria-label*='Volgende']",
            "a[aria-label*='volgende']",
            "a[aria-label*='Next']",
            "a.pagination-next",
        ]
        for selector in selectors:
            anchor = soup.select_one(selector)
            if not anchor:
                continue
            resolved = self._absolute_url(index_url, anchor.get("href"))
            if resolved:
                return resolved
        return None

    def _extract_listing_record(self, *, source_url: str, html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        blocks = extract_json_ld_blocks(soup)
        parser_warnings: list[str] = []

        title = _as_optional_str(_safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="title",
            parser_warnings=parser_warnings,
            extractor=lambda: extract_meta_content(soup, "og:title"),
        ))
        if not title:
            heading = soup.select_one("h1")
            title = _as_optional_str(heading.get_text(" ", strip=True) if heading else None)
        if not title:
            title = _as_optional_str(_safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="title_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["name", "headline"]),
            ))

        description = _as_optional_str(_safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="description",
            parser_warnings=parser_warnings,
            extractor=lambda: extract_meta_content(soup, "og:description") or extract_meta_content(soup, "description"),
        ))
        if not description:
            description = _as_optional_str(_read_details_value(soup, ["omschrijving", "description"]))
        if not description:
            description = _as_optional_str(_safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="description_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["description"]),
            ))

        address_value = _safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="address_jsonld",
            parser_warnings=parser_warnings,
            extractor=lambda: find_first_jsonld_value(blocks, ["address"]),
        )
        address = _as_optional_str(parse_address_from_jsonld(address_value))
        city_from_jsonld = _extract_city_from_jsonld_address(address_value)

        details_map = _safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="details_map",
            parser_warnings=parser_warnings,
            extractor=lambda: _collect_details_map(soup),
        )
        if not isinstance(details_map, dict):
            details_map = {}

        if not address:
            address = _as_optional_str(_read_details_value(soup, ["adres", "address"], details_map))

        city = city_from_jsonld or _extract_city_from_address(address) or _as_optional_str(_read_details_value(soup, ["plaats", "city"], details_map))

        price_value = _safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="asking_price_jsonld",
            parser_warnings=parser_warnings,
            extractor=lambda: find_first_jsonld_value(blocks, ["price", "offers", "priceSpecification"]),
        )
        price_value = _extract_price_value(price_value)
        asking_price_text = _as_optional_str(price_value)
        if price_value in (None, ""):
            price_value = _read_details_value(soup, ["vraagprijs", "asking price", "koopprijs"], details_map) or extract_meta_content(soup, "product:price:amount") or extract_meta_content(soup, "og:price:amount")
            asking_price_text = _as_optional_str(price_value)
        asking_price, asking_price_status = parse_price(price_value)

        living_area_value = _safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="living_area_jsonld",
            parser_warnings=parser_warnings,
            extractor=lambda: find_first_jsonld_value(blocks, ["floorSize", "floorSizeValue", "livingArea", "area"]),
        )
        living_area = parse_area_sqm(
            _extract_area_value(living_area_value) or _read_details_value(soup, ["woonoppervlakte", "living area", "gebruiksoppervlakte wonen", "oppervlakte"], details_map)
        )

        plot_size_value = _safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="plot_size_jsonld",
            parser_warnings=parser_warnings,
            extractor=lambda: find_first_jsonld_value(blocks, ["lotSize", "plotArea", "landSize"]),
        )
        plot_size = parse_area_sqm(
            _extract_area_value(plot_size_value) or _read_details_value(soup, ["perceeloppervlakte", "plot size", "perceel", "grondoppervlakte"], details_map)
        )

        property_type = _as_optional_str(_safe_extract(
            source_name=self.source_name,
            source_url=source_url,
            field_name="property_type_jsonld",
            parser_warnings=parser_warnings,
            extractor=lambda: _extract_property_type_value(find_first_jsonld_value(blocks, ["propertyType", "additionalType", "@type"])),
        ))
        if not property_type:
            property_type = _as_optional_str(_read_details_value(soup, ["soort woonhuis", "soort woning", "woningtype", "property type"], details_map))

        bedrooms = _to_int(
            _read_details_value(soup, ["slaapkamers", "aantal slaapkamers", "bedrooms", "aantal kamers"], details_map)
            or _safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="bedrooms_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["numberOfBedrooms", "numberOfRooms"]),
            )
        )
        energy_label = _normalize_energy_label(
            _read_details_value(soup, ["energielabel", "energie label", "energy label"], details_map)
            or _safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="energy_label_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["energyLabel", "energyPerformanceCertificate"]),
            )
        )
        construction_year = _to_int(
            _read_details_value(soup, ["bouwjaar", "construction year"], details_map)
            or _safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="construction_year_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["yearBuilt", "constructionYear", "dateBuilt"]),
            )
        )

        broker = _as_optional_str(_read_details_value(soup, ["makelaar", "aanbieder", "broker"], details_map))
        if not broker:
            broker_anchor = soup.select_one("a[href*='/makelaar-'], a[href*='makelaar']")
            broker = _as_optional_str(broker_anchor.get_text(" ", strip=True) if broker_anchor else None)
        if not broker:
            broker = _as_optional_str(_safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="broker_meta",
                parser_warnings=parser_warnings,
                extractor=lambda: extract_meta_content(soup, "author"),
            ))

        listed_since_raw = _read_details_value(
            soup,
            ["sinds", "aangeboden sinds", "te koop sinds", "listed since", "placed since", "publicatiedatum"],
            details_map,
        )
        listed_since = _to_date(listed_since_raw) or _to_date(
            _safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="listed_since_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["datePosted", "dateCreated", "datePublished"]),
            )
        )

        price_reduction_count = _to_int(
            _read_details_value(
                soup,
                ["prijswijzigingen", "aantal prijswijzigingen", "prijsverlagingen", "price changes", "price reductions"],
                details_map,
            )
        ) or 0

        original_asking_price = _to_float(
            _read_details_value(
                soup,
                ["oorspronkelijke vraagprijs", "originele vraagprijs", "original asking price", "eerste vraagprijs"],
                details_map,
            )
        )
        current_asking_price = asking_price
        total_price_reduction_amount = None
        total_price_reduction_percentage = None
        if original_asking_price is not None and current_asking_price is not None and original_asking_price > current_asking_price:
            total_price_reduction_amount = round(original_asking_price - current_asking_price, 2)
            if original_asking_price > 0:
                total_price_reduction_percentage = round((total_price_reduction_amount / original_asking_price) * 100.0, 2)

        last_price_reduction_date = _to_date(
            _read_details_value(
                soup,
                ["laatste prijswijziging", "laatste prijsverlaging", "last price reduction", "prijs gewijzigd op"],
                details_map,
            )
        )

        listing_history_source = None
        listing_history_confidence = "unknown"
        if listed_since is not None or price_reduction_count or original_asking_price is not None:
            listing_history_source = "funda"
            listing_history_confidence = "high" if listed_since is not None and (price_reduction_count > 0 or original_asking_price is not None) else "medium"

        photos = _extract_photo_urls(soup, base_url=source_url)
        photos.extend(_extract_jsonld_photo_urls(blocks, base_url=source_url))
        photos = list(dict.fromkeys([item for item in photos if item]))

        listing_id = _extract_listing_id_from_url(source_url)
        if not listing_id:
            listing_id = _as_optional_str(_safe_extract(
                source_name=self.source_name,
                source_url=source_url,
                field_name="listing_id_jsonld",
                parser_warnings=parser_warnings,
                extractor=lambda: find_first_jsonld_value(blocks, ["identifier", "sku", "productID"]),
            ))

        timestamp = _utc_now_iso()
        missing_fields = _collect_missing_fields(
            {
                "title": title,
                "address": address,
                "city": city,
                "asking_price": asking_price,
                "living_area": living_area,
            "plot_size_m2": plot_size,
                "property_type": property_type,
                "bedrooms": bedrooms,
                "energy_label": energy_label,
                "construction_year": construction_year,
                "broker": broker,
                "description": description,
                "photos": photos,
                "source_url": source_url,
                "listing_id": listing_id,
                "timestamp": timestamp,
            }
        )

        for field_name in missing_fields:
            LOGGER.warning("%s missing field source_url=%s field=%s", self.source_name, source_url, field_name)

        return {
            "source_name": self.source_name,
            "source_url": source_url,
            "listing_id": listing_id,
            "timestamp": timestamp,
            "source_timestamp": timestamp,
            "scraped_at": datetime.now(timezone.utc),
            "title": title,
            "address": address,
            "city": city,
            "country": _extract_country_from_jsonld(address_value),
            "asking_price": asking_price,
            "asking_price_text": asking_price_text,
            "asking_price_status": asking_price_status,
            "living_area": living_area,
            "plot_size": plot_size,
            "plot_size_m2": plot_size,
            "property_type": property_type,
            "bedrooms": bedrooms,
            "energy_label": energy_label,
            "construction_year": construction_year,
            "broker": broker,
            "description": description,
            "photos": photos,
            "raw_text": _extract_visible_text(soup),
            "surface_m2": living_area,
            "days_on_market": None,
            "listed_since": listed_since,
            "original_asking_price": original_asking_price,
            "current_asking_price": current_asking_price,
            "price_reduction_count": price_reduction_count,
            "last_price_reduction_date": last_price_reduction_date,
            "total_price_reduction_amount": total_price_reduction_amount,
            "total_price_reduction_percentage": total_price_reduction_percentage,
            "listing_history_source": listing_history_source,
            "listing_history_confidence": listing_history_confidence,
            "price_per_m2": _round_price_per_m2(asking_price, living_area),
            "current_use": _read_details_value(soup, ["gebruik", "current use", "bestemming"], details_map),
            "zoning": _read_details_value(soup, ["bestemming", "zoning"], details_map),
            "annual_rent": _to_float(_read_details_value(soup, ["jaarlijkse huur", "annual rent"], details_map)),
            "listing_status": "active",
            "raw_details": details_map,
            "missing_fields": missing_fields,
            "parser_warnings": parser_warnings,
        }


def _safe_extract(
    *,
    source_name: str,
    source_url: str,
    field_name: str,
    parser_warnings: list[str],
    extractor,
) -> Any:
    try:
        return extractor()
    except Exception as error:
        warning = f"field={field_name} parse_failed={type(error).__name__}: {error}"
        parser_warnings.append(warning)
        LOGGER.warning("%s parse warning source_url=%s field=%s error=%s", source_name, source_url, field_name, error)
        return None


def _collect_missing_fields(values: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key, value in values.items():
        if value is None:
            missing.append(key)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(key)
            continue
        if isinstance(value, list) and len(value) == 0:
            missing.append(key)
    return missing


def _extract_price_value(value: Any) -> Any:
    current = value
    while isinstance(current, list) and current:
        current = current[0]

    if isinstance(current, dict):
        for key in ("price", "amount", "value", "minPrice", "maxPrice"):
            if current.get(key) not in (None, ""):
                return current.get(key)

        nested = current.get("priceSpecification") or current.get("offers")
        if nested is not None:
            return _extract_price_value(nested)

    return current


def _extract_area_value(value: Any) -> Any:
    current = value
    while isinstance(current, list) and current:
        current = current[0]

    if isinstance(current, dict):
        for key in ("value", "size", "amount", "maxValue", "minValue"):
            if current.get(key) not in (None, ""):
                return current.get(key)
    return current


def _extract_property_type_value(value: Any) -> Any:
    current = value
    while isinstance(current, list) and current:
        current = current[0]

    if isinstance(current, dict):
        return current.get("@type") or current.get("name") or current.get("value")
    return current


def _extract_country_from_jsonld(address_value: Any) -> str | None:
    if not isinstance(address_value, dict):
        return None
    return _as_optional_str(address_value.get("addressCountry"))


def _extract_jsonld_photo_urls(blocks: list[dict[str, Any]], base_url: str | None = None) -> list[str]:
    image_value = find_first_jsonld_value(blocks, ["image", "photos"])
    raw_candidates: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text:
                raw_candidates.append(text)
            return
        if isinstance(value, dict):
            for key in ("url", "contentUrl", "image", "thumbnailUrl"):
                if key in value:
                    collect(value.get(key))
            return
        if isinstance(value, list):
            for item in value:
                collect(item)

    collect(image_value)

    resolved: list[str] = []
    for candidate in raw_candidates:
        if candidate.startswith("data:"):
            continue
        value = urljoin(base_url, candidate) if base_url else candidate
        resolved.append(value)
    return list(dict.fromkeys(resolved))


def _collect_details_map(soup: BeautifulSoup) -> dict[str, str]:
    details: dict[str, str] = {}
    for row in soup.select("dt"):
        key = _normalize_key(row.get_text(" ", strip=True))
        if not key:
            continue
        value_el = row.find_next_sibling("dd")
        value = _as_optional_str(value_el.get_text(" ", strip=True) if value_el else None)
        if value:
            details[key] = value
    return details


def _read_details_value(soup: BeautifulSoup, labels: list[str], details_map: dict[str, str] | None = None) -> str | None:
    details = details_map if isinstance(details_map, dict) else _collect_details_map(soup)
    normalized_labels = [_normalize_key(label) for label in labels]
    for label in normalized_labels:
        if label in details:
            return details[label]
    for key, value in details.items():
        if any(label and label in key for label in normalized_labels):
            return value
    return None


def _extract_photo_urls(soup: BeautifulSoup, base_url: str | None = None) -> list[str]:
    urls: list[str] = []
    for selector in ("meta[property='og:image']", "img[src]", "img[data-src]"):
        for node in soup.select(selector):
            candidate = node.get("content") or node.get("src") or node.get("data-src")
            if not isinstance(candidate, str):
                continue
            value = candidate.strip()
            if not value:
                continue
            if value.startswith("data:"):
                continue
            if base_url:
                value = urljoin(base_url, value)
            urls.append(value)
    return list(dict.fromkeys(urls))


def _extract_listing_id_from_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    path = (urlparse(url).path or "").strip("/")
    for segment in reversed(path.split("/")):
        match = re.search(r"(\d{4,})", segment)
        if match:
            return match.group(1)
    return None


def _extract_city_from_jsonld_address(address_value: Any) -> str | None:
    if not isinstance(address_value, dict):
        return None
    return _as_optional_str(address_value.get("addressLocality") or address_value.get("city"))


def normalize_funda_area_slug(city: str) -> str:
    raw_value = str(city or "").strip().lower()
    if not raw_value:
        return ""

    normalized = unicodedata.normalize("NFKD", raw_value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_value = ascii_value.replace("'", "")
    ascii_value = re.sub(r"[^a-z0-9\-\s]", " ", ascii_value)
    ascii_value = re.sub(r"[\s\-]+", "-", ascii_value).strip("-")
    return ascii_value


def _looks_like_funda_listing_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if "funda.nl" not in host:
        return False
    path = (urlparse(url).path or "").lower()
    return "/detail/" in path and any(token in path for token in ("/koop/", "/huur/"))


def _normalize_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.lower().split())
    return re.sub(r"[^a-z0-9\s]", "", text)


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"([0-9][0-9\.,\s]*)", str(value))
    if not match:
        return None
    cleaned = match.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"(\d{1,4})", str(value))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _to_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_energy_label(value: Any) -> str | None:
    text = _as_optional_str(value)
    if not text:
        return None
    match = re.search(r"\b([A-G](?:\+{1,3})?)", text.upper())
    if match:
        return match.group(1)
    return text


def _extract_visible_text(soup: BeautifulSoup) -> str | None:
    text = soup.get_text(" ", strip=True)
    cleaned = " ".join(text.split())
    return cleaned or None


def _round_price_per_m2(asking_price: float | None, surface_m2: float | None) -> float | None:
    if asking_price in (None, 0) or surface_m2 in (None, 0):
        return None
    try:
        return round(float(asking_price) / float(surface_m2), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    match = re.search(r"(\d{1,2})\s+([a-zA-Záàäâçéèëêïîñóòöôüû]+)\s+(\d{4})", text.lower())
    if match:
        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))
        month_number = _MONTH_NAME_TO_NUMBER.get(month_name)
        if month_number:
            try:
                return date(year, month_number, day)
            except ValueError:
                return None
    return None


_MONTH_NAME_TO_NUMBER = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mrt": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "dec": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
    "december": 12,
}


def _extract_city_from_address(address: str | None) -> str | None:
    text = _as_optional_str(address)
    if not text:
        return None
    parts = [part.strip() for part in text.split() if part.strip()]
    if not parts:
        return None
    last_tokens = parts[-2:] if len(parts) >= 2 else parts
    candidate = " ".join(last_tokens)
    candidate = re.sub(r"\b\d{4}\s?[A-Za-z]{2}\b", "", candidate).strip()
    candidate = re.sub(r"\bNL\b", "", candidate).strip()
    return candidate or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()