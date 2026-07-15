import unittest
from datetime import date

from models.comparable_sale import ComparableProperty
from services.comparable_sales import ComparableSalesService


class ComparableSalesTests(unittest.TestCase):
    def setUp(self):
        self.service = ComparableSalesService()

    def test_comparable_property_to_dict_and_from_dict(self):
        comp = ComparableProperty(
            address="Straat 1",
            distance_meters=250.0,
            living_area_m2=85.0,
            asking_price=410000.0,
            sold_price=400000.0,
            sold_date=date(2026, 5, 1),
            price_per_m2=4705.88,
            difference_with_subject_pct=3.5,
        )
        payload = comp.to_dict()
        rebuilt = ComparableProperty.from_dict(payload)
        self.assertEqual(rebuilt.address, "Straat 1")
        self.assertEqual(rebuilt.sold_date, date(2026, 5, 1))
        self.assertEqual(rebuilt.sold_price, 400000.0)

    def test_service_returns_placeholder_comparables_with_required_fields(self):
        comps = self.service.get_comparables(
            {
                "address": "Voorbeeldstraat 10",
                "city": "Amsterdam",
                "asking_price": 500000,
                "surface_m2": 100,
            }
        )
        self.assertGreaterEqual(len(comps), 5)
        for comp in comps:
            self.assertTrue(comp.address)
            self.assertIsNotNone(comp.distance_meters)
            self.assertIsNotNone(comp.living_area_m2)
            self.assertIsNotNone(comp.asking_price)
            self.assertIsNotNone(comp.sold_price)
            self.assertIsNotNone(comp.sold_date)
            self.assertIsNotNone(comp.price_per_m2)
            self.assertTrue(comp.difference_with_subject_pct is None or isinstance(comp.difference_with_subject_pct, float))

    def test_sorting_and_summary_statistics(self):
        comps = self.service.get_comparables(
            {
                "address": "Voorbeeldstraat 10",
                "city": "Amsterdam",
                "asking_price": 500000,
                "surface_m2": 100,
            }
        )
        sorted_desc = self.service.sort_comparables(comps, "price_per_m2", descending=True)
        self.assertGreaterEqual(sorted_desc[0].price_per_m2, sorted_desc[-1].price_per_m2)

        summary = self.service.calculate_summary(comps)
        self.assertIsNotNone(summary["average_price_per_m2"])
        self.assertIsNotNone(summary["median_price_per_m2"])
        self.assertIsNotNone(summary["lowest_comparable"])
        self.assertIsNotNone(summary["highest_comparable"])

    def test_valuation_outputs_expected_keys(self):
        comps = self.service.get_comparables(
            {
                "address": "Voorbeeldstraat 10",
                "city": "Amsterdam",
                "asking_price": 500000,
                "surface_m2": 100,
            }
        )
        valuation = self.service.calculate_valuation(
            subject_asking_price=500000,
            subject_surface_m2=100,
            comparables=comps,
        )
        self.assertIn("estimated_market_value", valuation)
        self.assertIn("recommended_max_bid", valuation)
        self.assertIn("negotiation_margin_pct", valuation)
        self.assertIn("summary", valuation)
        self.assertIsNotNone(valuation["estimated_market_value"])
        self.assertIsNotNone(valuation["recommended_max_bid"])


if __name__ == "__main__":
    unittest.main()