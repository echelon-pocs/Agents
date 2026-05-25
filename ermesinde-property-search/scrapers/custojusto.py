import json
import logging
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class CustoJustoScraper(BaseScraper):
    name = "CustoJusto"
    base_url = "https://www.custojusto.pt"

    SEARCH_URLS = [
        "https://www.custojusto.pt/porto/valongo/imoveis/comprar/apartamentos?pricemax=380000",
        "https://www.custojusto.pt/porto/gondomar/imoveis/comprar/apartamentos?pricemax=380000",
        "https://www.custojusto.pt/porto/maia/imoveis/comprar/apartamentos?pricemax=380000",
        "https://www.custojusto.pt/porto/imobiliario/apartamentos?pricemax=380000&q=T3+valongo",
        "https://www.custojusto.pt/porto/imobiliario/apartamentos?pricemax=380000&q=T3+gondomar",
        "https://www.custojusto.pt/porto/imobiliario/apartamentos?pricemax=380000&q=T3+maia",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}&o={page}"
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

                if not soup.select_one("a[rel='next']") and not soup.select_one(".next"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _parse_page(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        # CustoJusto listing cards
        cards = (
            soup.select("article.iAdItem")
            or soup.select("li[class*='adItem']")
            or soup.select("div[class*='listing-item']")
            or soup.select("article")
        )
        for card in cards:
            prop = self._parse_card(card)
            if prop:
                results.append(prop)
        return results

    def _parse_card(self, card) -> Optional[Property]:
        link = (
            card.select_one("a[href*='/imoveis/']")
            or card.select_one("a[href*='/imobiliario/']")
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

        # Filter out T0/T1 by title (main.py MIN_ROOMS handles the final cut)
        if title:
            rooms_found = self.parse_rooms(title)
            if rooms_found is not None and rooms_found < 2:
                return None

        price_el = (
            card.select_one(".price")
            or card.select_one("[class*='price']")
            or card.select_one("strong")
        )
        price = None
        if price_el:
            pt = price_el.get_text()
            if "€" in pt or re.search(r"\d{4,}", pt):
                price = self.parse_price(pt)
        if price and price > 380_000:
            return None

        area = rooms = None
        for el in card.select("li, span, div"):
            t = el.get_text(strip=True)
            if "m²" in t and area is None:
                area = self.parse_area(t)
            if rooms is None:
                rooms = self.parse_rooms(t)

        location_el = card.select_one("[class*='location']") or card.select_one("p")
        location = location_el.get_text(strip=True) if location_el else "Valongo"

        img = card.select_one("img[src]") or card.select_one("img[data-src]")
        images = []
        if img:
            src = img.get("src") or img.get("data-src", "")
            if src:
                images.append(src)

        combined = title
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)

        return Property(
            url=href,
            source=self.name,
            title=title,
            price=price,
            location=location,
            rooms=rooms,
            area_m2=area,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            images=images,
        )
