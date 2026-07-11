import unittest
from datetime import date, datetime, timezone

from models.area_development import AreaDevelopmentRecord
from models.government_record import GovernmentRecord
from models.neighborhood import NeighborhoodProfile
from models.news_record import NewsRecord
from services.location_service import LocationService


class LocationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LocationService()

    def test_valid_address(self):
        result = self.service.get_location_summary("Keizersgracht 123 Amsterdam")
        self.assertTrue(result["is_mock_data"])
        self.assertIsNotNone(result["neighborhood_profile"])

    def test_empty_address(self):
        result = self.service.get_location_summary("  ")
        self.assertEqual(result["confidence"], "unknown")
        self.assertIsNone(result["neighborhood_profile"])

    def test_unknown_neighborhood(self):
        result = self.service.get_neighborhood_profile("Onbekendpad 999 Delft")
        self.assertIsNone(result["neighborhood_profile"])
        self.assertIn("No neighborhood profile found for this address in current provider data.", result["data_quality_warnings"])

    def test_empty_developments_list(self):
        result = self.service.get_area_developments("Coolsingel 50 Rotterdam")
        self.assertEqual(result["area_developments"], [])

    def test_positive_development(self):
        result = self.service.get_area_developments("Keizersgracht 123 Amsterdam")
        has_positive = any(item.impact_direction == "positive" for item in result["area_developments"])
        self.assertTrue(has_positive)

    def test_negative_development(self):
        result = self.service.get_area_developments("Keizersgracht 123 Amsterdam")
        has_negative = any(item.impact_direction == "negative" for item in result["area_developments"])
        self.assertTrue(has_negative)

    def test_government_record(self):
        result = self.service.get_government_records("Keizersgracht 123 Amsterdam")
        self.assertGreaterEqual(len(result["government_records"]), 1)
        self.assertEqual(result["government_records"][0].governing_body, "municipal_council")

    def test_local_news_record(self):
        result = self.service.get_local_news("Keizersgracht 123 Amsterdam")
        self.assertGreaterEqual(len(result["local_news"]), 1)
        self.assertEqual(result["local_news"][0].sentiment, "positive")

    def test_mockdata_marking(self):
        result = self.service.get_location_summary("Keizersgracht 123 Amsterdam")
        self.assertTrue(result["is_mock_data"])
        self.assertTrue(any("Mock data only" in warning for warning in result["data_quality_warnings"]))

    def test_serialization_of_all_new_models(self):
        now = datetime.now(timezone.utc)

        neighborhood = NeighborhoodProfile(retrieved_at=now)
        self.assertIn("retrieved_at", neighborhood.to_dict())

        development = AreaDevelopmentRecord(
            title="test",
            announcement_date=date(2026, 1, 1),
            retrieved_at=now,
        )
        self.assertEqual(development.to_dict()["announcement_date"], "2026-01-01")

        government = GovernmentRecord(
            meeting_date=date(2026, 2, 1),
            retrieved_at=now,
        )
        self.assertEqual(government.to_dict()["meeting_date"], "2026-02-01")

        news = NewsRecord(
            publication_date=date(2026, 3, 1),
            retrieved_at=now,
        )
        self.assertEqual(news.to_dict()["publication_date"], "2026-03-01")

    def test_safe_defaults_for_lists_and_dicts(self):
        neighborhood = NeighborhoodProfile()
        self.assertEqual(neighborhood.age_distribution, {})
        self.assertEqual(neighborhood.household_composition, {})
        self.assertEqual(neighborhood.dominant_business_sectors, [])
        self.assertEqual(neighborhood.data_quality_warnings, [])

    def test_no_shared_mutable_defaults_between_instances(self):
        a = NeighborhoodProfile()
        b = NeighborhoodProfile()

        a.age_distribution["test"] = 1.0
        a.dominant_business_sectors.append("retail")
        a.data_quality_warnings.append("warning")

        self.assertNotIn("test", b.age_distribution)
        self.assertEqual(b.dominant_business_sectors, [])
        self.assertEqual(b.data_quality_warnings, [])


if __name__ == "__main__":
    unittest.main()
