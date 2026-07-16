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


if __name__ == "__main__":
    unittest.main()
