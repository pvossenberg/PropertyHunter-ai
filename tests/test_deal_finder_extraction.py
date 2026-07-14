import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from deal_finder.extraction import extract_listing_metadata
from deal_finder.orchestrator import DealFinderOrchestrator
from services.database import DatabaseService


class FakeResponse:
    def __init__(self, html: str, *, url: str, status_code: int = 200, encoding: str = "utf-8"):
        self._data = html.encode(encoding)
        self.url = url
        self.status_code = status_code
        self.encoding = encoding

    def iter_content(self, chunk_size: int = 8192):
        for index in range(0, len(self._data), chunk_size):
            yield self._data[index : index + chunk_size]

    def close(self):
        return None


class DealFinderExtractionTests(unittest.TestCase):
    def test_json_ld_extraction(self):
        html = Path("/workspaces/PropertyHunter-ai/tests/fixtures/gb_makelaars_public.html").read_text(encoding="utf-8")
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://www.gbmakelaars.nl/aanbod/utrecht/winkelpand-123")):
            result = extract_listing_metadata("https://www.gbmakelaars.nl/aanbod/utrecht/winkelpand-123")
        self.assertTrue(result.success)
        self.assertEqual(result.extraction_method, "json_ld")
        self.assertEqual(result.title, "Winkelpand Utrecht Centrum")
        self.assertEqual(result.city, "Utrecht")
        self.assertEqual(result.asking_price, 675000.0)
        self.assertEqual(result.surface_m2, 145.0)
        self.assertGreaterEqual(len(result.images), 1)

    def test_gb_makelaars_prefers_real_property_title_and_address(self):
        html = """
        <html>
            <head>
                <title>GB Makelaars | Burgemeester de Manlaan 1B - GB Makelaars</title>
                <meta property='og:title' content='Burgemeester de Manlaan 1B - GB Makelaars' />
                <meta property='og:description' content='Introductie' />
                <script type='application/ld+json'>
                {
                    "@context": "https://schema.org",
                    "@graph": [
                        {"@type": "WebPage", "name": "GB Makelaars", "description": "Kijkt net verder"},
                        {"@type": "BreadcrumbList", "itemListElement": [{"@type": "ListItem", "position": 1, "name": "Home"}, {"@type": "ListItem", "position": 2, "name": "Woningen"}, {"@type": "ListItem", "position": 3, "name": "Burgemeester de Manlaan 1B"}]}
                    ]
                }
                </script>
            </head>
            <body>
                <h3 class='single__title'>Burgemeester de Manlaan 1B, 4837 BN Breda</h3>
                <div class='table__row'><div class='table__cell'>Woonoppervlakte</div><div class='table__cell'>156 m 2</div></div>
            </body>
        </html>
        """
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2")):
            result = extract_listing_metadata("https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2")

        self.assertTrue(result.success)
        self.assertNotEqual(result.title, "GB Makelaars")
        self.assertEqual(result.title, "Burgemeester de Manlaan 1B")
        self.assertEqual(result.address, "Burgemeester de Manlaan 1B, 4837 BN, Breda")
        self.assertEqual(result.postal_code, "4837 BN")
        self.assertEqual(result.city, "Breda")
        self.assertEqual(result.surface_m2, 156.0)
        self.assertEqual(result.raw_metadata["html_fallback"]["title"], "Burgemeester de Manlaan 1B")
        self.assertEqual(result.raw_metadata["html_fallback"]["heading"], "Burgemeester de Manlaan 1B, 4837 BN Breda")

    def test_gb_makelaars_address_falls_back_to_url_slug(self):
        html = """
        <html>
            <head>
                <title>GB Makelaars</title>
                <meta property='og:title' content='GB Makelaars' />
            </head>
            <body>
                <p>Geen bruikbare adresgegevens zichtbaar.</p>
            </body>
        </html>
        """
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2")):
            result = extract_listing_metadata("https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2")

        self.assertTrue(result.success)
        self.assertEqual(result.address, "Burgemeester de Manlaan 1B-2")
        self.assertEqual(result.title, "Burgemeester de Manlaan 1B")

    def test_gb_makelaars_extracts_dutch_postcode_and_surface(self):
        html = """
        <html>
            <head><title>Burgemeester de Manlaan 1B - GB Makelaars</title></head>
            <body>
                <h3 class='single__title'>Burgemeester de Manlaan 1B, 4837 BN Breda</h3>
                <div class='table__row'><div class='table__cell'>Gebruiksoppervlakte wonen</div><div class='table__cell'>250 m²</div></div>
            </body>
        </html>
        """
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2")):
            result = extract_listing_metadata("https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2")

        self.assertEqual(result.postal_code, "4837 BN")
        self.assertEqual(result.surface_m2, 250.0)

    def test_open_graph_fallback(self):
        html = """
        <html><head>
          <meta property='og:title' content='OG Listing' />
          <meta property='og:description' content='OG description' />
          <meta property='og:image' content='https://example.com/og.jpg' />
          <link rel='canonical' href='https://example.com/listing/og' />
        </head><body></body></html>
        """
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://example.com/listing/og")):
            result = extract_listing_metadata("https://example.com/listing/og")
        self.assertTrue(result.success)
        self.assertEqual(result.extraction_method, "open_graph")
        self.assertEqual(result.title, "OG Listing")
        self.assertEqual(result.description, "OG description")

    def test_html_fallback(self):
        html = """
        <html><head><title>Appartement te koop</title></head>
        <body>
          <p class='adres'>Lange Nieuwstraat 10, 3512 PH Utrecht</p>
          <p class='vraagprijs'>EUR 420.000</p>
          <p class='oppervlakte'>98 m2</p>
          <p class='omschrijving'>Instapklaar appartement</p>
        </body></html>
        """
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://broker.example.com/listing/10")):
            result = extract_listing_metadata("https://broker.example.com/listing/10")
        self.assertTrue(result.success)
        self.assertEqual(result.extraction_method, "html_fallback")
        self.assertEqual(result.city, "Utrecht")
        self.assertEqual(result.asking_price, 420000.0)
        self.assertEqual(result.surface_m2, 98.0)

    def test_malformed_json_ld(self):
        html = """
        <html><head>
          <script type='application/ld+json'>{broken-json</script>
          <meta property='og:title' content='Fallback title' />
        </head><body></body></html>
        """
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://example.com/listing/malformed")):
            result = extract_listing_metadata("https://example.com/listing/malformed")
        self.assertTrue(result.success)
        self.assertEqual(result.title, "Fallback title")
        self.assertTrue(any("Malformed JSON-LD" in warning for warning in result.warnings))

    def test_timeout(self):
        with patch("deal_finder.extraction.requests.get", side_effect=requests.Timeout):
            result = extract_listing_metadata("https://example.com/timeout")
        self.assertFalse(result.success)
        self.assertEqual(result.extraction_method, "timeout")

    def test_unreachable_host(self):
        with patch("deal_finder.extraction.requests.get", side_effect=requests.ConnectionError):
            result = extract_listing_metadata("https://does-not-exist.invalid/listing")
        self.assertFalse(result.success)
        self.assertEqual(result.extraction_method, "connection_error")

    def test_private_local_url_rejection(self):
        with patch("deal_finder.extraction.requests.get") as mocked_get:
            result = extract_listing_metadata("http://127.0.0.1/private")
        self.assertFalse(result.success)
        self.assertEqual(result.extraction_method, "rejected")
        mocked_get.assert_not_called()

    def test_no_live_http_calls(self):
        html = "<html><head><title>No live</title></head><body></body></html>"
        with patch("deal_finder.extraction.requests.get", return_value=FakeResponse(html, url="https://example.com/no-live")) as mocked_get:
            result = extract_listing_metadata("https://example.com/no-live")
        self.assertTrue(result.success)
        mocked_get.assert_called_once()

    def test_refresh_enrichment_updates_existing_listing_fields(self):
        class StubDatabase(DatabaseService):
            def __init__(self):
                super().__init__(url="", key="")
                self._enabled = True
                self._client = object()
                self._tables = {
                    "listings": [
                        {
                            "id": "listing-1",
                            "source_id": "source-1",
                            "external_listing_id": "external-1",
                            "source_url": "https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2",
                            "title": "GB Makelaars",
                            "address": None,
                            "city": "unknown",
                            "asking_price": 1775000.0,
                            "surface_m2": None,
                            "property_type": None,
                            "listing_status": "active",
                            "raw_payload": {"metadata": {"title": "GB Makelaars", "address": None, "city": "Breda", "surface_m2": None}},
                            "property_id": None,
                        }
                    ],
                    "listing_sources": [{"id": "source-1", "name": "Local broker websites"}],
                    "listing_snapshots": [],
                    "deal_candidates": [{"id": "candidate-1", "listing_id": "listing-1", "review_status": "new", "priority": "low", "reasons": []}],
                }
                self._counter = 1

            def _next_id(self) -> str:
                self._counter += 1
                return f"generated-{self._counter}"

            def _fetch_rows(self, table_name, *, limit=100, order_column="created_at", ascending=False, filters=None):
                rows = [dict(item) for item in self._tables.get(table_name, [])]
                filtered = []
                for row in rows:
                    matches = True
                    for key, value in (filters or {}).items():
                        if row.get(key) != value:
                            matches = False
                            break
                    if matches:
                        filtered.append(row)
                filtered.sort(key=lambda item: str(item.get(order_column) or ""), reverse=not ascending)
                return filtered[:limit]

            def _insert_row(self, table_name, payload):
                row = dict(payload)
                row.setdefault("id", self._next_id())
                self._tables.setdefault(table_name, []).append(row)
                return dict(row)

            def _update_row(self, table_name, row_id, payload):
                rows = self._tables.get(table_name, [])
                for index, row in enumerate(rows):
                    if str(row.get("id")) == str(row_id):
                        updated = dict(row)
                        updated.update(payload)
                        rows[index] = updated
                        return dict(updated)
                return {}

            def _upsert_rows(self, table_name, payload, on_conflict):
                rows = self._tables.setdefault(table_name, [])
                keys = [item.strip() for item in on_conflict.split(",")]
                for index, row in enumerate(rows):
                    if all(row.get(key) == payload.get(key) for key in keys):
                        updated = dict(row)
                        updated.update(payload)
                        rows[index] = updated
                        return dict(updated)
                return self._insert_row(table_name, payload)

            @property
            def listings(self):
                return self._tables["listings"]

        extractor_result = {
            "title": "Burgemeester de Manlaan 1B",
            "address": "Burgemeester de Manlaan 1B",
            "postal_code": "4837 BN",
            "city": "Breda",
            "asking_price": 1775000.0,
            "surface_m2": 156.0,
            "property_type": "Apartment",
            "description": "Luxueuze woning",
        }

        def extractor(_url: str):
            from deal_finder.extraction import ListingExtractionResult

            return ListingExtractionResult(
                success=True,
                source_url="https://gbmakelaars.nl/woning/burgemeester-de-manlaan-1b-2",
                extraction_method="json_ld",
                confidence=0.9,
                warnings=[],
                raw_metadata={},
                **extractor_result,
            )

        db = StubDatabase()
        orchestrator = DealFinderOrchestrator(db, metadata_extractor=extractor)
        refresh_result = orchestrator.refresh_listing_metadata("listing-1")

        self.assertTrue(refresh_result["ok"])
        self.assertEqual(refresh_result["listing"]["title"], "Burgemeester de Manlaan 1B")
        self.assertEqual(refresh_result["listing"]["address"], "Burgemeester de Manlaan 1B")
        self.assertEqual(refresh_result["listing"]["city"], "Breda")
        self.assertEqual(refresh_result["candidate"]["listing_id"], "listing-1")
        self.assertEqual(len(db.listings), 1)

        stored_listing = db.get_listing_detail("listing-1")["listing"]
        self.assertEqual(stored_listing["title"], "Burgemeester de Manlaan 1B")
        self.assertEqual(stored_listing["address"], "Burgemeester de Manlaan 1B")
        self.assertEqual(stored_listing["surface_m2"], 156.0)
        self.assertEqual(stored_listing["city"], "Breda")
        self.assertEqual(stored_listing["raw_payload"]["postal_code"], "4837 BN")

        snapshot_count = len(db._tables["listing_snapshots"])
        second_refresh = orchestrator.refresh_listing_metadata("listing-1")
        self.assertTrue(second_refresh["ok"])
        self.assertFalse(second_refresh["snapshot_changed"])
        self.assertEqual(len(db._tables["listing_snapshots"]), snapshot_count)


if __name__ == "__main__":
    unittest.main()
