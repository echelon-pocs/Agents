import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class RemaxScraper(BaseScraper):
    name = "RE/MAX"
    base_url = "https://www.remax.pt"

    SEARCH_URLS = [
        "https://www.remax.pt/comprar/imoveis/apartamento/porto/valongo?quartos-min=2&preco-max=380000",
        "https://www.remax.pt/comprar/imoveis/moradia/porto/valongo?quartos-min=2&preco-max=380000",
    ]

    # RE/MAX GraphQL / internal API
    API_URL = "https://www.remax.pt/api/v2/listings/search"

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        # Try internal API
        for payload in self._api_payloads():
            api_props = self._call_api(payload)
            for p in api_props:
                if p.property_id not in seen:
                    seen.add(p.property_id)
                    properties.append(p)

        # HTML fallback
        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}&pagina={page}"
                soup = self.get_soup(url)
                if soup is None:
                    break

                props = self._parse_page(soup) or self._heuristic_extract(soup)
                if not props:
                    logger.warning(f"[{self.name}] Zero on {url} — title: {soup.title.string if soup.title else 'n/a'}")
                    break

                for p in props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)

                if not soup.select_one(".pagination .next, a[rel='next']"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _api_payloads(self):
        base = {
            "transactionTypeId": 1,  # buy
            "locationId": "porto/valongo",
            "minBedrooms": 2,
            "maxPrice": 380000,
            "pageSize": 30,
        }
        yield {**base, "propertyTypeId": 1}  # apartments
        yield {**base, "propertyTypeId": 2}  # houses

    def _call_api(self, payload: dict) -> List[Property]:
        try:
            resp = self.session.post(self.API_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("listings") or data.get("results") or data.get("data") or []
            return [p for p in (self._parse_api_item(i) for i in items) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] API call failed: {e}")
            return []

    def _parse_api_item(self, item: dict) -> Optional[Property]:
        url = item.get("url") or item.get("permalink", "")
        if not url.startswith("http"):
            url = self.base_url + url
        if not url:
            return None

        title = item.get("title") or item.get("description", "")[:100]
        price = item.get("price") or item.get("listingPrice")
        try:
            price = float(price) if price else None
        except (TypeError, ValueError):
            price = None
        if price and price > 380_000:
            return None

        rooms = item.get("bedrooms")
        area = item.get("netArea") or item.get("grossArea") or item.get("area")
        location = item.get("locationLabel") or item.get("city", "Valongo")

        photos = item.get("photos") or item.get("images") or []
        images = []
        for p in photos[:3]:
            if isinstance(p, str):
                images.append(p)
            elif isinstance(p, dict):
                images.append(p.get("url") or p.get("imageUrl", ""))

        desc = item.get("longDescription") or item.get("description", "")
        combined = str(title) + " " + str(desc)
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        return Property(
            url=url,
            source=self.name,
            title=str(title),
            price=price,
            location=str(location),
            rooms=int(rooms) if rooms else None,
            area_m2=float(area) if area else None,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=str(desc)[:500],
            images=[i for i in images if i],
        )

    def _parse_page(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        cards = (
            soup.select("div.property-card")
            or soup.select("article[class*='listing']")
            or soup.select("[data-testid*='listing']")
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
