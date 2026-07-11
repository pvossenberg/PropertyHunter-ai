from __future__ import annotations

from urllib.parse import urlparse

from .base import ScrapeResult
from .funda import FundaScraper
from .funda_business import FundaBusinessScraper
from .generic import GenericScraper


def get_scraper_for_url(url: str):
    parsed = urlparse((url or "").strip())
    hostname = (parsed.hostname or "").lower()

    if hostname.endswith("funda.nl"):
        return FundaScraper()
    if hostname.endswith("fundainbusiness.nl"):
        return FundaBusinessScraper()
    return GenericScraper()


def build_fallback_recommendation(result: ScrapeResult) -> dict[str, str]:
    fragment = (result.address or result.title or "").strip()
    fragment = " ".join(fragment.split())
    if not fragment:
        fragment = "bedrijfspand te koop"

    return {
        "paste_listing_text": "Plak de volledige advertentietekst uit de listing in het tekstveld.",
        "use_broker_source": "Gebruik de URL van de verkopende makelaar als primaire bron.",
        "provide_address_manually": "Voer het adres handmatig in als dit niet zichtbaar is op de pagina.",
        "broker_search_query": f"{fragment} makelaar te koop",
    }


def scrape_url(url: str) -> ScrapeResult:
    scraper = get_scraper_for_url(url)
    result = scraper.scrape(url)

    if result.source_name in {"funda", "funda_business"} and not result.success:
        result.fallback_recommendation = build_fallback_recommendation(result)

    return result
