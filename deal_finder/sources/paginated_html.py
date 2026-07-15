from __future__ import annotations

from abc import abstractmethod
import logging
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from deal_finder.sources.base import ListingSourceAdapter, SourceRecordResult

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_MAX_PAGES = 1000
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; PropertyHunterAI-DealFinder/1.0)"


class PaginatedHtmlListingAdapter(ListingSourceAdapter):
    default_start_url: str = ""

    def __init__(self):
        self._last_fetch_stats = {
            "records_found": 0,
            "records_imported": 0,
            "records_skipped": 0,
            "records_failed": 0,
            "listings_found": 0,
            "listings_imported": 0,
            "duplicates_skipped": 0,
            "failed_listings": 0,
        }

    def validate_configuration(self, configuration: dict[str, Any]) -> tuple[bool, list[str]]:
        if not isinstance(configuration, dict):
            return False, ["Configuration must be an object."]

        warnings: list[str] = []
        start_url = str(configuration.get("start_url") or self.default_start_url).strip()
        if not start_url:
            warnings.append("start_url is required")
        elif not start_url.startswith(("http://", "https://")):
            warnings.append("start_url must start with http:// or https://")

        max_pages = configuration.get("max_pages", DEFAULT_MAX_PAGES)
        try:
            if int(max_pages) <= 0:
                warnings.append("max_pages must be > 0")
        except (TypeError, ValueError):
            warnings.append("max_pages must be an integer")

        timeout_seconds = configuration.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        try:
            if float(timeout_seconds) <= 0:
                warnings.append("timeout_seconds must be > 0")
        except (TypeError, ValueError):
            warnings.append("timeout_seconds must be numeric")

        return len(warnings) == 0, warnings

    def discover_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        return self.fetch_listings(configuration)

    def fetch_listing_details(self, listing_ref: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
        source_url = str((listing_ref or {}).get("source_url") or "").strip()
        if not source_url:
            return dict(listing_ref or {})

        timeout_seconds = _to_float(configuration.get("timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS, minimum=1.0, maximum=60.0)
        html = self._fetch_html(source_url, timeout_seconds=timeout_seconds)
        return self._extract_listing_record(source_url=source_url, html=html)

    def fetch_listings(self, configuration: dict[str, Any]) -> list[dict[str, Any]]:
        ok, warnings = self.validate_configuration(configuration)
        if not ok:
            raise ValueError("; ".join(warnings) or "Invalid configuration")

        start_url = str(configuration.get("start_url") or self.default_start_url).strip()
        max_pages = _to_int(configuration.get("max_pages"), default=DEFAULT_MAX_PAGES, minimum=1, maximum=1000)
        timeout_seconds = _to_float(configuration.get("timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS, minimum=1.0, maximum=60.0)

        records: list[dict[str, Any]] = []
        visited_page_urls: set[str] = set()
        visited_listing_urls: set[str] = set()

        records_found = 0
        records_imported = 0
        records_skipped = 0
        records_failed = 0

        next_url = start_url
        page_index = 0

        while next_url and page_index < max_pages:
            if next_url in visited_page_urls:
                break
            visited_page_urls.add(next_url)

            page_index += 1
            try:
                html = self._fetch_html(next_url, timeout_seconds=timeout_seconds)
                soup = BeautifulSoup(html, "html.parser")
            except Exception as error:
                LOGGER.warning("%s page fetch failed url=%s error=%s", self.source_name, next_url, error)
                records_failed += 1
                break

            listing_urls = self._extract_listing_urls(index_url=next_url, soup=soup)
            for listing_url in listing_urls:
                records_found += 1
                if listing_url in visited_listing_urls:
                    records_skipped += 1
                    continue
                visited_listing_urls.add(listing_url)

                try:
                    listing_html = self._fetch_html(listing_url, timeout_seconds=timeout_seconds)
                    listing_record = self._extract_listing_record(source_url=listing_url, html=listing_html)
                    records.append(listing_record)
                    records_imported += 1
                except Exception as error:
                    records_failed += 1
                    LOGGER.warning("%s listing failed url=%s error=%s", self.source_name, listing_url, error)
                    continue

            resolved_next = self._extract_next_page_url(index_url=next_url, soup=soup)
            if not resolved_next or resolved_next == next_url:
                break
            next_url = resolved_next

        self._last_fetch_stats = {
            "records_found": records_found,
            "records_imported": records_imported,
            "records_skipped": records_skipped,
            "records_failed": records_failed,
            "listings_found": records_found,
            "listings_imported": records_imported,
            "duplicates_skipped": records_skipped,
            "failed_listings": records_failed,
        }
        LOGGER.info(
            "%s fetch stats listings_found=%s listings_imported=%s duplicates_skipped=%s failed_listings=%s",
            self.source_name,
            records_found,
            records_imported,
            records_skipped,
            records_failed,
        )
        return records

    def load_and_normalize_listings(self, configuration: dict[str, Any]) -> list[SourceRecordResult]:
        raw_records = self.fetch_listings(configuration)
        results = self.normalize_records(raw_records)
        self._log_connector_stats(results)
        return results

    def _log_connector_stats(self, results: list[SourceRecordResult]) -> None:
        records_found = self._last_fetch_stats.get("records_found") or len(results)
        records_imported = sum(1 for item in results if item.success)
        records_failed = sum(1 for item in results if not item.success and item.error)
        records_skipped = self._last_fetch_stats.get("records_skipped") or max(0, records_found - records_imported - records_failed)
        LOGGER.info(
            "%s connector stats listings_found=%s listings_imported=%s duplicates_skipped=%s failed_listings=%s",
            self.source_name,
            records_found,
            records_imported,
            records_skipped,
            records_failed,
        )

    def _fetch_html(self, url: str, timeout_seconds: float) -> str:
        response = requests.get(
            url,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def _absolute_url(self, base_url: str, href: str | None) -> str | None:
        if not isinstance(href, str) or not href.strip():
            return None
        return urljoin(base_url, href.strip())

    @abstractmethod
    def _extract_listing_urls(self, *, index_url: str, soup: BeautifulSoup) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def _extract_next_page_url(self, *, index_url: str, soup: BeautifulSoup) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def _extract_listing_record(self, *, source_url: str, html: str) -> dict[str, Any]:
        raise NotImplementedError


def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _to_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))