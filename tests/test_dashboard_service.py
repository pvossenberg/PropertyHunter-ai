from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from services.dashboard_service import DashboardService


class FakeDatabaseService:
    def __init__(self, scan_runs: list[dict], sources: list[dict], candidates: list[dict]) -> None:
        self.is_enabled = True
        self._scan_runs = scan_runs
        self._sources = sources
        self._candidates = candidates

    def _fetch_rows(self, table_name: str, **kwargs):
        if table_name == "scan_runs":
            return list(self._scan_runs)
        if table_name == "listing_sources":
            return list(self._sources)
        return []

    def list_deal_candidates(self, **kwargs):
        return list(self._candidates)


class DashboardServiceTests(unittest.TestCase):
    def test_load_latest_dashboard_result_from_database(self) -> None:
        scan_runs = [
            {
                "id": "run-1",
                "source_id": "source-1",
                "started_at": "2026-07-15T14:00:00+00:00",
                "completed_at": "2026-07-15T14:10:00+00:00",
                "status": "completed",
                "items_found": 2,
                "items_new": 1,
                "items_changed": 1,
                "metadata": {"source_stats": {"failed_listings": 0}},
            }
        ]
        sources = [{"id": "source-1", "name": "funda.nl"}]
        candidates = [
            {
                "listing_id": "listing-2",
                "investment_score": 74,
                "hidden_value_score": 91,
                "detected_at": "2026-07-15T14:06:00+00:00",
                "listing": {
                    "id": "listing-2",
                    "source_url": "https://example.com/listing-2",
                    "address": "Secondstraat 2",
                    "city": "Utrecht",
                    "asking_price": 450000,
                    "days_on_market": 9,
                    "source_id": "source-1",
                    "raw_payload": {
                        "surface_m2": 110,
                        "plot_size_m2": 160,
                        "bedrooms": 4,
                        "energy_label": "A",
                        "construction_year": 1998,
                        "bag_id": "0758010000018784",
                        "latest_woz_value": 400000,
                        "woz_valuation_year": 2025,
                        "woz_source": "Kadaster WOZ-waardeloket",
                        "woz_retrieval_date": "2026-07-16T12:00:00+00:00",
                    },
                },
                "source": {"name": "funda.nl"},
            },
            {
                "listing_id": "listing-1",
                "investment_score": 60,
                "hidden_value_score": 50,
                "detected_at": "2026-07-15T14:05:00+00:00",
                "listing": {
                    "id": "listing-1",
                    "source_url": "https://example.com/listing-1",
                    "address": "Eersteweg 1",
                    "city": "Amersfoort",
                    "asking_price": 300000,
                    "days_on_market": 12,
                    "source_id": "source-1",
                    "raw_payload": {
                        "surface_m2": 85,
                        "plot_size_m2": 100,
                        "bedrooms": 3,
                        "energy_label": "B",
                        "construction_year": 2004,
                    },
                },
                "source": {"name": "funda.nl"},
            },
            {
                "listing_id": "listing-outside-window",
                "investment_score": 99,
                "hidden_value_score": 99,
                "detected_at": "2026-07-15T13:59:59+00:00",
                "listing": {"id": "listing-outside-window", "source_url": "https://example.com/outside", "source_id": "source-1"},
                "source": {"name": "funda.nl"},
            },
        ]

        service = DashboardService(database_service=FakeDatabaseService(scan_runs, sources, candidates))
        result = service.load_latest_dashboard_result()

        self.assertEqual(result.scan_timestamp, "2026-07-15T14:10:00+00:00")
        self.assertEqual(result.source_names, ["funda.nl"])
        self.assertEqual(result.listings_found, 2)
        self.assertEqual(result.new_listings, 1)
        self.assertEqual(result.changed_listings, 1)
        self.assertEqual(result.failed_listings, 0)
        self.assertEqual(result.average_investment_score, 67.0)
        self.assertEqual(result.average_opportunity_score, 70.5)
        self.assertEqual([row.listing_id for row in result.top_properties], ["listing-2", "listing-1"])
        self.assertEqual(result.top_properties[0].living_area, 110.0)
        self.assertEqual(result.top_properties[0].plot_size, 160.0)
        self.assertEqual(result.top_properties[0].energy_label, "A")
        self.assertEqual(result.top_properties[0].construction_year, 1998)
        self.assertEqual(result.top_properties[0].source_name, "funda.nl")
        self.assertEqual(result.top_properties[0].bag_id, "0758010000018784")
        self.assertEqual(result.top_properties[0].woz_value, 400000.0)
        self.assertEqual(result.top_properties[0].woz_valuation_year, 2025)
        self.assertEqual(result.top_properties[0].asking_price_minus_woz_value, 50000.0)
        self.assertEqual(result.top_properties[0].asking_price_vs_woz_percentage, 12.5)

    def test_load_latest_dashboard_result_from_json_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scan_dir = Path(temp_dir) / "output" / "scan-runs"
            scan_dir.mkdir(parents=True)

            older_payload = {
                "source": "old-source",
                "summary": {"listings_found": 1, "listings_imported": 1, "partially_imported": 1, "listings_failed": 0},
                "properties": [],
            }
            newer_payload = {
                "source": "funda",
                "summary": {"listings_found": 2, "listings_imported": 2, "partially_imported": 1, "listings_failed": 0},
                "properties": [
                    {
                        "property": {
                            "listing_id": "json-1",
                            "address": "Janstraat 1",
                            "city": "Leiden",
                            "asking_price": 500000,
                            "surface_m2": 95,
                            "plot_size_m2": 120,
                            "bedrooms": 4,
                            "energy_label": "A+",
                            "construction_year": 2001,
                            "days_on_market": 7,
                            "source_url": "https://example.com/json-1",
                            "bag_id": "0758010000011111",
                            "latest_woz_value": 480000,
                            "woz_valuation_year": 2025,
                            "woz_source": "Kadaster WOZ-waardeloket",
                            "woz_retrieval_date": "2026-07-16T12:00:00+00:00",
                        },
                        "investment_score": 81,
                        "opportunity_score": 77,
                    },
                    {
                        "property_for_database": {
                            "listing_id": "json-2",
                            "address": "Kade 2",
                            "city": "Gouda",
                            "asking_price": 320000,
                            "surface_m2": 70,
                            "plot_size_m2": 90,
                            "bedrooms": 2,
                            "energy_label": "C",
                            "construction_year": 1988,
                            "days_on_market": 11,
                            "source_url": "https://example.com/json-2",
                        }
                    },
                ],
            }

            older_file = scan_dir / "funda_scan_20260715_130000.json"
            newer_file = scan_dir / "funda_scan_20260715_144923.json"
            older_file.write_text(json.dumps(older_payload), encoding="utf-8")
            newer_file.write_text(json.dumps(newer_payload), encoding="utf-8")

            older_time = datetime(2026, 7, 15, 13, 0, 0, tzinfo=timezone.utc).timestamp()
            newer_time = datetime(2026, 7, 15, 14, 49, 23, tzinfo=timezone.utc).timestamp()
            # Preserve a deterministic latest file choice by setting modification times.
            os.utime(older_file, (older_time, older_time))
            os.utime(newer_file, (newer_time, newer_time))

            service = DashboardService(database_service=type("DisabledDB", (), {"is_enabled": False})(), scan_runs_dir=scan_dir)
            result = service.load_latest_dashboard_result()

            self.assertEqual(result.scan_timestamp, "2026-07-15T14:49:23+00:00")
            self.assertEqual(result.source_names, ["funda"])
            self.assertEqual(result.listings_found, 2)
            self.assertEqual(result.new_listings, 2)
            self.assertEqual(result.failed_listings, 0)
            self.assertEqual([row.listing_id for row in result.top_properties], ["json-1", "json-2"])
            self.assertEqual(result.top_properties[0].source_url, "https://example.com/json-1")
            self.assertEqual(result.top_properties[0].investment_score, 81)
            self.assertEqual(result.top_properties[0].opportunity_score, 77)
            self.assertEqual(result.top_properties[0].bag_id, "0758010000011111")
            self.assertEqual(result.top_properties[0].woz_value, 480000.0)
            self.assertEqual(result.top_properties[0].woz_valuation_year, 2025)
            self.assertEqual(result.top_properties[0].asking_price_minus_woz_value, 20000.0)
            self.assertEqual(result.top_properties[0].asking_price_vs_woz_percentage, 4.17)


if __name__ == "__main__":
    unittest.main()