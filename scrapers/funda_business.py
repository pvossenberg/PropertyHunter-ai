from __future__ import annotations

from bs4 import BeautifulSoup

from .base import (
    BaseScraper,
    ScrapeResult,
    extract_json_ld_blocks,
    extract_meta_content,
    extract_visible_text,
    find_first_jsonld_value,
    parse_address_from_jsonld,
    parse_area_sqm,
    parse_price,
)


class FundaBusinessScraper(BaseScraper):
    source_name = "funda_business"

    def parse_public_html(self, url: str, html: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "html.parser")
        blocks = extract_json_ld_blocks(soup)

        title = extract_meta_content(soup, "og:title")
        if not title and soup.title and soup.title.string:
            title = " ".join(soup.title.string.split())

        description = extract_meta_content(soup, "og:description") or extract_meta_content(soup, "description")

        address = parse_address_from_jsonld(find_first_jsonld_value(blocks, ["address"]))

        price_raw = find_first_jsonld_value(blocks, ["price", "offers", "priceSpecification"])
        if isinstance(price_raw, dict):
            price_raw = price_raw.get("price") or price_raw.get("minPrice")

        asking_price, price_status = parse_price(price_raw)
        if price_status is None:
            asking_price, price_status = parse_price(description)

        living_area = parse_area_sqm(find_first_jsonld_value(blocks, ["floorSize", "floorSizeValue", "size"]))
        plot_area = parse_area_sqm(find_first_jsonld_value(blocks, ["lotSize", "plotArea"]))
        object_type = find_first_jsonld_value(blocks, ["@type", "additionalType", "propertyType"])
        if isinstance(object_type, list):
            object_type = object_type[0] if object_type else None
        if not isinstance(object_type, str):
            object_type = None

        raw_text = extract_visible_text(soup)

        warnings: list[str] = []
        if not raw_text or len(raw_text.split()) < 35:
            warnings.append("Onvoldoende publiek leesbare inhoud op de Funda in Business-pagina.")
        if not title:
            warnings.append("Titel ontbreekt in publiek beschikbare metadata.")
        if not address:
            warnings.append("Adres ontbreekt in publiek beschikbare metadata.")

        success = bool(raw_text and len(raw_text.split()) >= 35 and (title or address))

        return ScrapeResult(
            source_name=self.source_name,
            source_url=url,
            success=success,
            title=title,
            address=address,
            asking_price=asking_price,
            price_status=price_status or "unknown",
            living_area=living_area,
            plot_area=plot_area,
            object_type=object_type,
            description=description,
            features=[],
            images=[],
            raw_text=raw_text[:15000] if raw_text else None,
            warnings=warnings,
            extraction_method="public_html_meta_jsonld",
            confidence=0.72 if success else 0.32,
        )
