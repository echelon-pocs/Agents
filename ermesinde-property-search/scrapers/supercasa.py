import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class SupercasaScraper(BaseScraper):
    name = "Supercasa"
    base_url = "https://supercasa.pt"

    SEARCH_URLS = [
        "https://supercasa.pt/comprar-casas/ermesinde/porto?quartos=3,4,5&preco-max=380000",
        "https://supercasa.pt/comprar-casas/valongo/porto?quartos=3,4,5&preco-max=380000",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}&pagina={page}"
                soup = self.get_soup(url)
                if soup is None:
                    break

                props = self._extract_nextjs(soup) or self._parse_html(soup)
                if not props:
                    break

                for p in props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)

                if not soup.select_one("a[rel='next']"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _extract_nextjs(self, soup: BeautifulSoup) -> Optional[List[Property]]:
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            return None
        try:
            data = json.loads(script.string)
            listings = (
                data.get("props", {})
                .get("pageProps", {})
                .get("listings", [])
            )
            if not listings:
                return None
            return [p for p in (self._parse_listing(l) for l in listings) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] Next.js parse failed: {e}")
            return None

    def _parse_listing(self, listing: dict) -> Optional[Property]:
        url = listing.get("url") or listing.get("slug", "")
        if not url.startswith("http"):
            url = self.base_url + "/" + url.lstrip("/")

        title = listing.get("title", "")
        price = listing.get("price") or listing.get("totalPrice")
        try:
            price = float(price) if price else None
        except (ValueError, TypeError):
            price = None
        if price and price > 380_000:
            return None

        rooms = listing.get("bedrooms") or listing.get("rooms")
        area = listing.get("area") or listing.get("totalArea")
        location = listing.get("location") or listing.get("city", "Valongo")

        photos = listing.get("photos") or listing.get("images") or []
        images = [p.get("url", "") for p in photos[:3] if isinstance(p, dict)]

        desc = listing.get("description", "")
        combined = title + " " + desc
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        return Property(
            url=url,
            source=self.name,
            title=title,
            price=price,
            location=str(location),
            rooms=int(rooms) if rooms else None,
            area_m2=float(area) if area else None,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=desc[:500],
            images=[i for i in images if i],
        )

    def _parse_html(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        for card in soup.select("article") or soup.select("[class*='property-card']"):
            prop = self._parse_card(card)
            if prop:
                results.append(prop)
        return results

    def _parse_card(self, card) -> Optional[Property]:
        link = card.select_one("a[href*='/imovel/']") or card.select_one("a")
        if not link:
            return None
        href = link.get("href", "")
        if not href.startswith("http"):
            href = self.base_url + href

        title_el = card.select_one("h2") or card.select_one("h3")
        title = title_el.get_text(strip=True) if title_el else ""

        price_el = card.select_one("[class*='price']")
        price = self.parse_price(price_el.get_text()) if price_el else None
        if price and price > 380_000:
            return None

        area = rooms = None
        for el in card.select("span"):
            t = el.get_text(strip=True)
            if "m²" in t:
                area = self.parse_area(t)
            elif rooms is None:
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
