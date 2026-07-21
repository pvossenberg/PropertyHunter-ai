from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger(__name__)

KLUSVASTGOED_BASE_URL = "https://www.klusvastgoed.nl"
KLUSVASTGOED_LISTINGS_ENDPOINT = f"{KLUSVASTGOED_BASE_URL}/get_panden.php"
KLUSVASTGOED_SET_BOUNDS_ENDPOINT = f"{KLUSVASTGOED_BASE_URL}/set_bounds.php"
KLUSVASTGOED_CITY_INDEX_URL = f"{KLUSVASTGOED_BASE_URL}/kluswoningen-per-stad"
KLUSVASTGOED_PAGE_SIZE = 12
KLUSVASTGOED_USER_AGENT = "Mozilla/5.0 (compatible; PropertyHunterAI-Klusvastgoed/1.0)"
DEFAULT_BOUNDS_LAT_DELTA = 0.08
DEFAULT_BOUNDS_LNG_DELTA = 0.15
NETHERLANDS_PROVINCES = (
    "Drenthe",
    "Flevoland",
    "Friesland",
    "Gelderland",
    "Groningen",
    "Limburg",
    "Noord-Brabant",
    "Noord-Holland",
    "Overijssel",
    "Utrecht",
    "Zeeland",
    "Zuid-Holland",
)


@dataclass
class KlusvastgoedListingRef:
    source_url: str
    title: str | None = None
    address: str | None = None
    city: str | None = None
    municipality: str | None = None
    province: str | None = None
    canonical_url: str | None = None
    asking_price: float | None = None
    living_area: float | None = None
    plot_area: float | None = None
    image_url: str | None = None
    property_type: str | None = None

    def to_payload(self) -> dict[str, object | None]:
        return {
            "source_url": self.source_url,
            "title": self.title,
            "address": self.address,
            "city": self.city,
            "municipality": self.municipality,
            "province": self.province,
            "canonical_url": self.canonical_url,
            "asking_price": self.asking_price,
            "living_area": self.living_area,
            "plot_area": self.plot_area,
            "image_url": self.image_url,
            "property_type": self.property_type,
        }


class KlusvastgoedService:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": KLUSVASTGOED_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json",
            }
        )

    def build_start_url(self, municipality: str) -> str:
        slug = normalize_klusvastgoed_municipality_slug(municipality)
        if not slug:
            raise ValueError("municipality is required")
        return f"{KLUSVASTGOED_BASE_URL}/kluswoning-{slug}"

    def build_city_index_url(self) -> str:
        return KLUSVASTGOED_CITY_INDEX_URL

    def discover_city_refs(self, timeout_seconds: float) -> list[dict[str, object | None]]:
        html = self._request_text("GET", KLUSVASTGOED_CITY_INDEX_URL, timeout_seconds=timeout_seconds)
        soup = BeautifulSoup(html, "html.parser")
        refs: list[dict[str, object | None]] = []
        seen_urls: set[str] = set()

        for anchor in soup.select('a[href^="/kluswoning-"]'):
            href = str(anchor.get("href") or "").strip()
            city_url = urljoin(KLUSVASTGOED_BASE_URL, href)
            if city_url in seen_urls:
                continue
            seen_urls.add(city_url)

            text = _clean_text(_text_from_node(anchor)) or ""
            municipality, province, listing_count = _parse_city_overview_label(text=text, href=href)
            if not municipality:
                municipality = _municipality_from_slug(href.rsplit("/", 1)[-1])

            refs.append(
                {
                    "municipality": municipality,
                    "province": province,
                    "city_url": city_url,
                    "listing_count": listing_count,
                }
            )

        return refs

    def fetch_national_listing_cards(self, *, timeout_seconds: float, max_pages: int) -> list[dict[str, object | None]]:
        city_refs = self.discover_city_refs(timeout_seconds=timeout_seconds)
        records: list[dict[str, object | None]] = []
        seen_urls: set[str] = set()
        for city_ref in city_refs:
            municipality = str(city_ref.get("municipality") or "").strip()
            city_url = str(city_ref.get("city_url") or "").strip()
            province = str(city_ref.get("province") or "").strip() or None
            if not municipality or not city_url:
                continue
            try:
                city_records = self.fetch_listing_cards_for_city(
                    city_url,
                    municipality=municipality,
                    province=province,
                    timeout_seconds=timeout_seconds,
                    max_pages=max_pages,
                )
            except Exception as error:
                LOGGER.warning("Klusvastgoed national crawl failed for %s: %s", city_url, error)
                continue

            for record in city_records:
                source_url = str(record.get("source_url") or "").strip()
                if not source_url or source_url in seen_urls:
                    continue
                seen_urls.add(source_url)
                records.append(record)

        return records

    def fetch_municipality_page(self, start_url: str, timeout_seconds: float) -> str:
        return self._request_text("GET", start_url, timeout_seconds=timeout_seconds)

    def fetch_listing_cards_for_city(
        self,
        start_url: str,
        *,
        municipality: str | None = None,
        province: str | None = None,
        timeout_seconds: float,
        max_pages: int,
    ) -> list[dict[str, object | None]]:
        html = self.fetch_municipality_page(start_url, timeout_seconds=timeout_seconds)
        records: list[dict[str, object | None]] = []
        seen_urls: set[str] = set()
        expected_city_slug = _expected_city_slug_from_start_url(start_url)
        self._prime_bounds_session(start_url=start_url, html=html, timeout_seconds=timeout_seconds)

        for page_index in range(max(1, int(max_pages))):
            offset = page_index * KLUSVASTGOED_PAGE_SIZE
            fragment = self._fetch_listing_fragment(start_url=start_url, timeout_seconds=timeout_seconds, offset=offset, limit=KLUSVASTGOED_PAGE_SIZE)
            if not fragment.strip():
                if page_index == 0:
                    LOGGER.warning("Klusvastgoed listing endpoint returned an empty response for %s", start_url)
                break

            raw_batch = self.extract_listing_refs_from_fragment(start_url=start_url, html_fragment=fragment)
            if not raw_batch:
                break

            batch = self._filter_refs_for_municipality(refs=raw_batch, expected_city_slug=expected_city_slug)

            added_in_batch = 0
            for ref in batch:
                payload = ref.to_payload()
                if municipality and not payload.get("municipality"):
                    payload["municipality"] = municipality
                if province and not payload.get("province"):
                    payload["province"] = province
                source_url = str(payload.get("source_url") or "").strip()
                if source_url in seen_urls:
                    continue
                seen_urls.add(source_url)
                records.append(payload)
                added_in_batch += 1

            if added_in_batch == 0 and expected_city_slug:
                continue
            if added_in_batch == 0:
                break

        return records

    def fetch_listing_cards(self, start_url: str, *, timeout_seconds: float, max_pages: int) -> list[dict[str, object | None]]:
        return self.fetch_listing_cards_for_city(start_url, timeout_seconds=timeout_seconds, max_pages=max_pages)

    def fetch_listing_detail_html(self, source_url: str, *, timeout_seconds: float, referer: str | None = None) -> str:
        headers = {"Referer": referer} if referer else None
        return self._request_text("GET", source_url, timeout_seconds=timeout_seconds, headers=headers)

    def extract_listing_refs_from_fragment(self, *, start_url: str, html_fragment: str) -> list[KlusvastgoedListingRef]:
        soup = BeautifulSoup(html_fragment, "html.parser")
        refs: list[KlusvastgoedListingRef] = []

        for card in soup.select("div.listing-card"):
            anchor = card.find_parent("a")
            if anchor is None:
                anchor = card.find("a", href=True)
            href = str(anchor.get("href") or "").strip() if anchor else ""
            source_url = urljoin(start_url, href)
            if not source_url or not _is_supported_listing_path(source_url):
                continue

            city = _clean_text(_text_from_node(card.select_one("strong.mobile-title")))
            address = _clean_text(_text_from_node(card.select_one("span.min-h-40p")))
            title = _clean_text(" ".join(part for part in [address, city] if part))

            metric_values = [
                _clean_text(node.get_text(" ", strip=True))
                for node in card.select("div.kvg-metrics-row span.kvg-metric span.v")
            ]
            living_area = _parse_area(metric_values[1] if len(metric_values) > 1 else None)
            plot_area = _parse_area(metric_values[2] if len(metric_values) > 2 else None)
            asking_price = _parse_price(_text_from_node(card.select_one("span.kvg-price-now")))
            image_url = _normalize_image_url(card.select_one("img.listing-thumb"))
            property_type = _property_type_from_image_alt(card.select_one("img.listing-thumb"))

            refs.append(
                KlusvastgoedListingRef(
                    source_url=source_url,
                    title=title,
                    address=address,
                    city=city,
                    asking_price=asking_price,
                    living_area=living_area,
                    plot_area=plot_area,
                    image_url=image_url,
                    property_type=property_type,
                )
            )

        return refs

    def _fetch_listing_fragment(self, *, start_url: str, timeout_seconds: float, offset: int, limit: int) -> str:
        headers = {
            "Referer": start_url,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": KLUSVASTGOED_BASE_URL,
        }
        payload = {
            "offset": str(max(0, int(offset))),
            "limit": str(max(1, int(limit))),
        }
        return self._request_text(
            "POST",
            KLUSVASTGOED_LISTINGS_ENDPOINT,
            timeout_seconds=timeout_seconds,
            headers=headers,
            data=payload,
        )

    def _prime_bounds_session(self, *, start_url: str, html: str, timeout_seconds: float) -> None:
        center = extract_map_center_from_html(html)
        if center is None:
            LOGGER.warning("Klusvastgoed map center missing for %s; continuing without bounds session", start_url)
            return

        center_lat, center_lng = center
        payload = {
            "ne_lat": f"{center_lat + DEFAULT_BOUNDS_LAT_DELTA:.6f}",
            "ne_lng": f"{center_lng + DEFAULT_BOUNDS_LNG_DELTA:.6f}",
            "sw_lat": f"{center_lat - DEFAULT_BOUNDS_LAT_DELTA:.6f}",
            "sw_lng": f"{center_lng - DEFAULT_BOUNDS_LNG_DELTA:.6f}",
        }
        self._request_text(
            "POST",
            KLUSVASTGOED_SET_BOUNDS_ENDPOINT,
            timeout_seconds=timeout_seconds,
            headers={"Referer": start_url, "Origin": KLUSVASTGOED_BASE_URL},
            data=payload,
        )

    def _request_text(
        self,
        method: str,
        url: str,
        *,
        timeout_seconds: float,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        attempts: int = 3,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    timeout=timeout_seconds,
                    headers=headers,
                    data=data,
                )
                response.raise_for_status()
                return response.text
            except Exception as error:
                last_error = error
                if attempt >= max(1, int(attempts)):
                    break
                LOGGER.warning("Klusvastgoed request retry %s/%s for %s failed: %s", attempt, attempts, url, error)
                time.sleep(min(0.5 * attempt, 2.0))
        if last_error is not None:
            raise last_error
        return ""

    def _filter_refs_for_municipality(
        self,
        *,
        refs: list[KlusvastgoedListingRef],
        expected_city_slug: str,
    ) -> list[KlusvastgoedListingRef]:
        if not expected_city_slug:
            return refs

        filtered: list[KlusvastgoedListingRef] = []
        skipped = 0
        for ref in refs:
            city_slug = normalize_klusvastgoed_municipality_slug(ref.city or "")
            if city_slug == expected_city_slug:
                filtered.append(ref)
            else:
                skipped += 1

        if skipped:
            LOGGER.info(
                "Filtered %s Klusvastgoed cards outside selected municipality slug=%s",
                skipped,
                expected_city_slug,
            )
        return filtered


def normalize_klusvastgoed_municipality_slug(municipality: str) -> str:
    if not isinstance(municipality, str):
        return ""
    normalized = unicodedata.normalize("NFKD", municipality)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.casefold().replace("&", " en ")
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


def extract_map_center_from_html(html: str) -> tuple[float, float] | None:
    if not isinstance(html, str) or not html.strip():
        return None

    lat_match = re.search(r'centerLat\s*=\s*parseFloat\("([0-9\.-]+)"\)', html)
    lng_match = re.search(r'centerLng\s*=\s*parseFloat\("([0-9\.-]+)"\)', html)
    if not lat_match or not lng_match:
        return None

    try:
        return float(lat_match.group(1)), float(lng_match.group(1))
    except ValueError:
        return None


def _expected_city_slug_from_start_url(start_url: str) -> str:
    parsed = urlparse(str(start_url or ""))
    path = str(parsed.path or "")
    marker = "/kluswoning-"
    if marker not in path:
        return ""
    return normalize_klusvastgoed_municipality_slug(path.split(marker, 1)[1])


def _parse_city_overview_label(*, text: str, href: str) -> tuple[str | None, str | None, int | None]:
    normalized = _clean_text(text) or ""
    if not normalized:
        return _municipality_from_slug(href.rsplit("/", 1)[-1]), None, None

    province = None
    province_pattern = sorted(NETHERLANDS_PROVINCES, key=len, reverse=True)
    for candidate in province_pattern:
        if normalized.endswith(f" {candidate}"):
            province = candidate
            normalized = normalized[: -len(candidate)].strip()
            break

    count_match = re.match(r"^(\d+)\s+(?:kluswoningen?|kluswoning(?:en)?)\s+(.*)$", normalized, flags=re.I)
    if count_match:
        count = int(count_match.group(1))
        municipality = _clean_text(count_match.group(2))
        return municipality or _municipality_from_slug(href.rsplit("/", 1)[-1]), province, count

    count_match = re.match(r"^(.*?)(?:\s+(\d+))?$", normalized)
    municipality = _clean_text(count_match.group(1) if count_match else normalized)
    count = int(count_match.group(2)) if count_match and count_match.group(2) else None
    municipality = municipality.replace("kluswoningen", "").replace("kluswoning", "").strip()
    return municipality or _municipality_from_slug(href.rsplit("/", 1)[-1]), province, count


def _municipality_from_slug(slug: str) -> str:
    text = str(slug or "").strip().replace("-", " ")
    text = re.sub(r"\bkluswoning\b", "", text, flags=re.I)
    text = " ".join(text.split()).strip()
    if not text:
        return ""
    return " ".join(part.capitalize() if part not in {"en", "van", "de", "den", "der", "het"} else part for part in text.split())


def _is_supported_listing_path(source_url: str) -> bool:
    parsed = urlparse(str(source_url or ""))
    path = str(parsed.path or "")
    return path.startswith("/pand/") or path.startswith("/koop/")


def _text_from_node(node: object) -> str:
    getter = getattr(node, "get_text", None)
    if callable(getter):
        return str(getter(" ", strip=True))
    return ""


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text or None


def _parse_number(value: object) -> float | None:
    if value in (None, ""):
        return None
    text = str(value)
    match = re.search(r"([0-9][0-9\.,\s]*)", text)
    if not match:
        return None
    cleaned = match.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_price(value: object) -> float | None:
    return _parse_number(value)


def _parse_area(value: object) -> float | None:
    return _parse_number(value)


def _normalize_image_url(node: object) -> str | None:
    getter = getattr(node, "get", None)
    if not callable(getter):
        return None
    src = str(getter("src") or "").strip()
    if not src:
        return None
    if src.startswith("//"):
        return f"https:{src}"
    parsed = urlparse(src)
    if parsed.scheme and parsed.netloc:
        return src
    return urljoin(KLUSVASTGOED_BASE_URL, src)


def _property_type_from_image_alt(node: object) -> str | None:
    getter = getattr(node, "get", None)
    if not callable(getter):
        return None
    alt = _clean_text(str(getter("alt") or ""))
    if not alt:
        return None
    head = alt.split(" in ", 1)[0].strip()
    if not head:
        return None
    if "," in head:
        return _clean_text(head.split(",", 1)[0])
    if head.casefold().endswith(" kluswoning te koop"):
        return "kluswoning"
    return None