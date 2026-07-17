import unittest

from models.property import Property
from services.property_enrichment import PropertyEnrichmentEngine


class StubPublicDataService:
    async def fetch_bag_snapshot(self, property_obj):
        return {
            "bag_id": "0363010000000001",
            "bag_nummeraanduiding_id": "0363200000000001",
            "bag_pand_id": "0363100000000001",
            "bag_building_year": 1998,
            "bag_usage_purpose": "woonfunctie",
            "bag_official_floor_area_m2": 100.0,
            "bag_coordinates_rd": {"x": 121000.0, "y": 487000.0},
            "bag_coordinates_ll": {"longitude": 4.89, "latitude": 52.37},
            "bag_postcode": "1015AB",
            "bag_municipality": "Amsterdam",
            "source": "stub-bag",
            "retrieval_date": "2026-07-15T12:00:00+00:00",
            "confidence_score": 92,
            "raw_payload": {},
        }

    async def fetch_woz_snapshot(self, property_obj):
        return {
            "woz_object_number": 123456789,
            "latest_woz_value": 650000.0,
            "woz_valuation_year": 2025,
            "woz_historical_values": [{"valuation_year": 2024, "value": 630000.0}],
            "source": "stub-woz",
            "retrieval_date": "2026-07-15T12:00:00+00:00",
            "confidence_score": 95,
            "raw_payload": {},
        }


class BagFailingPublicDataService(StubPublicDataService):
    async def fetch_bag_snapshot(self, property_obj):
        raise RuntimeError("bag unavailable")


class FailingStreetPriceEngine(PropertyEnrichmentEngine):
    async def _lookup_street_price(self, property_obj, address):
        raise RuntimeError("street lookup failed")


class PropertyEnrichmentEngineTests(unittest.TestCase):
    def test_enriches_all_expected_fields(self):
        engine = PropertyEnrichmentEngine(public_data_service=StubPublicDataService())
        property_obj = Property(
            source_url="https://example.com/p1",
            listing_id="p1",
            title="Test object",
            address="Herengracht 1",
            city="Amsterdam",
            postal_code="1015 AB",
            municipality="Amsterdam",
            asking_price=750000,
            surface_m2=100,
            property_type="Appartement",
            description="Gemeentelijk monument in centrum",
            raw_text="Gemeentelijk monument in centrum",
        )

        result = engine.enrich(property_obj)
        items_by_key = {item.enrichment_key: item for item in result.items}

        self.assertEqual(len(result.items), 39)
        self.assertEqual(items_by_key["postal_code"].value, "1015 AB")
        self.assertEqual(items_by_key["municipality"].value, "Amsterdam")
        self.assertEqual(items_by_key["monument_status"].value, "monument")
        self.assertEqual(items_by_key["bag_id"].value, "0363010000000001")
        self.assertEqual(items_by_key["latest_woz_value"].value, 650000.0)
        self.assertEqual(items_by_key["calculation_area_m2"].value, 100.0)
        self.assertEqual(items_by_key["calculation_area_source"].value, "BAG")
        self.assertEqual(items_by_key["asking_price_per_m2"].value, 7500.0)
        self.assertEqual(items_by_key["woz_value_per_m2"].value, 6500.0)
        self.assertIn("distance_to_city_center", items_by_key)
        self.assertTrue(all("retrieval_date" in item.to_dict() for item in result.items))

    def test_falls_back_to_funda_area_when_bag_match_is_low_confidence(self):
        class LowConfidenceBagService(StubPublicDataService):
            async def fetch_bag_snapshot(self, property_obj):
                result = await super().fetch_bag_snapshot(property_obj)
                result["confidence_score"] = 45
                result["quality_flags"] = ["low_confidence_match", "missing_official_area"]
                return result

        engine = PropertyEnrichmentEngine(public_data_service=LowConfidenceBagService())
        property_obj = Property(
            source_url="https://example.com/p5",
            listing_id="p5",
            address="Herengracht 5",
            city="Amsterdam",
            postal_code="1015 AB",
            municipality="Amsterdam",
            asking_price=600000,
            surface_m2=96,
            property_type="Appartement",
        )

        result = engine.enrich(property_obj)
        items_by_key = {item.enrichment_key: item for item in result.items}

        self.assertEqual(items_by_key["calculation_area_m2"].value, 96.0)
        self.assertEqual(items_by_key["calculation_area_source"].value, "Funda")
        self.assertEqual(items_by_key["asking_price_per_m2"].value, 6250.0)
        self.assertIn("low_confidence_match", items_by_key["bag_quality_flags"].value)

    def test_continues_when_one_enrichment_fails(self):
        engine = FailingStreetPriceEngine(public_data_service=StubPublicDataService())
        property_obj = Property(
            source_url="https://example.com/p2",
            listing_id="p2",
            address="Stationsweg 1",
            city="Utrecht",
            asking_price=500000,
            surface_m2=90,
            description="Normaal object",
        )

        result = engine.enrich(property_obj)
        items_by_key = {item.enrichment_key: item for item in result.items}

        self.assertEqual(len(result.items), 39)
        self.assertFalse(items_by_key["street_m2_price_average"].success)
        self.assertIsNotNone(items_by_key["street_m2_price_average"].error_message)
        self.assertTrue(items_by_key["latest_woz_value"].success)
        self.assertTrue(items_by_key["permit_information"].success)

    def test_public_data_partial_failure_still_returns_other_source(self):
        engine = PropertyEnrichmentEngine(public_data_service=BagFailingPublicDataService())
        property_obj = Property(
            source_url="https://example.com/p4",
            listing_id="p4",
            address="Dam 1",
            city="Amsterdam",
        )

        result = engine.enrich(property_obj)
        items_by_key = {item.enrichment_key: item for item in result.items}

        self.assertFalse(items_by_key["bag_id"].success)
        self.assertTrue(items_by_key["latest_woz_value"].success)
        self.assertEqual(items_by_key["latest_woz_value"].value, 650000.0)

    def test_to_dict_is_json_friendly(self):
        engine = PropertyEnrichmentEngine(public_data_service=StubPublicDataService())
        result = engine.enrich(Property(source_url="https://example.com/p3", listing_id="p3", address="Dam 1", city="Amsterdam"))
        payload = result.to_dict()

        self.assertEqual(payload["property_id"], "p3")
        self.assertEqual(len(payload["items"]), 39)
        self.assertIsInstance(payload["items"], list)


if __name__ == "__main__":
    unittest.main()
