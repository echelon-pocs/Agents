import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class IdealistaScraper(BaseScraper):
    name = "Idealista"
    base_url = "https://www.idealista.pt"

    SEARCH_URLS = [
        "https://www.idealista.pt/comprar-casas/porto/valongo/ermesinde/?preco-maximo=380000&ordenado-por=atualizado-desc",
        "https://www.idealista.pt/comprar-casas/porto/valongo/?preco-maximo=380000&ordenado-por=atualizado-desc",
    ]

    def search(self) -> List[Property]:
        properties: List[Property] = []
        seen: set = set()

        for base_search_url in self.SEARCH_URLS:
            for page in range(1, self.MAX_PAGES + 1):
                url = base_search_url if page == 1 else f"{base_search_url}&pagina={page}"
                soup = self.get_soup(url)
                if soup is None:
                    break

                new_props = self._parse_page(soup)
                if not new_props:
                    break

                for p in new_props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)

                if not soup.select_one("a.icon-arrow-right-after"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _parse_page(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        for article in soup.select("article.item"):
            prop = self._parse_article(article)
            if prop:
                results.append(prop)
        return results

    def _parse_article(self, article) -> Optional[Property]:
        link = article.select_one("a.item-link") or article.select_one("a[href*='/imovel/']")
        if not link:
            return None

        href = link.get("href", "")
        if not href.startswith("http"):
            href = self.base_url + href
        title = link.get("title", "") or link.get_text(strip=True)

        price_el = (
            article.select_one("span.item-price")
            or article.select_one(".price-row .price")
            or article.select_one("[class*='item-price']")
        )
        price = self.parse_price(price_el.get_text()) if price_el else None
        if price and price > 380_000:
            return None

        rooms = area = None
        for span in article.select(".item-detail"):
            t = span.get_text(strip=True)
            if rooms is None:
                rooms = self.parse_rooms(t)
            if area is None and "m²" in t:
                area = self.parse_area(t)

        location_el = article.select_one(".item-detail-location") or article.select_one("[class*='location']")
        location = location_el.get_text(strip=True) if location_el else "Valongo"

        desc_el = article.select_one("p.description") or article.select_one(".item-description")
        description = desc_el.get_text(strip=True) if desc_el else ""

        combined_text = title + " " + description
        has_garage, garage_spaces = self.detect_garage(combined_text)
        has_outdoor = self.detect_outdoor(combined_text)
        balcony_area = self.detect_balcony_area(combined_text)

        images = []
        img = article.select_one("img[src]") or article.select_one("img[data-src]")
        if img:
            src = img.get("src") or img.get("data-src", "")
            if src and not src.endswith("gif"):
                images.append(src)

        return Property(
            url=href,
            source=self.name,
            title=title,
            price=price,
            location=location,
            rooms=rooms,
            area_m2=area,
            balcony_area_m2=balcony_area,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=description,
            images=images,
        )
