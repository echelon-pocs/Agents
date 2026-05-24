import json
import logging
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class ImovirtualScraper(BaseScraper):
    name = "Imovirtual"
    base_url = "https://www.imovirtual.com"

    SEARCH_URLS = [
        # New URL format (2024+)
        "https://www.imovirtual.com/pt/resultados/comprar/apartamento,t3/porto/valongo?preco-max-380000",
        "https://www.imovirtual.com/pt/resultados/comprar/apartamento,t4/porto/valongo?preco-max-380000",
        # Legacy URL format
        "https://www.imovirtual.com/comprar/apartamento/valongo/?search%5Bfilter_float_price%3Ato%5D=380000&search%5Bfilter_enum_rooms_num%5D%5B0%5D=THREE&search%5Bfilter_enum_rooms_num%5D%5B1%5D=FOUR&search%5Bfilter_enum_rooms_num%5D%5B2%5D=FIVE",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}&page={page}"
                soup = self.get_soup(url)
                if soup is None:
                    break

                # Try to extract JSON data embedded in Next.js __NEXT_DATA__
                props = self._extract_nextjs_data(soup) or self._parse_html(soup)
                if not props:
                    break

                for p in props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)

                if not soup.select_one("[data-cy='pagination.next-page']"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _extract_nextjs_data(self, soup: BeautifulSoup) -> Optional[List[Property]]:
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            return None
        try:
            data = json.loads(script.string)
            items = (
                data.get("props", {})
                .get("pageProps", {})
                .get("data", {})
                .get("searchAds", {})
                .get("items", [])
            )
            if not items:
                return None
            return [p for p in (self._parse_json_item(i) for i in items) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] Next.js data parse failed: {e}")
            return None

    def _parse_json_item(self, item: dict) -> Optional[Property]:
        url = item.get("url") or item.get("slug") or ""
        if not url.startswith("http"):
            url = self.base_url + url
        if not url:
            return None

        title = item.get("title", "")
        price = None
        price_info = item.get("totalPrice") or item.get("price") or {}
        if isinstance(price_info, dict):
            price = price_info.get("value")
        elif isinstance(price_info, (int, float)):
            price = float(price_info)

        if price and price > 380_000:
            return None

        area = item.get("areaInSquareMeters") or item.get("totalArea")
        rooms_val = item.get("roomsNumber") or item.get("rooms")
        rooms = int(rooms_val) if rooms_val else None

        location_parts = [
            item.get("locationLabel", {}).get("value", ""),
            item.get("address", {}).get("city", {}).get("name", ""),
        ]
        location = ", ".join(p for p in location_parts if p) or "Valongo"

        images_raw = item.get("photos") or item.get("images") or []
        images = [i.get("large") or i.get("medium") or i.get("small", "") for i in images_raw if isinstance(i, dict)]
        images = [i for i in images if i]

        desc = item.get("description", "")
        combined = title + " " + desc
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        return Property(
            url=url,
            source=self.name,
            title=title,
            price=price,
            location=location,
            rooms=rooms,
            area_m2=float(area) if area else None,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=desc[:500],
            images=images[:3],
        )

    def _parse_html(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        for article in soup.select("article[data-cy='listing-item']") or soup.select("li.offer-item"):
            prop = self._parse_article(article)
            if prop:
                results.append(prop)
        return results

    def _parse_article(self, article) -> Optional[Property]:
        link = article.select_one("a[href*='/pt/imovel/']") or article.select_one("a.offer-item-link")
        if not link:
            return None
        href = link.get("href", "")
        if not href.startswith("http"):
            href = self.base_url + href
        title = link.get("title", "") or link.get_text(strip=True)

        price_el = article.select_one("[data-cy='listing-item-price']") or article.select_one(".offer-item-price")
        price = self.parse_price(price_el.get_text()) if price_el else None
        if price and price > 380_000:
            return None

        area = rooms = None
        for li in article.select("li"):
            t = li.get_text(strip=True)
            if "m²" in t:
                area = self.parse_area(t)
            if rooms is None:
                rooms = self.parse_rooms(t)

        location_el = article.select_one("[data-cy='listing-item-address']") or article.select_one("p.offer-item-location")
        location = location_el.get_text(strip=True) if location_el else "Valongo"

        img = article.select_one("img[src]")
        images = [img["src"]] if img and img.get("src") else []

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
