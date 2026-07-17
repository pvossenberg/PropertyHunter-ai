import unittest

from app import _scan_data_origin_label
from deal_finder.sources.funda import normalize_funda_area_slug
from deal_finder.sources.paginated_html import PaginatedHtmlListingAdapter


class _FakeAdapter(PaginatedHtmlListingAdapter):
    source_name = "fake"
    source_type = "portal"
    is_enabled = True
    default_start_url = "https://example.test/start"

    def __init__(self):
        super().__init__()
        self.last_force_refresh_flags: list[bool] = []

    def _extract_listing_urls(self, *, index_url, soup):
        return []

    def _extract_next_page_url(self, *, index_url, soup):
        return None

    def _extract_listing_record(self, *, source_url, html):
        return {}

    def normalize_listing(self, payload):
        return payload

    def _fetch_html(self, url: str, timeout_seconds: float, force_refresh: bool = False) -> str:
        self.last_force_refresh_flags.append(bool(force_refresh))
        return "<html></html>"


class DealFinderLiveRefreshTests(unittest.TestCase):
    def test_normalize_funda_area_slug_handles_special_characters(self):
        self.assertEqual(normalize_funda_area_slug("Rotterdam"), "rotterdam")
        self.assertEqual(normalize_funda_area_slug("Mathenesserlaan 369 A/B"), "mathenesserlaan-369-a-b")
        self.assertEqual(normalize_funda_area_slug("Súdwest-Fryslân"), "sudwest-fryslan")
        self.assertEqual(normalize_funda_area_slug("’s-Hertogenbosch"), "s-hertogenbosch")

    def test_paginated_adapter_propagates_force_refresh(self):
        adapter = _FakeAdapter()
        adapter.fetch_listings(
            {
                "start_url": "https://example.test/start",
                "max_pages": 1,
                "timeout_seconds": 5,
                "force_refresh": True,
            }
        )
        self.assertTrue(adapter.last_force_refresh_flags)
        self.assertTrue(all(adapter.last_force_refresh_flags))

    def test_scan_data_origin_label(self):
        self.assertIn("live scraper", _scan_data_origin_label(latest_scan_result={"mode": "live"}, database_enabled=True))
        self.assertIn("dry-run", _scan_data_origin_label(latest_scan_result={"mode": "dry-run"}, database_enabled=True))
        self.assertIn("Supabase", _scan_data_origin_label(latest_scan_result=None, database_enabled=True))


if __name__ == "__main__":
    unittest.main()
