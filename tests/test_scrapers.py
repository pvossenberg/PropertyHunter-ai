import unittest
from pathlib import Path

from app import _compose_source_text, _has_sufficient_source_text
from scrapers.funda import FundaScraper
from scrapers.funda_business import FundaBusinessScraper
from scrapers.generic import GenericScraper
from scrapers.router import build_fallback_recommendation, get_scraper_for_url


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class ScraperRoutingTests(unittest.TestCase):
    def test_routes_to_funda_scraper(self):
        scraper = get_scraper_for_url("https://www.funda.nl/koop/amsterdam/huis-123/")
        self.assertIsInstance(scraper, FundaScraper)

    def test_routes_to_funda_business_scraper(self):
        scraper = get_scraper_for_url("https://www.fundainbusiness.nl/bedrijfspand/utrecht/")
        self.assertIsInstance(scraper, FundaBusinessScraper)

    def test_routes_to_generic_scraper(self):
        scraper = get_scraper_for_url("https://broker.example.com/object/42")
        self.assertIsInstance(scraper, GenericScraper)


class FundaScraperTests(unittest.TestCase):
    def test_funda_page_with_jsonld(self):
        html = _load_fixture("funda_jsonld_success.html")
        result = FundaScraper().parse_public_html("https://www.funda.nl/koop/test", html)

        self.assertTrue(result.success)
        self.assertEqual(result.source_name, "funda")
        self.assertEqual(result.address, "Herengracht 10 1015 BR Amsterdam NL")
        self.assertEqual(result.asking_price, 850000.0)
        self.assertEqual(result.price_status, "known")
        self.assertEqual(result.living_area, 145.0)
        self.assertEqual(result.plot_area, 92.0)

    def test_funda_page_with_insufficient_data(self):
        html = _load_fixture("funda_insufficient.html")
        result = FundaScraper().parse_public_html("https://www.funda.nl/koop/test", html)

        self.assertFalse(result.success)
        self.assertGreaterEqual(len(result.warnings), 1)


class FundaBusinessScraperTests(unittest.TestCase):
    def test_funda_business_page_with_insufficient_data(self):
        html = _load_fixture("funda_business_insufficient.html")
        result = FundaBusinessScraper().parse_public_html("https://www.fundainbusiness.nl/koop/test", html)

        self.assertFalse(result.success)
        self.assertGreaterEqual(len(result.warnings), 1)


class GenericScraperTests(unittest.TestCase):
    def test_successful_local_broker_page(self):
        html = _load_fixture("local_broker_success.html")
        result = GenericScraper().parse_public_html("https://broker.example.com/object/42", html)

        self.assertTrue(result.success)
        self.assertEqual(result.source_name, "generic")
        self.assertEqual(result.address, "Kanaalweg 50 3526 KL Utrecht NL")
        self.assertEqual(result.living_area, 220.0)
        self.assertGreaterEqual(len(result.features), 1)

    def test_price_on_request(self):
        html = _load_fixture("local_broker_success.html")
        result = GenericScraper().parse_public_html("https://broker.example.com/object/42", html)

        self.assertEqual(result.price_status, "on_request")
        self.assertIsNone(result.asking_price)

    def test_missing_address(self):
        html = """
        <html>
          <head><title>Object zonder adres</title></head>
          <body><p>Ruime tekst met meerdere woorden zodat content wel beschikbaar is voor extractie.</p></body>
        </html>
        """
        result = GenericScraper().parse_public_html("https://example.com/no-address", html)
        self.assertIsNone(result.address)

    def test_no_fictional_zero_values(self):
        html = _load_fixture("funda_insufficient.html")
        result = GenericScraper().parse_public_html("https://example.com/empty", html)

        self.assertIsNone(result.asking_price)
        self.assertIsNone(result.living_area)
        self.assertIsNone(result.plot_area)


class FallbackAndSafetyTests(unittest.TestCase):
    def test_fallback_recommendation(self):
        html = _load_fixture("funda_insufficient.html")
        result = FundaScraper().parse_public_html("https://www.funda.nl/koop/test", html)
        fallback = build_fallback_recommendation(result)

        self.assertIn("paste_listing_text", fallback)
        self.assertIn("use_broker_source", fallback)
        self.assertIn("provide_address_manually", fallback)
        self.assertIn("broker_search_query", fallback)

    def test_no_ai_analysis_when_source_data_is_insufficient(self):
        html = _load_fixture("funda_insufficient.html")
        result = FundaScraper().parse_public_html("https://www.funda.nl/koop/test", html)
        source_text = _compose_source_text(result)

        self.assertFalse(_has_sufficient_source_text(source_text))


if __name__ == "__main__":
    unittest.main()
