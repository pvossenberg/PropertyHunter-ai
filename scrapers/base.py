from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


USER_AGENT = "Mozilla/5.0 (compatible; PropertyHunterAI/1.0; +https://example.com/propertyhunter)"


@dataclass
class ScrapeResult:
    source_name: str
    source_url: str
    success: bool
    title: str | None = None
    address: str | None = None
    asking_price: float | None = None
    price_status: str | None = None
    living_area: float | None = None
    plot_area: float | None = None
    object_type: str | None = None
    description: str | None = None
    features: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    raw_text: str | None = None
    warnings: list[str] = field(default_factory=list)
    extraction_method: str | None = None
    confidence: float | None = None
    fallback_recommendation: dict[str, Any] | None = None


class BaseScraper:
    source_name = "generic"

    def validate_url(self, url: str) -> str:
        if not url or not url.strip():
            raise ValueError("Geef een URL op.")

        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Gebruik een geldige http(s)-URL.")
        return url.strip()

    def fetch_html(self, url: str) -> str:
        valid_url = self.validate_url(url)
        headers = {"User-Agent": USER_AGENT}

        try:
            response = requests.get(valid_url, headers=headers, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise requests.RequestException(f"De pagina kon niet worden opgehaald: {exc}") from exc

        return response.text

    def scrape(self, url: str) -> ScrapeResult:
        html = self.fetch_html(url)
        return self.parse_public_html(url, html)

    def parse_public_html(self, url: str, html: str) -> ScrapeResult:
        raise NotImplementedError


# Shared extraction helpers

def _normalized_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if cleaned else None


def extract_visible_text(soup: BeautifulSoup) -> str:
    work_soup = BeautifulSoup(str(soup), "html.parser")
    for element in work_soup(["script", "style", "noscript"]):
        element.decompose()
    text = work_soup.get_text(separator=" ", strip=True)
    return " ".join(text.split())


def extract_meta_content(soup: BeautifulSoup, property_name: str) -> str | None:
    tag = soup.find("meta", attrs={"property": property_name})
    if tag and tag.get("content"):
        return _normalized_text(tag.get("content"))

    tag = soup.find("meta", attrs={"name": property_name})
    if tag and tag.get("content"):
        return _normalized_text(tag.get("content"))

    return None


def extract_json_ld_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            blocks.append(parsed)
        elif isinstance(parsed, list):
            blocks.extend([item for item in parsed if isinstance(item, dict)])
    return blocks


def find_first_jsonld_value(blocks: list[dict[str, Any]], keys: list[str]) -> Any:
    for block in blocks:
        for key in keys:
            if key in block and block.get(key) not in (None, ""):
                return block.get(key)

        graph = block.get("@graph")
        if isinstance(graph, list):
            graph_blocks = [item for item in graph if isinstance(item, dict)]
            value = find_first_jsonld_value(graph_blocks, keys)
            if value not in (None, ""):
                return value
    return None


def parse_address_from_jsonld(address_obj: Any) -> str | None:
    if isinstance(address_obj, str):
        return _normalized_text(address_obj)

    if not isinstance(address_obj, dict):
        return None

    parts = [
        address_obj.get("streetAddress"),
        address_obj.get("postalCode"),
        address_obj.get("addressLocality"),
        address_obj.get("addressCountry"),
    ]
    merged = " ".join([str(part).strip() for part in parts if part])
    return _normalized_text(merged)


def parse_price(value: Any) -> tuple[float | None, str | None]:
    if value in (None, ""):
        return None, None

    if isinstance(value, (int, float)):
        return float(value), "known"

    if not isinstance(value, str):
        return None, None

    lowered = value.lower().strip()
    if "prijs op aanvraag" in lowered or lowered == "poa" or "op aanvraag" in lowered:
        return None, "on_request"

    if "vanaf" in lowered:
        number = _extract_first_number(value)
        return number, "from_price"

    if "veiling" in lowered:
        number = _extract_first_number(value)
        return number, "auction"

    if " - " in value or "tot" in lowered:
        return None, "range"

    number = _extract_first_number(value)
    if number is not None:
        return number, "known"

    return None, "unknown"


def _extract_first_number(value: str) -> float | None:
    match = re.search(r"([0-9][0-9\.,\s]*)", value)
    if not match:
        return None

    cleaned = match.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_area_sqm(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    match = re.search(r"([0-9][0-9\.,\s]*)\s*(m2|m²)", value.lower())
    if match:
        return _extract_first_number(match.group(1))
    return _extract_first_number(value)
