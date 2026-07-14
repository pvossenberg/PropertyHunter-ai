from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_TIMEOUT_SECONDS = 6
MAX_RESPONSE_BYTES = 1_000_000
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
GB_SITE_TITLE = "gb makelaars"
LOGGER = logging.getLogger(__name__)


@dataclass
class ListingExtractionResult:
    success: bool
    source_url: str
    title: str | None = None
    address: str | None = None
    postal_code: str | None = None
    city: str | None = None
    asking_price: float | None = None
    surface_m2: float | None = None
    property_type: str | None = None
    description: str | None = None
    images: list[str] = field(default_factory=list)
    extraction_method: str = "none"
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_listing_metadata(source_url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> ListingExtractionResult:
    validation_error = _validate_source_url(source_url)
    if validation_error:
        return ListingExtractionResult(
            success=False,
            source_url=source_url,
            extraction_method="rejected",
            confidence=0.0,
            warnings=[validation_error],
            raw_metadata={},
        )

    response = None
    try:
        response = requests.get(
            source_url,
            headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout_seconds,
            stream=True,
        )
        if response.status_code >= 400:
            return ListingExtractionResult(
                success=False,
                source_url=source_url,
                extraction_method="http_error",
                confidence=0.0,
                warnings=[f"HTTP status {response.status_code}"],
                raw_metadata={"status_code": response.status_code},
            )

        final_url = str(response.url or source_url)
        if _looks_like_login_wall(final_url):
            return ListingExtractionResult(
                success=False,
                source_url=source_url,
                extraction_method="login_wall",
                confidence=0.0,
                warnings=["Redirected to a login wall; metadata extraction skipped."],
                raw_metadata={"final_url": final_url},
            )

        html_text, truncated = _read_response_limited(response, MAX_RESPONSE_BYTES)
        result = _extract_from_html(source_url=source_url, html=html_text, final_url=final_url)
        if truncated:
            result.warnings.append("Response truncated due to size limit.")
        return result
    except requests.Timeout:
        return ListingExtractionResult(
            success=False,
            source_url=source_url,
            extraction_method="timeout",
            confidence=0.0,
            warnings=["Request timed out."],
            raw_metadata={},
        )
    except requests.ConnectionError:
        return ListingExtractionResult(
            success=False,
            source_url=source_url,
            extraction_method="connection_error",
            confidence=0.0,
            warnings=["Host unreachable."],
            raw_metadata={},
        )
    except Exception as error:
        return ListingExtractionResult(
            success=False,
            source_url=source_url,
            extraction_method="parse_error",
            confidence=0.0,
            warnings=[f"Metadata extraction error: {type(error).__name__}"],
            raw_metadata={},
        )
    finally:
        if response is not None:
            response.close()


def _extract_from_html(source_url: str, html: str, final_url: str) -> ListingExtractionResult:
    soup = BeautifulSoup(html or "", "html.parser")
    warnings: list[str] = []

    jsonld_data, jsonld_warnings, jsonld_types = _extract_json_ld(soup)
    warnings.extend(jsonld_warnings)
    og_data = _extract_open_graph(soup, final_url=final_url)
    html_data = _extract_html_fallback(soup, final_url=final_url)

    if _is_gb_makelaars_url(final_url):
        _log_gb_makelaars_debug(
            final_url=final_url,
            jsonld_types=jsonld_types,
            og_title=og_data.get("title"),
            document_title=html_data.get("title"),
            heading_texts=_extract_heading_texts(soup),
            snippets=_collect_keyword_snippets(
                soup.get_text("\n", strip=True),
                ["4837", "woonoppervlakte", "gebruiksoppervlakte", "m²", "Burgemeester"],
            ),
        )

    merged: dict[str, Any] = {}
    extraction_method = "none"
    confidence = 0.0

    for candidate, method, method_confidence in (
        (jsonld_data, "json_ld", 0.9),
        (og_data, "open_graph", 0.7),
        (html_data, "html_fallback", 0.5),
    ):
        contributed = _merge_metadata(merged, candidate)
        if contributed and extraction_method == "none":
            extraction_method = method
            confidence = method_confidence

    slug_title, slug_address = _slug_title_and_address(final_url)

    best_title = _select_best_text_candidate(
        [
            (merged.get("title"), 90),
            (jsonld_data.get("title"), 85),
            (og_data.get("title"), 80),
            (html_data.get("heading"), 88),
            (html_data.get("document_title"), 76),
            (slug_title, 72),
        ]
    )
    cleaned_title = _clean_property_title(best_title)
    if cleaned_title and _title_quality(cleaned_title) > _title_quality(_as_non_empty(merged.get("title"))):
        merged["title"] = cleaned_title

    best_address = _select_best_text_candidate(
        [
            (merged.get("address"), 90),
            (jsonld_data.get("address"), 85),
            (html_data.get("address"), 90),
            (html_data.get("heading"), 88),
            (slug_address, 70),
        ]
    )
    cleaned_address = _clean_property_address(best_address)
    if cleaned_address and _address_quality(cleaned_address) > _address_quality(_as_non_empty(merged.get("address"))):
        merged["address"] = cleaned_address

    if not merged.get("postal_code") and html_data.get("postal_code"):
        merged["postal_code"] = html_data.get("postal_code")
    if not merged.get("city") and html_data.get("city"):
        merged["city"] = html_data.get("city")
    if not merged.get("postal_code") and slug_address:
        merged["postal_code"] = _extract_postal_code(slug_address)
    if not merged.get("city") and slug_address:
        merged["city"] = _extract_city_from_text(slug_address)
    if _is_gb_makelaars_url(final_url):
        if not merged.get("title") and slug_title:
            merged["title"] = slug_title
        if not merged.get("address") and slug_address:
            merged["address"] = slug_address

    success = any(
        merged.get(key)
        for key in ("title", "address", "city", "asking_price", "surface_m2", "description", "images")
    )
    if not success:
        warnings.append("No useful metadata could be extracted.")

    return ListingExtractionResult(
        success=success,
        source_url=source_url,
        title=_as_non_empty(merged.get("title")),
        address=_as_non_empty(merged.get("address")),
        postal_code=_as_non_empty(merged.get("postal_code")),
        city=_as_non_empty(merged.get("city")),
        asking_price=_as_float(merged.get("asking_price")),
        surface_m2=_as_float(merged.get("surface_m2")),
        property_type=_as_non_empty(merged.get("property_type")),
        description=_as_non_empty(merged.get("description")),
        images=_normalize_images(merged.get("images")),
        extraction_method=extraction_method,
        confidence=confidence,
        warnings=warnings,
        raw_metadata={
            "json_ld": jsonld_data,
            "open_graph": og_data,
            "html_fallback": html_data,
            "final_url": final_url,
        },
    )


def _extract_json_ld(soup: BeautifulSoup) -> tuple[dict[str, Any], list[str], list[str]]:
    warnings: list[str] = []
    scripts = soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.IGNORECASE)})
    parsed_nodes: list[dict[str, Any]] = []
    object_types: list[str] = []

    for script in scripts:
        raw_text = script.string or script.get_text() or ""
        raw_text = raw_text.strip()
        if not raw_text:
            continue
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            warnings.append("Malformed JSON-LD block ignored.")
            continue
        parsed_nodes.extend(_collect_jsonld_nodes(payload))
        object_types.extend(_collect_jsonld_types(payload))

    best: dict[str, Any] = {}
    for node in parsed_nodes:
        extracted = _extract_fields_from_jsonld_node(node)
        if _metadata_score(extracted) > _metadata_score(best):
            best = extracted

    return best, warnings, object_types


def _collect_jsonld_nodes(payload: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            nodes.extend(_collect_jsonld_nodes(item))
        return nodes
    if not isinstance(payload, dict):
        return nodes

    nodes.append(payload)
    graph = payload.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            if isinstance(item, dict):
                nodes.append(item)
    return nodes


def _collect_jsonld_types(payload: Any) -> list[str]:
    collected: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            collected.extend(_collect_jsonld_types(item))
        return collected
    if not isinstance(payload, dict):
        return collected

    value = payload.get("@type")
    if isinstance(value, str) and value.strip():
        collected.append(value.strip())
    elif isinstance(value, list):
        collected.extend(str(item).strip() for item in value if str(item).strip())

    graph = payload.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            collected.extend(_collect_jsonld_types(item))
    return [item for item in dict.fromkeys(collected)]


def _extract_fields_from_jsonld_node(node: dict[str, Any]) -> dict[str, Any]:
    address_obj = node.get("address") if isinstance(node.get("address"), dict) else {}
    offers = node.get("offers")
    offer_obj = offers[0] if isinstance(offers, list) and offers and isinstance(offers[0], dict) else offers if isinstance(offers, dict) else {}
    floor_size = node.get("floorSize")
    rooms = node.get("numberOfRooms")

    image_value = node.get("image")
    images: list[str] = []
    if isinstance(image_value, str) and image_value.strip():
        images = [image_value.strip()]
    elif isinstance(image_value, list):
        images = [str(item).strip() for item in image_value if str(item).strip()]

    street_address = _as_non_empty(address_obj.get("streetAddress"))
    postal_code = _as_non_empty(address_obj.get("postalCode"))
    city = _as_non_empty(address_obj.get("addressLocality"))

    return {
        "title": node.get("name") or node.get("headline"),
        "address": _compose_address(street_address, postal_code, city),
        "street_address": street_address,
        "postal_code": postal_code,
        "city": city,
        "asking_price": _parse_numeric_value(offer_obj.get("price")),
        "surface_m2": _parse_numeric_value(_floor_size_value(floor_size)),
        "property_type": _as_non_empty(node.get("@type")),
        "description": node.get("description"),
        "images": images,
        "date_posted": node.get("datePosted"),
        "number_of_rooms": _parse_numeric_value(rooms),
        "price_currency": offer_obj.get("priceCurrency"),
    }


def _floor_size_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("value") or value.get("@value") or value.get("name")
    return value


def _extract_open_graph(soup: BeautifulSoup, final_url: str) -> dict[str, Any]:
    og_title = _meta_content(soup, property_name="og:title")
    og_description = _meta_content(soup, property_name="og:description")
    og_image = _meta_content(soup, property_name="og:image")
    canonical = _link_href(soup, rel="canonical") or final_url

    return {
        "title": og_title,
        "description": og_description,
        "images": [og_image] if og_image else [],
        "canonical_url": canonical,
    }


def _extract_html_fallback(soup: BeautifulSoup, final_url: str) -> dict[str, Any]:
    title = _as_non_empty(soup.title.string if soup.title else None)
    heading = _extract_by_selectors(soup, ["h3.single__title", ".single__title", "h1", "h2", "h3"])
    breadcrumb = _extract_by_selectors(soup, ["nav.breadcrumb", ".breadcrumb", "[class*='breadcrumb']"])
    address = _extract_by_selectors(soup, [
        "[itemprop='streetAddress']",
        "address",
        ".address",
        ".adres",
        ".street-address",
        "[class*='address']",
        "[class*='adres']",
        "[class*='street']",
        "[class*='location']",
    ])
    if not address:
        address = _extract_address_from_heading(heading)
    if not address:
        address = _extract_address_from_text(soup)
    if not address:
        _, slug_address = _slug_title_and_address(final_url)
        address = slug_address

    city = _extract_by_selectors(soup, ["[itemprop='addressLocality']", ".city", "[class*='plaats']", "[class*='city']"])
    if not city:
        city = _extract_city_from_text(heading or address or breadcrumb or soup.get_text(" ", strip=True))
    postal_code = _extract_postal_code((address or heading or "") + " " + soup.get_text(" ", strip=True))
    price_text = _extract_by_selectors(soup, [
        "[itemprop='price']",
        ".price",
        "[class*='price']",
        "[class*='vraagprijs']",
        "[class*='asking']",
        "[class*='koopsom']",
    ])
    surface_text = _extract_surface_text(soup)
    description = _extract_by_selectors(soup, [
        "[itemprop='description']",
        ".description",
        "[class*='omschrijving']",
        "article p",
        ".summary",
        "[class*='intro']",
    ])

    if not title:
        title = heading
    if not title:
        title, _ = _slug_title_and_address(final_url)

    if title:
        title = _clean_property_title(title) or title

    if address and postal_code and city:
        address = _compose_address(_extract_street_from_address(address), postal_code, city) or address

    if not city:
        city = _extract_city_from_text(address or soup.get_text(" ", strip=True))

    return {
        "title": title,
        "heading": heading,
        "breadcrumb": breadcrumb,
        "document_title": title,
        "address": address,
        "postal_code": postal_code,
        "city": city,
        "asking_price": _parse_numeric_value(price_text),
        "surface_m2": _parse_numeric_value(surface_text),
        "description": description,
    }


def _extract_city_from_text(text: str) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None

    match = re.search(r"\b\d{4}\s?[A-Za-z]{2}\s+([A-Za-z][A-Za-z\-\s]{1,60})\b", text)
    if match:
        city_value = match.group(1).strip(" ,.-")
        if city_value:
            return city_value.split(",")[0].strip()

    chunks = [part.strip(" ,.-") for part in text.split(",") if part.strip(" ,.-")]
    if chunks:
        tail = chunks[-1]
        words = [item for item in tail.split() if item]
        if words and all(word.replace("-", "").isalpha() for word in words):
            return " ".join(words)
    return None


def _extract_address_from_text(soup: BeautifulSoup) -> str | None:
    candidate_texts = _extract_multiple_texts(
        soup,
        [
            "[itemprop='streetAddress']",
            "address",
            ".address",
            ".adres",
            ".street-address",
            "[class*='address']",
            "[class*='adres']",
            "[class*='street']",
            "[class*='location']",
        ],
    )

    for line in soup.get_text("\n", strip=True).splitlines():
        normalized = _normalize_whitespace(line)
        if normalized and (_contains_postal_code(normalized) or _looks_like_address_line(normalized)):
            candidate_texts.append(normalized)

    best = None
    best_score = -1
    for candidate in candidate_texts:
        score = _address_quality(candidate)
        if score > best_score:
            best = _normalize_whitespace(candidate)
            best_score = score
    return best


def _extract_address_from_heading(heading: str | None) -> str | None:
    normalized = _normalize_whitespace(heading)
    if not normalized:
        return None
    if _contains_postal_code(normalized):
        return normalized
    if _looks_like_address_line(normalized):
        return normalized
    return None


def _extract_surface_text(soup: BeautifulSoup) -> str | None:
    labeled_rows = _extract_labeled_rows(soup, [
        r"woonoppervlakte",
        r"gebruiksoppervlakte(?:\s+wonen)?",
        r"oppervlakte",
        r"m²\s*wonen",
        r"m2\s*wonen",
    ])
    for candidate in labeled_rows:
        normalized = _normalize_whitespace(candidate)
        if normalized and any(char.isdigit() for char in normalized) and len(normalized.split()) <= 8:
            return normalized

    candidate_texts = _extract_multiple_texts(
        soup,
        [
            "[itemprop='floorSize']",
            ".surface",
            "[class*='oppervlakte']",
            "[class*='surface']",
            "[class*='m2']",
            "[class*='woonoppervlakte']",
            "[class*='gebruiksoppervlakte']",
        ],
    )
    candidate_texts.extend(labeled_rows)

    keywords = (
        "woonoppervlakte",
        "gebruiksoppervlakte wonen",
        "gebruiksoppervlakte",
        "m² wonen",
        "m2 wonen",
        "oppervlakte",
    )
    for line in soup.get_text("\n", strip=True).splitlines():
        normalized = _normalize_whitespace(line)
        if normalized and any(keyword in normalized.lower() for keyword in keywords) and len(normalized.split()) <= 12:
            candidate_texts.append(normalized)

    best = None
    best_score = -1
    for candidate in candidate_texts:
        normalized = _normalize_whitespace(candidate)
        if not normalized or not any(char.isdigit() for char in normalized):
            continue
        if len(normalized.split()) > 12:
            continue
        score = 1
        lowered = normalized.lower()
        if any(keyword in lowered for keyword in keywords):
            score += 4
        if re.search(r"\b\d+(?:[\.,]\d+)?\s?(?:m2|m²|m\u00b2|sqm)\b", lowered):
            score += 3
        if score > best_score:
            best = normalized
            best_score = score
    return best


def _extract_labeled_rows(soup: BeautifulSoup, label_patterns: list[str]) -> list[str]:
    texts: list[str] = []
    for pattern in label_patterns:
        regex = re.compile(pattern, re.IGNORECASE)
        for node in soup.find_all(string=regex):
            parent = getattr(node, "parent", None)
            if not parent:
                continue
            row = parent.parent if parent.parent and parent.parent.name == "div" else parent
            row_text = _normalize_whitespace(row.get_text(" ", strip=True))
            if row_text and row_text not in texts:
                texts.append(row_text)
    return texts


def _extract_multiple_texts(soup: BeautifulSoup, selectors: list[str]) -> list[str]:
    texts: list[str] = []
    for selector in selectors:
        for node in soup.select(selector):
            text = _normalize_whitespace(node.get_text(" ", strip=True))
            if text and text not in texts:
                texts.append(text)
    return texts


def _normalize_whitespace(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()).strip()


def _contains_postal_code(text: str) -> bool:
    return bool(re.search(r"\b\d{4}\s?[A-Za-z]{2}\b", text or ""))


def _normalize_postal_code(text: str | None) -> str | None:
    match = re.search(r"\b(\d{4})\s?([A-Za-z]{2})\b", text or "")
    if not match:
        return None
    return f"{match.group(1)} {match.group(2).upper()}"


def _looks_like_address_line(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    lowered = _normalize_whitespace(text).lower()
    if lowered == GB_SITE_TITLE:
        return False
    if len(_normalize_whitespace(text).split()) > 12:
        return False
    if _contains_postal_code(text):
        return True
    return bool(re.search(r"\b\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?\b", text)) and bool(re.search(r"\b[A-Z][a-z]+\b", text))


def _address_quality(text: str | None) -> int:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return -1000
    score = 0
    if _contains_postal_code(normalized):
        score += 3
    if re.search(r"\b\d+[A-Za-z]?(?:-\d+[A-Za-z]?)?\b", normalized):
        score += 2
    if "," in normalized:
        score += 1
    if len(normalized.split()) >= 3:
        score += 1
    return score


def _compose_address(street: str | None, postal_code: str | None, city: str | None) -> str | None:
    normalized_postal_code = _normalize_postal_code(postal_code)
    parts = [_normalize_whitespace(part) for part in (street, normalized_postal_code, city)]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return ", ".join(parts)


def _extract_street_from_address(address: str | None) -> str | None:
    normalized = _normalize_whitespace(address)
    if not normalized:
        return None
    postal_match = re.search(r"\b\d{4}\s?[A-Za-z]{2}\b", normalized)
    if postal_match:
        street = normalized[: postal_match.start()].rstrip(", ")
        return _normalize_whitespace(street) or normalized
    return normalized


def _clean_property_title(text: str | None) -> str | None:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return None
    if normalized.lower() == GB_SITE_TITLE:
        return None
    normalized = re.sub(r"^\s*GB Makelaars\s*[\-|\|]\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*[-|]\s*GB Makelaars\s*$", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*,\s*\d{4}\s?[A-Za-z]{2}\s+[A-Za-z][A-Za-z\-\s]{1,60}$", "", normalized)
    normalized = re.sub(r"\s+\d{4}\s?[A-Za-z]{2}\s+[A-Za-z][A-Za-z\-\s]{1,60}$", "", normalized)
    normalized = normalized.rstrip(", -")
    return normalized or None


def _clean_property_address(text: str | None) -> str | None:
    normalized = _normalize_whitespace(text)
    if not normalized:
        return None
    if normalized.lower() == GB_SITE_TITLE:
        return None
    normalized = re.sub(r"\s*[-|]\s*GB Makelaars\s*$", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+\b(?:Kijkt net verder|Kijkt\s+net\s+verder)\b.*$", "", normalized, flags=re.IGNORECASE)
    postal_code = _normalize_postal_code(normalized)
    if postal_code:
        normalized = re.sub(r"\b\d{4}\s?[A-Za-z]{2}\b", postal_code, normalized)
    return normalized.rstrip(", -") or None


def _slug_title_and_address(final_url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(final_url or "")
    slug = (parsed.path or "").strip("/").split("/")[-1]
    if not slug:
        return None, None
    slug = slug.strip().lower()
    if not slug or slug in {"woning", "object", "aanbod", "listing"}:
        return None, None

    slug = slug.replace("--", "-")
    match = re.match(r"^(?P<street>.+?)-(?P<number>\d+[a-z]?)(?:-(?P<suffix>\d+[a-z]?))?$", slug)
    if match:
        street = _slug_to_title_case(match.group("street").replace("-", " "))
        number = match.group("number").upper()
        suffix = match.group("suffix")
        if suffix:
            address = f"{street} {number}-{suffix.upper()}"
            title = f"{street} {number}"
        else:
            address = f"{street} {number}"
            title = address
        return title, address

    title = _slug_to_title_case(slug.replace("-", " "))
    return title, title


def _slug_to_title_case(text: str) -> str:
    particles = {"de", "den", "der", "het", "van", "von", "en", "op", "te", "ter", "ten"}
    words = [part for part in text.split() if part]
    formatted: list[str] = []
    for index, word in enumerate(words):
        if index > 0 and word.lower() in particles:
            formatted.append(word.lower())
        else:
            formatted.append(word[:1].upper() + word[1:].lower())
    return " ".join(formatted)


def _extract_heading_texts(soup: BeautifulSoup) -> list[str]:
    texts: list[str] = []
    for selector in ["h3.single__title", ".single__title", "h1", "h2", "h3"]:
        for node in soup.select(selector):
            text = _normalize_whitespace(node.get_text(" ", strip=True))
            if text and text not in texts:
                texts.append(text)
    return texts


def _collect_keyword_snippets(text: str, keywords: list[str], max_snippets: int = 3) -> dict[str, list[str]]:
    snippets: dict[str, list[str]] = {}
    for keyword in keywords:
        keyword_snippets: list[str] = []
        for line in text.splitlines():
            normalized = _normalize_whitespace(line)
            if normalized and keyword.lower() in normalized.lower():
                keyword_snippets.append(normalized[:220])
            if len(keyword_snippets) >= max_snippets:
                break
        snippets[keyword] = keyword_snippets
    return snippets


def _is_gb_makelaars_url(final_url: str) -> bool:
    host = (urlparse(final_url or "").hostname or "").lower()
    return "gbmakelaars" in host


def _log_gb_makelaars_debug(
    *,
    final_url: str,
    jsonld_types: list[str],
    og_title: str | None,
    document_title: str | None,
    heading_texts: list[str],
    snippets: dict[str, list[str]],
) -> None:
    LOGGER.debug(
        "GB Makelaars metadata debug final_url=%s jsonld_types=%s og:title=%r document_title=%r headings=%s snippets=%s",
        final_url,
        jsonld_types,
        og_title,
        document_title,
        heading_texts,
        snippets,
    )


def _select_best_text_candidate(candidates: list[tuple[Any, int]]) -> str | None:
    best_value = None
    best_score = -1000
    for value, weight in candidates:
        normalized = _normalize_whitespace(value)
        if not normalized:
            continue
        score = weight
        lowered = normalized.lower()
        if lowered == GB_SITE_TITLE:
            score -= 100
        if any(keyword in lowered for keyword in ("te koop", "winkelpand", "kantoor", "bedrijfspand", "bedrijfsruimte", "horeca", "woning", "appartement")):
            score += 5
        if _contains_postal_code(normalized):
            score += 2
        if re.search(r"\b\d+[A-Za-z]?\b", normalized):
            score += 1
        if len(normalized) > 20:
            score += 1
        if score > best_score:
            best_value = normalized
            best_score = score
    return best_value


def _title_quality(text: str | None) -> int:
    normalized = _normalize_whitespace(text).lower()
    if not normalized:
        return -1000
    score = len(normalized.split())
    if normalized == GB_SITE_TITLE:
        return -100
    if normalized.startswith(f"{GB_SITE_TITLE} |") or normalized.endswith(f"| {GB_SITE_TITLE}"):
        score -= 2
    if any(keyword in normalized for keyword in ("te koop", "winkelpand", "kantoor", "bedrijfspand", "bedrijfsruimte", "horeca", "woning", "appartement")):
        score += 5
    if _contains_postal_code(normalized):
        score += 2
    if re.search(r"\b\d+[A-Za-z]?\b", normalized):
        score += 1
    if len(normalized) > 20:
        score += 1
    return score


def _extract_by_selectors(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return None


def _extract_postal_code(text: str) -> str | None:
    return _normalize_postal_code(text)


def _meta_content(soup: BeautifulSoup, property_name: str) -> str | None:
    node = soup.find("meta", attrs={"property": property_name})
    if node and node.get("content"):
        return str(node.get("content")).strip()
    return None


def _link_href(soup: BeautifulSoup, rel: str) -> str | None:
    node = soup.find("link", attrs={"rel": rel})
    if node and node.get("href"):
        return str(node.get("href")).strip()
    return None


def _read_response_limited(response: requests.Response, max_bytes: int) -> tuple[str, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            allowed = max_bytes - (total - len(chunk))
            if allowed > 0:
                chunks.append(chunk[:allowed])
            truncated = True
            break
        chunks.append(chunk)
    data = b"".join(chunks)
    encoding = response.encoding or "utf-8"
    return data.decode(encoding, errors="ignore"), truncated


def _validate_source_url(source_url: str) -> str | None:
    try:
        parsed = urlparse(source_url)
    except Exception:
        return "Invalid URL."

    if parsed.scheme not in {"http", "https"}:
        return "Only http/https URLs are allowed."

    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL hostname is missing."

    if host in LOCAL_HOSTNAMES or host.endswith(".local"):
        return "Local/private hosts are not allowed."

    try:
        ip_value = ipaddress.ip_address(host)
    except ValueError:
        ip_value = None

    if ip_value is not None:
        if ip_value.is_private or ip_value.is_loopback or ip_value.is_link_local or ip_value.is_multicast or ip_value.is_reserved or ip_value.is_unspecified:
            return "Local/private IP addresses are not allowed."

    return None


def _looks_like_login_wall(url: str) -> bool:
    lowered = (url or "").lower()
    return any(token in lowered for token in ("/login", "signin", "inloggen", "auth"))


def _merge_metadata(target: dict[str, Any], incoming: dict[str, Any]) -> bool:
    method_fields = {"title", "address", "postal_code", "city", "asking_price", "surface_m2", "property_type", "description", "images"}
    changed = False
    for key, value in incoming.items():
        if key == "images":
            merged_images = _normalize_images(target.get("images"))
            for image in _normalize_images(value):
                if image not in merged_images:
                    merged_images.append(image)
                    if key in method_fields:
                        changed = True
            if merged_images:
                target["images"] = merged_images
            continue

        normalized = _as_non_empty(value)
        if key in {"asking_price", "surface_m2", "number_of_rooms"}:
            numeric = _as_float(value)
            if numeric is not None and target.get(key) in (None, ""):
                target[key] = numeric
                if key in method_fields:
                    changed = True
            continue

        if normalized and target.get(key) in (None, ""):
            target[key] = normalized
            if key in method_fields:
                changed = True
    return changed


def _normalize_images(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out
    return []


def _metadata_score(data: dict[str, Any]) -> int:
    score = 0
    for key in ("title", "address", "city", "asking_price", "surface_m2", "description"):
        if data.get(key) not in (None, ""):
            score += 1
    return score


def _as_non_empty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_numeric_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.strip().lower()
    if not text:
        return None

    tokens = re.findall(r"[-+]?\d[\d\.,]*", text)
    if not tokens:
        return None
    cleaned = tokens[0]
    if not cleaned:
        return None

    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(",") > 0 and cleaned.count(".") > 0:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") > 0 and cleaned.count(".") == 0:
        cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None
