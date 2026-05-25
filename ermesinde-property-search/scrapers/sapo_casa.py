import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class SapoCasaScraper(BaseScraper):
    name = "Casa.sapo"
    base_url = "https://casa.sapo.pt"

    SEARCH_URLS = [
        "https://casa.sapo.pt/comprar-apartamentos/t2/valongo/ermesinde/",
        "https://casa.sapo.pt/comprar-apartamentos/t3/valongo/ermesinde/",
        "https://casa.sapo.pt/comprar-apartamentos/t3/valongo/",
        "https://casa.sapo.pt/comprar-moradias/t3/valongo/ermesinde/",
        "https://casa.sapo.pt/comprar-moradias/t2/valongo/ermesinde/",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}?pn={page}"
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

                next_el = soup.select_one("a[rel='next']") or soup.select_one(".next a")
                if not next_el:
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _parse_page(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        # Sapo Casa uses various container selectors depending on version
        cards = (
            soup.select("div.property-list-content")
            or soup.select("article.searchResultProperty")
            or soup.select("div[class*='searchResultProperty']")
            or soup.select("li.searchResultProperty")
        )
        for card in cards:
            prop = self._parse_card(card)
            if prop:
                results.append(prop)
        return results

    def _parse_card(self, card) -> Optional[Property]:
        link = (
            card.select_one("a[href*='/comprar']")
            or card.select_one("a.searchResultProperty")
            or card.select_one("h2 a")
            or card.select_one("a")
        )
        if not link:
            return None
        href = link.get("href", "")
        if not href.startswith("http"):
            href = self.base_url + href

        title_el = card.select_one("h2") or card.select_one("h3") or card.select_one(".title")
        title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)

        price_el = (
            card.select_one("span.price")
            or card.select_one("[class*='price']")
            or card.select_one("div.value")
        )
        price = None
        if price_el:
            price = self.parse_price(price_el.get_text())
        if price and price > 380_000:
            return None

        area = rooms = None
        for el in card.select("span, li, div"):
            t = el.get_text(strip=True)
            if "m²" in t and area is None:
                area = self.parse_area(t)
            if rooms is None and ("T3" in t or "T4" in t or "T5" in t or "quarto" in t.lower()):
                rooms = self.parse_rooms(t)

        location_el = card.select_one("[class*='location']") or card.select_one("p.localization")
        location = location_el.get_text(strip=True) if location_el else "Valongo"

        img = card.select_one("img[src]") or card.select_one("img[data-src]")
        images = []
        if img:
            src = img.get("src") or img.get("data-src", "")
            if src:
                images.append(src)

        desc_el = card.select_one("p.description") or card.select_one("[class*='description']")
        desc = desc_el.get_text(strip=True) if desc_el else ""

        combined = title + " " + desc
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        return Property(
            url=href,
            source=self.name,
            title=title,
            price=price,
            location=location,
            rooms=rooms,
            area_m2=area,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=desc[:500],
            images=images,
        )
