import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class BpiScraper(BaseScraper):
    """
    BPI Imobiliário — bank-managed real estate portal.
    Covers distressed / foreclosure properties plus standard agency listings.
    """
    name = "BPI Imobiliário"
    base_url = "https://www.bpiexpressoimobiliario.pt"

    SEARCH_URLS = [
        "https://www.bpiexpressoimobiliario.pt/imoveis?distrito=Porto&municipio=Valongo&tipologia=T3&tipologia=T4&tipologia=T5&preco_max=380000&finalidade=venda",
        "https://www.bpiexpressoimobiliario.pt/imoveis?distrito=Porto&municipio=Valongo&preco_max=380000&finalidade=venda",
    ]

    # Millennium BCP also has a bank RE portal worth covering
    _MBK_URLS = [
        "https://www.millenniumbcp.pt/imoveis/pesquisa?distrito=Porto&municipio=Valongo&quartos=3&preco_max=380000",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for url_list in (self.SEARCH_URLS, self._MBK_URLS):
            for base_url in url_list:
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
                     .get("properties") or data.get("props", {}).get("pageProps", {})
                     .get("imoveis") or [])
            if not items:
                return None
            return [p for p in (self._parse_item(i) for i in items) if p]
        except Exception as e:
            logger.debug(f"[{self.name}] Next.js parse failed: {e}")
            return None

    def _parse_item(self, item: dict) -> Optional[Property]:
        url = item.get("url") or item.get("link", "")
        if not url.startswith("http"):
            url = self.base_url + url
        if not url:
            return None
        title = item.get("title") or item.get("descricao", "")
        price = None
        try:
            price = float(item.get("price") or item.get("preco") or 0) or None
        except (TypeError, ValueError):
            pass
        if price and price > 380_000:
            return None
        rooms_raw = item.get("typology") or item.get("tipologia") or item.get("bedrooms", "")
        rooms = self.parse_rooms(str(rooms_raw)) if rooms_raw else None
        area = item.get("area") or item.get("areaUtil")
        location = item.get("location") or item.get("freguesia") or "Valongo"
        desc = item.get("description") or item.get("descricao_completa", "")
        combined = str(title) + " " + str(desc)
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        photos = item.get("photos") or item.get("fotos") or []
        images = [p.get("url", p) if isinstance(p, dict) else str(p) for p in photos[:3]]
        return Property(
            url=url, source=self.name, title=str(title), price=price,
            location=str(location), rooms=rooms,
            area_m2=float(area) if area else None,
            has_garage=has_garage, garage_spaces=garage_spaces,
            has_outdoor=has_outdoor, description=str(desc)[:500],
            images=[i for i in images if i],
        )

    def _parse_html(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        cards = (soup.select("article[class*='imovel']")
                 or soup.select("div[class*='property-card']")
                 or soup.select("li[class*='imovel']"))
        for card in cards:
            link = card.select_one("a[href*='/imovel/']") or card.select_one("a")
            if not link:
                continue
            href = link.get("href", "")
            if not href.startswith("http"):
                href = "https://www.bpiexpressoimobiliario.pt" + href
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
