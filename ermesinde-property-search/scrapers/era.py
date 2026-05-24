import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class EraScraper(BaseScraper):
    name = "ERA"
    base_url = "https://www.era.pt"

    SEARCH_URLS = [
        "https://www.era.pt/comprar/habitacao/porto/valongo/?quartos=3&preco-max=380000",
        "https://www.era.pt/comprar/habitacao/porto/gondomar/?quartos=3&preco-max=380000",
    ]

    # ERA also exposes a JSON endpoint via their search API
    API_URL = (
        "https://www.era.pt/api/listing/search?"
        "transactionType=buy&propertyType=residential"
        "&location=valongo&minBedrooms=3&maxPrice=380000&pageSize=30"
    )

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        # Try JSON API first
        api_props = self._try_api()
        for p in api_props:
            if p.property_id not in seen:
                seen.add(p.property_id)
                properties.append(p)

        # Fall back to HTML scraping
        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}&pagina={page}"
                soup = self.get_soup(url)
                if soup is None:
                    break

                props = self._parse_page(soup)
                if not props:
                    break

                for p in props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)

                if not soup.select_one(".pagination .next"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _try_api(self) -> List[Property]:
        data = self.get_json(self.API_URL)
        if not data:
            return []
        try:
            items = data.get("listings") or data.get("results") or data.get("data") or []
            return [p for p in (self._parse_api_item(i) for i in items) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] API parse failed: {e}")
            return []

    def _parse_api_item(self, item: dict) -> Optional[Property]:
        url = item.get("url") or item.get("link", "")
        if not url.startswith("http"):
            url = self.base_url + url
        if not url:
            return None

        title = item.get("title") or item.get("name", "")
        price = item.get("price") or item.get("totalPrice")
        try:
            price = float(price) if price else None
        except (TypeError, ValueError):
            price = None
        if price and price > 380_000:
            return None

        rooms = item.get("bedrooms") or item.get("rooms")
        area = item.get("area") or item.get("totalArea")

        loc_parts = [item.get("city", ""), item.get("district", "")]
        location = ", ".join(p for p in loc_parts if p) or "Valongo"

        photos = item.get("photos") or item.get("images") or []
        images = []
        for p in photos[:3]:
            if isinstance(p, str):
                images.append(p)
            elif isinstance(p, dict):
                images.append(p.get("url") or p.get("src", ""))

        desc = item.get("description", "")
        combined = title + " " + desc
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)

        return Property(
            url=url,
            source=self.name,
            title=title,
            price=price,
            location=location,
            rooms=int(rooms) if rooms else None,
            area_m2=float(area) if area else None,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=desc[:500],
            images=[i for i in images if i],
        )

    def _parse_page(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        cards = (
            soup.select("div.property-card")
            or soup.select("article[class*='property']")
            or soup.select("[class*='listing-item']")
        )
        for card in cards:
            prop = self._parse_card(card)
            if prop:
                results.append(prop)
        return results

    def _parse_card(self, card) -> Optional[Property]:
        link = card.select_one("a[href*='/imovel/']") or card.select_one("a[href*='/property/']") or card.select_one("a")
        if not link:
            return None
        href = link.get("href", "")
        if not href.startswith("http"):
            href = self.base_url + href

        title_el = card.select_one("h2") or card.select_one("h3") or card.select_one(".title")
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = card.select_one("[class*='price']")
        price = self.parse_price(price_el.get_text()) if price_el else None
        if price and price > 380_000:
            return None

        area = rooms = None
        for el in card.select("span, li"):
            t = el.get_text(strip=True)
            if "m²" in t and area is None:
                area = self.parse_area(t)
            if rooms is None:
                rooms = self.parse_rooms(t)

        img = card.select_one("img[src]")
        images = [img["src"]] if img else []

        combined = title
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)

        return Property(
            url=href,
            source=self.name,
            title=title,
            price=price,
            location="Valongo",
            rooms=rooms,
            area_m2=area,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            images=images,
        )
