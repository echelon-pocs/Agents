import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class Century21Scraper(BaseScraper):
    name = "Century21"
    base_url = "https://www.century21.pt"

    SEARCH_URLS = [
        "https://www.century21.pt/imoveis?municipio=Valongo&tipologias=T3,T4,T5&preco_max=380000&tipo_negocio=VENDA&tipo_imovel=APARTAMENTO",
        "https://www.century21.pt/imoveis?municipio=Valongo&tipologias=T3,T4,T5&preco_max=380000&tipo_negocio=VENDA&tipo_imovel=MORADIA",
        "https://www.century21.pt/imoveis?municipio=Gondomar&tipologias=T3,T4,T5&preco_max=380000&tipo_negocio=VENDA&tipo_imovel=APARTAMENTO",
    ]

    # C21 Portugal exposes a REST search endpoint
    API_URL = "https://www.century21.pt/api/search/properties"

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        api_props = self._try_api()
        for p in api_props:
            if p.property_id not in seen:
                seen.add(p.property_id)
                properties.append(p)

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

    def _try_api(self) -> List[Property]:
        for payload in [
            {"municipio": "Valongo", "tipologias": ["T3", "T4", "T5"], "precoMax": 380000, "tipoNegocio": "VENDA"},
            {"municipio": "Gondomar", "tipologias": ["T3", "T4", "T5"], "precoMax": 380000, "tipoNegocio": "VENDA"},
        ]:
            try:
                resp = self.session.post(self.API_URL, json=payload, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                items = data.get("properties") or data.get("results") or data.get("data") or []
                return [p for p in (self._parse_api_item(i) for i in items) if p]
            except Exception as e:
                logger.debug(f"[{self.name}] API failed: {e}")
        return []

    def _parse_api_item(self, item: dict) -> Optional[Property]:
        url = item.get("url") or item.get("permalink", "")
        if not url.startswith("http"):
            url = self.base_url + url
        if not url:
            return None
        title = item.get("title") or item.get("designation", "")
        price = None
        try:
            price = float(item.get("price") or item.get("totalPrice") or 0) or None
        except (TypeError, ValueError):
            pass
        if price and price > 380_000:
            return None
        rooms = item.get("typology") or item.get("bedrooms")
        if isinstance(rooms, str):
            rooms = self.parse_rooms(rooms)
        area = item.get("area") or item.get("usableArea")
        location = item.get("location") or item.get("parish") or "Valongo"
        desc = item.get("description", "")
        combined = str(title) + " " + str(desc)
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        photos = item.get("photos") or item.get("images") or []
        images = [p.get("url", p) if isinstance(p, dict) else p for p in photos[:3]]
        return Property(
            url=url, source=self.name, title=str(title), price=price,
            location=str(location), rooms=int(rooms) if rooms else None,
            area_m2=float(area) if area else None, has_garage=has_garage,
            garage_spaces=garage_spaces, has_outdoor=has_outdoor,
            description=str(desc)[:500], images=[i for i in images if i],
        )

    def _extract_nextjs(self, soup: BeautifulSoup) -> Optional[List[Property]]:
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            return None
        try:
            data = json.loads(script.string)
            items = (data.get("props", {}).get("pageProps", {})
                     .get("properties") or data.get("props", {}).get("pageProps", {})
                     .get("listings") or [])
            if not items:
                return None
            return [p for p in (self._parse_api_item(i) for i in items) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] Next.js parse failed: {e}")
            return None

    def _parse_html(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        cards = (soup.select("article[class*='property']")
                 or soup.select("div[class*='property-card']")
                 or soup.select("li[class*='listing']"))
        for card in cards:
            link = card.select_one("a[href*='/imovel/']") or card.select_one("a")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                href = self.base_url + href
            title_el = card.select_one("h2, h3, .title")
            title = title_el.get_text(strip=True) if title_el else ""
            price_el = card.select_one("[class*='price']")
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
            images = [img["src"]] if img else []
            results.append(Property(
                url=href, source=self.name, title=title, price=price,
                location="Valongo", rooms=rooms, area_m2=area,
                has_garage=has_garage, garage_spaces=garage_spaces,
                has_outdoor=has_outdoor, images=images,
            ))
        return results
