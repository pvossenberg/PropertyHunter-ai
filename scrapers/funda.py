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


class FundaScraper(BaseScraper):
    source_name = "funda"

    def parse_public_html(self, url: str, html: str) -> ScrapeResult:
        soup = BeautifulSoup(html, "html.parser")
        blocks = extract_json_ld_blocks(soup)

        title = extract_meta_content(soup, "og:title")
        if not title and soup.title and soup.title.string:
            title = " ".join(soup.title.string.split())

        description = extract_meta_content(soup, "og:description") or extract_meta_content(soup, "description")

        address_value = find_first_jsonld_value(blocks, ["address"])
        address = parse_address_from_jsonld(address_value)

        price_value = find_first_jsonld_value(blocks, ["price", "offers"])
        if isinstance(price_value, dict):
            candidate = price_value.get("price") or price_value.get("priceSpecification")
            if isinstance(candidate, dict):
                candidate = candidate.get("price")
            price_value = candidate
        if isinstance(price_value, list):
            candidate = price_value[0] if price_value else None
            if isinstance(candidate, dict):
                price_value = candidate.get("price")
            else:
                price_value = candidate

        if price_value in (None, ""):
            price_value = extract_meta_content(soup, "product:price:amount")

        asking_price, price_status = parse_price(price_value)
        if price_status is None:
            asking_price, price_status = parse_price(description)

        living_area = parse_area_sqm(find_first_jsonld_value(blocks, ["floorSize", "floorSizeValue", "livingArea"]))
        plot_area = parse_area_sqm(find_first_jsonld_value(blocks, ["lotSize", "plotArea"]))

        object_type = find_first_jsonld_value(blocks, ["@type", "additionalType", "propertyType"])
        if isinstance(object_type, list):
            object_type = object_type[0] if object_type else None
        if not isinstance(object_type, str):
            object_type = None

        raw_text = extract_visible_text(soup)
        images: list[str] = []
        og_image = extract_meta_content(soup, "og:image")
        if og_image:
            images.append(og_image)

        image_candidate = find_first_jsonld_value(blocks, ["image", "photos"])
        if isinstance(image_candidate, str):
            images.append(image_candidate)
        elif isinstance(image_candidate, list):
            for item in image_candidate:
                if isinstance(item, str):
                    images.append(item)

        warnings: list[str] = []
        if not raw_text or len(raw_text.split()) < 35:
            warnings.append("Onvoldoende publiek leesbare inhoud op de Funda-pagina.")
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
            images=list(dict.fromkeys(images)),
            raw_text=raw_text[:15000] if raw_text else None,
            warnings=warnings,
            extraction_method="public_html_meta_jsonld",
            confidence=0.75 if success else 0.35,
        )
