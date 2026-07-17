import unittest

from models.property import Property
from services.public_data_service import DutchPublicDataService


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class PublicDataServiceTests(unittest.TestCase):
    def test_woz_uses_nummeraanduiding_id_when_available(self):
        calls = []

        def requester(method, url, **kwargs):
            calls.append((method.upper(), url, kwargs.get("params")))
            if "locatieserver" in url:
                return FakeResponse(
                    200,
                    {
                        "response": {
                            "docs": [
                                {
                                    "type": "adres",
                                    "identificatie": "0758010000018784-0758200000010265",
                                    "nummeraanduiding_id": "0758200000010265",
                                    "adresseerbaarobject_id": "0758010000018784",
                                    "postcode": "4824LP",
                                    "huisnummer": 6,
                                }
                            ]
                        }
                    },
                )
            if "/wozwaarde/nummeraanduiding/0758200000010265" in url:
                return FakeResponse(
                    200,
                    {
                        "wozObject": {
                            "wozobjectnummer": "75800023179",
                            "adresseerbaarobjectid": "0758010000018784",
                            "nummeraanduidingid": "0758200000010265",
                        },
                        "wozWaarden": [
                            {"peildatum": "2025-01-01", "vastgesteldeWaarde": 396000}
                        ],
                    },
                )
            return FakeResponse(404, {})

        service = DutchPublicDataService(requester=requester)
        snapshot = service._fetch_woz_snapshot_sync(
            Property(source_url="https://example.com/a", address="Hekoord 6 Breda", city="Breda")
        )

        self.assertEqual(snapshot.get("latest_woz_value"), 396000.0)
        self.assertEqual(snapshot.get("woz_valuation_year"), 2025)
        woz_calls = [call for call in calls if "/wozwaarde/nummeraanduiding/" in call[1]]
        self.assertEqual(len(woz_calls), 1)
        self.assertTrue(woz_calls[0][1].endswith("/0758200000010265"))

    def test_woz_fallbacks_to_split_identificatie_candidates(self):
        calls = []

        def requester(method, url, **kwargs):
            calls.append((method.upper(), url, kwargs.get("params")))
            if "locatieserver" in url:
                return FakeResponse(
                    200,
                    {
                        "response": {
                            "docs": [
                                {
                                    "type": "adres",
                                    "identificatie": "0758010000018784-0758200000010265",
                                }
                            ]
                        }
                    },
                )
            if url.endswith("/0758010000018784"):
                return FakeResponse(404, {})
            if url.endswith("/0758200000010265"):
                return FakeResponse(
                    200,
                    {
                        "wozObject": {
                            "wozobjectnummer": "75800023179",
                            "adresseerbaarobjectid": "0758010000018784",
                            "nummeraanduidingid": "0758200000010265",
                        },
                        "wozWaarden": [
                            {"peildatum": "2025-01-01", "vastgesteldeWaarde": 396000}
                        ],
                    },
                )
            return FakeResponse(404, {})

        service = DutchPublicDataService(requester=requester)
        snapshot = service._fetch_woz_snapshot_sync(
            Property(source_url="https://example.com/b", address="Hekoord 6 Breda", city="Breda")
        )

        self.assertEqual(snapshot.get("latest_woz_value"), 396000.0)
        self.assertEqual(snapshot.get("bag_numberaanduiding_id"), "0758200000010265")
        tried = ((snapshot.get("raw_payload") or {}).get("tried_nummeraanduiding_ids") or [])
        self.assertGreaterEqual(len(tried), 2)

    def test_bag_prefers_matching_letter_and_residential_object(self):
        def requester(method, url, **kwargs):
            if "locatieserver" in url:
                return FakeResponse(
                    200,
                    {
                        "response": {
                            "docs": [
                                {
                                    "type": "adres",
                                    "identificatie": "0518200000001001",
                                    "nummeraanduiding_id": "0518200000001001",
                                    "adresseerbaarobject_id": "0518010000002001",
                                    "postcode": "4811AB",
                                    "huisnummer": 10,
                                    "huisletter": "A",
                                    "straatnaam": "Markt",
                                    "woonplaatsnaam": "Breda",
                                },
                                {
                                    "type": "adres",
                                    "identificatie": "0518200000001002",
                                    "nummeraanduiding_id": "0518200000001002",
                                    "adresseerbaarobject_id": "0518010000002002",
                                    "postcode": "4811AB",
                                    "huisnummer": 10,
                                    "huisletter": "A",
                                    "straatnaam": "Markt",
                                    "woonplaatsnaam": "Breda",
                                },
                            ]
                        }
                    },
                )
            if "service.pdok.nl" in url and kwargs.get("data"):
                request_body = str(kwargs.get("data"))
                if "0518010000002001" in request_body:
                    return FakeResponse(
                        200,
                        {
                            "features": [
                                {
                                    "properties": {
                                        "identificatie": "0518010000002001",
                                        "oppervlakte": 70,
                                        "gebruiksdoel": ["kantoorfunctie"],
                                        "bouwjaar": 1991,
                                        "pandidentificatie": "0518100000003001",
                                        "status": "Verblijfsobject in gebruik",
                                    }
                                }
                            ]
                        },
                    )
                if "0518010000002002" in request_body:
                    return FakeResponse(
                        200,
                        {
                            "features": [
                                {
                                    "properties": {
                                        "identificatie": "0518010000002002",
                                        "oppervlakte": 82,
                                        "gebruiksdoel": ["woonfunctie"],
                                        "bouwjaar": 2003,
                                        "pandidentificatie": "0518100000003002",
                                        "status": "Verblijfsobject in gebruik",
                                    }
                                }
                            ]
                        },
                    )
            return FakeResponse(404, {})

        service = DutchPublicDataService(requester=requester)
        snapshot = service._fetch_bag_snapshot_sync(
            Property(
                source_url="https://example.com/c",
                address="Markt 10 A Breda",
                city="Breda",
                postal_code="4811 AB",
            )
        )

        self.assertEqual(snapshot.get("bag_address_id"), "0518200000001002")
        self.assertEqual(snapshot.get("bag_verblijfsobject_id"), "0518010000002002")
        self.assertEqual(snapshot.get("bag_usage_purpose"), "woonfunctie")
        self.assertEqual(snapshot.get("bag_official_floor_area_m2"), 82.0)

    def test_bag_flags_multiple_matches_and_missing_area_without_crashing(self):
        def requester(method, url, **kwargs):
            if "locatieserver" in url:
                return FakeResponse(
                    200,
                    {
                        "response": {
                            "docs": [
                                {
                                    "type": "adres",
                                    "identificatie": "0518200000001010",
                                    "nummeraanduiding_id": "0518200000001010",
                                    "adresseerbaarobject_id": "0518010000002010",
                                    "postcode": "4811AB",
                                    "huisnummer": 12,
                                    "straatnaam": "Markt",
                                    "woonplaatsnaam": "Breda",
                                },
                                {
                                    "type": "adres",
                                    "identificatie": "0518200000001011",
                                    "nummeraanduiding_id": "0518200000001011",
                                    "adresseerbaarobject_id": "0518010000002011",
                                    "postcode": "4811AB",
                                    "huisnummer": 12,
                                    "straatnaam": "Markt",
                                    "woonplaatsnaam": "Breda",
                                },
                            ]
                        }
                    },
                )
            if "service.pdok.nl" in url:
                return FakeResponse(
                    200,
                    {
                        "features": [
                            {
                                "properties": {
                                    "identificatie": "0518010000002010",
                                    "oppervlakte": None,
                                    "gebruiksdoel": ["winkelfunctie"],
                                    "bouwjaar": 1988,
                                    "pandidentificatie": "0518100000003010",
                                    "status": "Verblijfsobject in gebruik",
                                }
                            }
                        ]
                    },
                )
            return FakeResponse(404, {})

        service = DutchPublicDataService(requester=requester)
        snapshot = service._fetch_bag_snapshot_sync(
            Property(
                source_url="https://example.com/d",
                address="Markt 12 Breda",
                city="Breda",
                postal_code="4811 AB",
            )
        )

        self.assertIn("multiple_possible_bag_matches", snapshot.get("quality_flags") or [])
        self.assertIn("non_residential_usage_purpose", snapshot.get("quality_flags") or [])
        self.assertIn("missing_official_area", snapshot.get("quality_flags") or [])

    def test_bag_prefers_doc_vbo_id_when_woz_vbo_id_drops_leading_zero(self):
        def requester(method, url, **kwargs):
            if "locatieserver" in url:
                return FakeResponse(
                    200,
                    {
                        "response": {
                            "docs": [
                                {
                                    "type": "adres",
                                    "identificatie": "0758010000017236-0758200000019961",
                                    "nummeraanduiding_id": "0758200000019961",
                                    "adresseerbaarobject_id": "0758010000017236",
                                    "postcode": "4814CH",
                                    "huisnummer": 12,
                                    "straatnaam": "Dijkstraat",
                                    "woonplaatsnaam": "Breda",
                                }
                            ]
                        }
                    },
                )
            if "/wozwaarde/nummeraanduiding/0758200000019961" in url:
                return FakeResponse(
                    200,
                    {
                        "wozObject": {
                            "wozobjectnummer": "75800023179",
                            "adresseerbaarobjectid": "758010000017236",
                            "nummeraanduidingid": "0758200000019961",
                        },
                        "wozWaarden": [
                            {"peildatum": "2025-01-01", "vastgesteldeWaarde": 396000}
                        ],
                    },
                )
            if "service.pdok.nl" in url and kwargs.get("data") and "0758010000017236" in str(kwargs.get("data")):
                return FakeResponse(
                    200,
                    {
                        "features": [
                            {
                                "properties": {
                                    "identificatie": "0758010000017236",
                                    "oppervlakte": 118,
                                    "gebruiksdoel": ["woonfunctie"],
                                    "bouwjaar": 1930,
                                    "pandidentificatie": "0758100000009999",
                                    "status": "Verblijfsobject in gebruik",
                                }
                            }
                        ]
                    },
                )
            return FakeResponse(404, {})

        service = DutchPublicDataService(requester=requester)
        snapshot = service._fetch_bag_snapshot_sync(
            Property(source_url="https://example.com/e", address="Dijkstraat 12 Breda", city="Breda")
        )

        self.assertEqual(snapshot.get("bag_verblijfsobject_id"), "0758010000017236")
        self.assertEqual(snapshot.get("bag_official_floor_area_m2"), 118.0)
        self.assertEqual(snapshot.get("bag_building_year"), 1930)


if __name__ == "__main__":
    unittest.main()
