import unittest

from app import (
    _build_propertyhunter_rows,
    _filter_propertyhunter_rows,
    _latest_scan_metrics,
    _propertyhunter_listing_history_text,
)


class PropertyHunterInterfaceHelpersTests(unittest.TestCase):
    def test_build_rows_maps_required_columns_from_listing_and_payload(self):
        candidates = [
            {
                "investment_score": 78,
                "hidden_value_score": 84,
                "listing": {
                    "id": "listing-1",
                    "address": "Voorbeeldstraat 1",
                    "city": "Amsterdam",
                    "asking_price": 525000,
                    "surface_m2": 95,
                    "days_on_market": 21,
                    "listing_status": "active",
                    "source_url": "https://www.funda.nl/detail/koop/amsterdam/object/123/",
                    "raw_payload": {
                        "plot_size_m2": 120,
                        "bedrooms": 3,
                        "energy_label": "A",
                        "construction_year": 1998,
                    },
                },
                "source": {"name": "funda.nl"},
            }
        ]

        rows = _build_propertyhunter_rows(candidates)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["listing_id"], "listing-1")
        self.assertEqual(row["adres"], "Voorbeeldstraat 1")
        self.assertEqual(row["plaats"], "Amsterdam")
        self.assertEqual(row["vraagprijs"], 525000.0)
        self.assertEqual(row["woonoppervlak"], 95.0)
        self.assertEqual(row["perceel"], 120.0)
        self.assertEqual(row["slaapkamers"], 3)
        self.assertEqual(row["energielabel"], "A")
        self.assertEqual(row["bouwjaar"], 1998)
        self.assertEqual(row["days_on_market"], 21)
        self.assertEqual(row["investment_score"], 78)
        self.assertEqual(row["opportunity_score"], 84)
        self.assertEqual(row["bron"], "funda.nl")

    def test_filter_rows_applies_all_filter_dimensions(self):
        rows = [
            {
                "listing_id": "a",
                "plaats": "Amsterdam",
                "vraagprijs": 500000,
                "woonoppervlak": 100,
                "energielabel": "A",
                "opportunity_score": 80,
            },
            {
                "listing_id": "b",
                "plaats": "Rotterdam",
                "vraagprijs": 350000,
                "woonoppervlak": 75,
                "energielabel": "C",
                "opportunity_score": 55,
            },
        ]

        filtered = _filter_propertyhunter_rows(
            rows,
            place="Amsterdam",
            min_price=400000,
            max_price=600000,
            min_surface=90,
            energy_label="A",
            min_opportunity_score=70,
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["listing_id"], "a")

    def test_latest_scan_metrics_returns_values_from_most_recent_run(self):
        payload = {
            "latest_scan_runs": [
                {
                    "started_at": "2026-07-15T11:00:00+00:00",
                    "items_found": 10,
                    "items_new": 3,
                    "items_changed": 1,
                },
                {
                    "started_at": "2026-07-15T12:00:00+00:00",
                    "items_found": 15,
                    "items_new": 5,
                    "items_changed": 4,
                },
            ]
        }

        metrics = _latest_scan_metrics(payload)

        self.assertEqual(metrics["found"], 15)
        self.assertEqual(metrics["new"], 5)
        self.assertEqual(metrics["changed"], 4)

    def test_listing_history_text_prefers_reduction_then_days(self):
        listing_with_reduction = {
            "listing_status": "active",
            "total_price_reduction_percentage": 12.4,
            "days_on_market": 50,
        }
        listing_without_reduction = {
            "listing_status": "active",
            "days_on_market": 50,
        }

        text_reduction = _propertyhunter_listing_history_text(listing_with_reduction)
        text_days = _propertyhunter_listing_history_text(listing_without_reduction)

        self.assertEqual(text_reduction, "status=active, 12% prijsdaling")
        self.assertEqual(text_days, "status=active, 50 dagen")


if __name__ == "__main__":
    unittest.main()
