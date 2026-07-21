import unittest

from app import (
    _deal_recommendation_from_score,
    _build_rows_from_scan_properties,
    _build_propertyhunter_rows,
    _filter_propertyhunter_rows,
    _latest_scan_metrics,
    _normalize_address_input,
    _propertyhunter_listing_history_text,
    _scan_data_origin_label,
    _score_rows_with_opportunity_intelligence,
    _sort_deal_intelligence_rows,
    _woz_metrics,
    _woz_pct_badge,
)


class PropertyHunterInterfaceHelpersTests(unittest.TestCase):
    def test_normalize_address_input_supports_exact_lookup(self):
        self.assertEqual(_normalize_address_input(" Mathenesserlaan 369 A/B "), "mathenesserlaan 369 a/b")

    def test_scan_data_origin_label_values(self):
        self.assertIn("live scraper", _scan_data_origin_label(latest_scan_result={"mode": "live"}, database_enabled=True))
        self.assertIn("dry-run", _scan_data_origin_label(latest_scan_result={"mode": "dry-run"}, database_enabled=True))
        self.assertIn("Supabase", _scan_data_origin_label(latest_scan_result=None, database_enabled=True))

    def test_woz_metrics_handles_missing_and_calculates_difference(self):
        self.assertEqual(_woz_metrics(None, 400000), (None, None))
        self.assertEqual(_woz_metrics(500000, None), (None, None))
        self.assertEqual(_woz_metrics(500000, 0), (None, None))
        self.assertEqual(_woz_metrics(500000, 400000), (100000.0, 25.0))

    def test_woz_pct_badge_color_thresholds(self):
        self.assertIn("#2E7D32", _woz_pct_badge(-2.0))
        self.assertIn("#EF6C00", _woz_pct_badge(8.0))
        self.assertIn("#C62828", _woz_pct_badge(15.0))
        self.assertIn("Niet beschikbaar", _woz_pct_badge(None))

    def test_deal_recommendation_scale(self):
        self.assertEqual(_deal_recommendation_from_score(90), "★★★★★ Exceptional")
        self.assertEqual(_deal_recommendation_from_score(75), "★★★★ Strong Buy")
        self.assertEqual(_deal_recommendation_from_score(60), "★★★ Consider")
        self.assertEqual(_deal_recommendation_from_score(45), "★★ Weak")
        self.assertEqual(_deal_recommendation_from_score(10), "★ Avoid")

    def test_score_rows_adds_deal_intelligence_pro_fields(self):
        rows = [
            {
                "adres": "Markt 1",
                "plaats": "Breda",
                "vraagprijs": 350000,
                "woonoppervlak": 125,
                "perceel": 190,
                "slaapkamers": 4,
                "energielabel": "C",
                "bouwjaar": 1988,
                "days_on_market": 72,
                "price_reduction_count": 2,
                "price_per_m2": None,
                "investment_score": None,
                "opportunity_score": None,
            },
            {
                "adres": "Markt 2",
                "plaats": "Breda",
                "vraagprijs": 540000,
                "woonoppervlak": 105,
                "perceel": 120,
                "slaapkamers": 2,
                "energielabel": "A",
                "bouwjaar": 2005,
                "days_on_market": 15,
                "price_reduction_count": 0,
                "price_per_m2": None,
                "investment_score": None,
                "opportunity_score": None,
            },
        ]

        scored = _score_rows_with_opportunity_intelligence(rows)
        first = scored[0]

        self.assertIn("city_avg_price_per_m2", first)
        self.assertIn("difference_vs_city_avg_pct", first)
        self.assertIn("split_potential", first)
        self.assertIn("vertical_extension_potential", first)
        self.assertIn("rental_potential", first)
        self.assertIn("renovation_potential", first)
        self.assertIn("overall_investment_score", first)
        self.assertIn("investment_recommendation", first)

        self.assertGreaterEqual(first["split_potential"], 0)
        self.assertLessEqual(first["split_potential"], 100)
        self.assertGreaterEqual(first["vertical_extension_potential"], 0)
        self.assertLessEqual(first["vertical_extension_potential"], 100)
        self.assertGreaterEqual(first["rental_potential"], 0)
        self.assertLessEqual(first["rental_potential"], 100)
        self.assertGreaterEqual(first["renovation_potential"], 0)
        self.assertLessEqual(first["renovation_potential"], 100)

    def test_sort_deal_intelligence_rows_by_new_scores(self):
        rows = [
            {"adres": "A", "split_potential": 30, "rental_potential": 20, "deal_score": 50},
            {"adres": "B", "split_potential": 80, "rental_potential": 60, "deal_score": 70},
            {"adres": "C", "split_potential": 55, "rental_potential": 90, "deal_score": 40},
        ]

        sorted_split = _sort_deal_intelligence_rows(rows, "Hoogste split potential")
        sorted_rental = _sort_deal_intelligence_rows(rows, "Hoogste rental potential")

        self.assertEqual(sorted_split[0]["adres"], "B")
        self.assertEqual(sorted_rental[0]["adres"], "C")

    def test_build_rows_from_scan_properties_populates_missing_fields_from_raw_data(self):
        properties = [
            {
                "property": {
                    "address": "Fallbackstraat 10",
                    "city": "Breda",
                    "asking_price": 325000,
                    "source_url": "https://www.funda.nl/detail/koop/breda/object/123/",
                    "raw_extracted_data": {
                        "living_area": 118,
                        "energy_label": "B",
                        "construction_year": 2001,
                        "plot_size": 164,
                        "bedrooms": 4,
                    },
                }
            }
        ]

        rows = _build_rows_from_scan_properties(properties)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["woonoppervlak"], 118.0)
        self.assertEqual(row["energielabel"], "B")
        self.assertEqual(row["bouwjaar"], 2001)
        self.assertEqual(row["perceel"], 164.0)
        self.assertEqual(row["slaapkamers"], 4)

    def test_build_rows_from_scan_properties_formats_klusvastgoed_source_name(self):
        properties = [
            {
                "property": {
                    "address": "Nystadstraat 11",
                    "city": "Rotterdam",
                    "source_name": "klusvastgoed.nl",
                    "source_url": "https://www.klusvastgoed.nl/koop/rotterdam/nystadstraat-11",
                    "raw_payload": {
                        "living_area": 106,
                    },
                }
            }
        ]

        rows = _build_rows_from_scan_properties(properties)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bron"], "Klusvastgoed")

    def test_opportunity_scoring_is_clamped_and_prefers_better_candidate(self):
        rows = [
            {
                "adres": "Kansstraat 1",
                "plaats": "Breda",
                "vraagprijs": 250000,
                "woonoppervlak": 120,
                "perceel": 180,
                "slaapkamers": 4,
                "energielabel": "A",
                "bouwjaar": 2000,
                "price_reduction_count": 1,
                "price_per_m2": None,
                "investment_score": None,
                "opportunity_score": None,
            },
            {
                "adres": "Duurstraat 2",
                "plaats": "Breda",
                "vraagprijs": 620000,
                "woonoppervlak": 100,
                "perceel": 120,
                "slaapkamers": 2,
                "energielabel": "D",
                "bouwjaar": 1980,
                "price_reduction_count": 0,
                "price_per_m2": None,
                "investment_score": 130,
                "opportunity_score": -10,
            },
        ]

        scored = _score_rows_with_opportunity_intelligence(rows)

        self.assertEqual(scored[0]["investment_score"], 100)
        self.assertGreaterEqual(scored[0]["opportunity_score"], 0)
        self.assertLessEqual(scored[0]["opportunity_score"], 100)

        self.assertEqual(scored[1]["investment_score"], 100)
        self.assertEqual(scored[1]["opportunity_score"], 0)
        self.assertGreater(scored[0]["opportunity_score"], scored[1]["opportunity_score"])

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
