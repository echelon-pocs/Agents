import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class LugarCertoScraper(BaseScraper):
    name = "LugarCerto"
    base_url = "https://www.lugarcerto.pt"

    SEARCH_URLS = [
        "https://www.lugarcerto.pt/imoveis/venda/apartamento/porto/valongo/?tipologia=3,4,5&preco_maximo=380000",
        "https://www.lugarcerto.pt/imoveis/venda/moradia/porto/valongo/?tipologia=3,4,5&preco_maximo=380000",
        "https://www.lugarcerto.pt/imoveis/venda/apartamento/porto/gondomar/?tipologia=3,4,5&preco_maximo=380000",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for base_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_url if page == 1 else f"{base_url}&pagina={page}"
                soup = self.get_soup(url)
                if not soup:
                    break
                props = self._extract_nextjs(soup) or self._parse_html(soup)
                if not props:
                    break
                for p in props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)
                if not soup.select_one("a[rel='next'], .pagination .next"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _extract_nextjs(self, soup: BeautifulSoup) -> Optional[List[Property]]:
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            return None
        try:
            data = json.loads(script.string)
            items = (data.get("props", {}).get("pageProps", {})
                     .get("listings") or data.get("props", {}).get("pageProps", {})
                     .get("properties") or [])
            if not items:
                return None
            return [p for p in (self._parse_item(i) for i in items) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] Next.js parse failed: {e}")
            return None

    def _parse_item(self, item: dict) -> Optional[Property]:
        url = item.get("url") or item.get("slug", "")
        if not url.startswith("http"):
            url = self.base_url + "/" + url.lstrip("/")
        if not url:
            return None
        title = item.get("title") or item.get("name", "")
        price = None
        try:
            price = float(item.get("price") or item.get("preco") or 0) or None
        except (TypeError, ValueError):
            pass
        if price and price > 380_000:
            return None
        rooms_raw = item.get("typology") or item.get("quartos") or ""
        rooms = self.parse_rooms(str(rooms_raw)) if rooms_raw else None
        area = item.get("area") or item.get("areaUtil")
        location = item.get("location") or item.get("localidade") or "Valongo"
        desc = item.get("description", "")
        combined = str(title) + " " + str(desc)
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)
        photos = item.get("photos") or item.get("fotos") or []
        images = [p.get("url", p) if isinstance(p, dict) else str(p) for p in photos[:3]]
        return Property(
            url=url, source=self.name, title=str(title), price=price,
            location=str(location), rooms=rooms,
            area_m2=float(area) if area else None, balcony_area_m2=balcony,
            has_garage=has_garage, garage_spaces=garage_spaces,
            has_outdoor=has_outdoor, description=str(desc)[:500],
            images=[i for i in images if i],
        )

    def _parse_html(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        cards = (soup.select("article[class*='property']")
                 or soup.select("div[class*='imovel-card']")
                 or soup.select("li[class*='property']"))
        for card in cards:
            link = card.select_one("a[href*='/imovel/']") or card.select_one("a[href*='/venda/']") or card.select_one("a")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                href = self.base_url + href
            title_el = card.select_one("h2, h3, .title")
            title = title_el.get_text(strip=True) if title_el else ""
            price_el = card.select_one("[class*='price'], [class*='preco']")
            price = self.parse_price(price_el.get_text()) if price_el else None
            if price and price > 380_000:
                continue
            area = rooms = None
            for el in card.select("span, li"):
                t = el.get_text(strip=True)
                if "m²" in t and area is None:
                    area = self.parse_area(t)
                if rooms is None:
                    rooms = self.parse_rooms(t)
            combined = title
            has_garage, garage_spaces = self.detect_garage(combined)
            has_outdoor = self.detect_outdoor(combined)
            img = card.select_one("img[src]")
            results.append(Property(
                url=href, source=self.name, title=title, price=price,
                location="Valongo", rooms=rooms, area_m2=area,
                has_garage=has_garage, garage_spaces=garage_spaces,
                has_outdoor=has_outdoor, images=[img["src"]] if img else [],
            ))
        return results
