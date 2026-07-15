import unittest

from deal_finder.sources.funda import FundaAdapter


INDEX_PAGE_1 = """
<html>
  <body>
    <a href="/detail/koop/amsterdam/object-1/11111111/">Listing 1</a>
    <a href="/detail/koop/utrecht/object-2/22222222/">Listing 2</a>
    <a rel="next" href="/zoeken/koop?p=2">Volgende</a>
  </body>
</html>
"""

INDEX_PAGE_2 = """
<html>
  <body>
    <a href="/detail/koop/amsterdam/object-1/11111111/">Listing 1 duplicate</a>
    <a href="/detail/koop/rotterdam/object-3/33333333/">Listing 3</a>
  </body>
</html>
"""

LISTING_HTML = """
<html>
  <head>
    <meta property="og:title" content="Ruim appartement aan de gracht" />
    <meta property="og:description" content="Karakteristiek object in de binnenstad" />
    <meta property="og:image" content="https://img.example.com/a.jpg" />
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "SingleFamilyResidence",
        "name": "Ruim appartement aan de gracht",
        "address": {
          "streetAddress": "Herengracht 10",
          "postalCode": "1015BR",
          "addressLocality": "Amsterdam",
          "addressCountry": "NL"
        },
        "offers": {"price": "850000"},
        "floorSize": "145 m2",
        "lotSize": "92 m2",
        "image": ["https://img.example.com/a.jpg", "https://img.example.com/b.jpg"]
      }
    </script>
  </head>
  <body>
    <h1>Ruim appartement aan de gracht</h1>
    <dl>
      <dt>Perceeloppervlakte</dt><dd>92 m2</dd>
      <dt>Sinds</dt><dd>20 juni 2026</dd>
      <dt>Prijswijzigingen</dt><dd>2 prijswijzigingen</dd>
      <dt>Vraagprijs</dt><dd>€ 850.000 k.k.</dd>
      <dt>Slaapkamers</dt><dd>3 slaapkamers</dd>
      <dt>Energielabel</dt><dd>A+</dd>
      <dt>Bouwjaar</dt><dd>1905</dd>
      <dt>Makelaar</dt><dd>Voorbeeld Makelaars</dd>
      <dt>Woningtype</dt><dd>Appartement</dd>
    </dl>
    <img src="/images/c.jpg" />
  </body>
</html>
"""


class FundaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FundaAdapter()

    def test_validate_configuration_requires_funda_host(self):
        ok, warnings = self.adapter.validate_configuration({"start_url": "https://example.com/listings"})
        self.assertFalse(ok)
        self.assertTrue(any("funda.nl" in item for item in warnings))

    def test_fetch_listings_crawls_pagination_and_skips_duplicates(self):
        html_by_url = {
            "https://www.funda.nl/zoeken/koop": INDEX_PAGE_1,
            "https://www.funda.nl/zoeken/koop?p=2": INDEX_PAGE_2,
            "https://www.funda.nl/detail/koop/amsterdam/object-1/11111111/": LISTING_HTML,
            "https://www.funda.nl/detail/koop/utrecht/object-2/22222222/": LISTING_HTML,
            "https://www.funda.nl/detail/koop/rotterdam/object-3/33333333/": LISTING_HTML,
        }

        def fake_fetch(url: str, timeout_seconds: float) -> str:
            self.assertGreater(timeout_seconds, 0)
            return html_by_url[url]

        self.adapter._fetch_html = fake_fetch  # type: ignore[method-assign]

        records = self.adapter.fetch_listings({"start_url": "https://www.funda.nl/zoeken/koop", "max_pages": 5})

        self.assertEqual(len(records), 3)
        self.assertEqual(self.adapter._last_fetch_stats["records_found"], 4)
        self.assertEqual(self.adapter._last_fetch_stats["records_imported"], 3)
        self.assertEqual(self.adapter._last_fetch_stats["records_skipped"], 1)
        self.assertEqual(self.adapter._last_fetch_stats["records_failed"], 0)

    def test_fetch_listings_continues_when_individual_listing_fails(self):
        html_by_url = {
            "https://www.funda.nl/zoeken/koop": INDEX_PAGE_1,
            "https://www.funda.nl/detail/koop/amsterdam/object-1/11111111/": LISTING_HTML,
            "https://www.funda.nl/detail/koop/utrecht/object-2/22222222/": LISTING_HTML,
        }

        def fake_fetch(url: str, timeout_seconds: float) -> str:
            if url.endswith("22222222/"):
                raise RuntimeError("listing parse failed")
            return html_by_url[url]

        self.adapter._fetch_html = fake_fetch  # type: ignore[method-assign]

        records = self.adapter.fetch_listings({"start_url": "https://www.funda.nl/zoeken/koop", "max_pages": 1})
        self.assertEqual(len(records), 1)
        self.assertEqual(self.adapter._last_fetch_stats["records_found"], 2)
        self.assertEqual(self.adapter._last_fetch_stats["records_imported"], 1)
        self.assertEqual(self.adapter._last_fetch_stats["records_failed"], 1)

    def test_extract_and_normalize_all_required_fields(self):
        record = self.adapter._extract_listing_record(
            source_url="https://www.funda.nl/detail/koop/amsterdam/object-1/11111111/",
            html=LISTING_HTML,
        )

        self.assertEqual(record["title"], "Ruim appartement aan de gracht")
        self.assertEqual(record["city"], "Amsterdam")
        self.assertEqual(record["asking_price"], 850000.0)
        self.assertEqual(record["asking_price_status"], "known")
        self.assertEqual(record["asking_price_text"], "850000")
        self.assertEqual(record["living_area"], 145.0)
        self.assertEqual(record["plot_size"], 92.0)
        self.assertEqual(record["property_type"], "SingleFamilyResidence")
        self.assertEqual(record["bedrooms"], 3)
        self.assertEqual(record["energy_label"], "A+")
        self.assertEqual(record["construction_year"], 1905)
        self.assertEqual(record["broker"], "Voorbeeld Makelaars")
        self.assertEqual(record["listed_since"].isoformat(), "2026-06-20")
        self.assertEqual(record["price_reduction_count"], 2)
        self.assertEqual(record["current_asking_price"], 850000.0)
        self.assertEqual(record["listing_history_source"], "funda")
        self.assertIn(record["listing_history_confidence"], {"medium", "high"})
        self.assertEqual(record["listing_id"], "11111111")
        self.assertGreaterEqual(len(record["photos"]), 2)
        self.assertIsNotNone(record["timestamp"])

        listing = self.adapter.normalize_listing(record)
        self.assertEqual(listing.source_name, "funda.nl")
        self.assertEqual(listing.external_listing_id, "11111111")
        self.assertEqual(listing.source_url, "https://www.funda.nl/detail/koop/amsterdam/object-1/11111111/")
        self.assertEqual(listing.title, "Ruim appartement aan de gracht")
        self.assertEqual(listing.asking_price, 850000.0)
        self.assertEqual(listing.surface_m2, 145.0)
        self.assertEqual(listing.property_type, "SingleFamilyResidence")
        self.assertEqual(listing.raw_payload.get("bedrooms"), 3)
        self.assertEqual(listing.raw_payload.get("energy_label"), "A+")
        self.assertEqual(listing.raw_payload.get("construction_year"), 1905)
        self.assertEqual(listing.raw_payload.get("broker"), "Voorbeeld Makelaars")
        self.assertEqual(listing.raw_payload.get("asking_price_status"), "known")
        self.assertEqual(listing.raw_payload.get("asking_price_text"), "850000")
        self.assertEqual(listing.raw_payload.get("listed_since").isoformat(), "2026-06-20")
        self.assertEqual(listing.raw_payload.get("price_reduction_count"), 2)
        self.assertEqual(listing.raw_payload.get("current_asking_price"), 850000.0)
        self.assertTrue(listing.raw_payload.get("photos"))

        property_model = self.adapter.to_property_model(record)
        self.assertEqual(property_model.source_url, listing.source_url)
        self.assertEqual(property_model.title, listing.title)
        self.assertEqual(property_model.address, listing.address)
        self.assertEqual(property_model.city, listing.city)
        self.assertEqual(property_model.asking_price, 850000.0)
        self.assertEqual(property_model.asking_price_status, "known")
        self.assertEqual(property_model.asking_price_text, "850000")
        self.assertEqual(property_model.surface_m2, 145.0)
        self.assertEqual(property_model.plot_size_m2, 92.0)
        self.assertEqual(property_model.property_type, "SingleFamilyResidence")
        self.assertEqual(property_model.bedrooms, 3)
        self.assertEqual(property_model.energy_label, "A+")
        self.assertEqual(property_model.construction_year, 1905)
        self.assertEqual(property_model.broker, "Voorbeeld Makelaars")
        self.assertEqual(property_model.listed_since.isoformat(), "2026-06-20")
        self.assertEqual(property_model.price_reduction_count, 2)
        self.assertEqual(property_model.current_asking_price, 850000.0)
        self.assertEqual(property_model.listing_id, "11111111")
        self.assertEqual(property_model.external_listing_id, "11111111")
        self.assertTrue(property_model.photos)
        self.assertIn("https://www.funda.nl/images/c.jpg", property_model.photos)
        self.assertIsNotNone(property_model.source_timestamp)
        self.assertIsNotNone(property_model.scraped_at)
        self.assertIsNotNone(property_model.price_per_m2)


if __name__ == "__main__":
    unittest.main()