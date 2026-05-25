import json
import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)


class OlxScraper(BaseScraper):
    name = "OLX"
    base_url = "https://www.olx.pt"

    SEARCH_URLS = [
        "https://www.olx.pt/imoveis/apartamento-casa-a-venda/valongo-porto/?search%5Bfilter_float_price%3Ato%5D=380000",
        "https://www.olx.pt/imoveis/apartamento-casa-a-venda/gondomar-porto/?search%5Bfilter_float_price%3Ato%5D=380000",
        "https://www.olx.pt/imoveis/apartamento-casa-a-venda/maia-porto/?search%5Bfilter_float_price%3Ato%5D=380000",
        "https://www.olx.pt/imoveis/q-ermesinde/?search%5Bfilter_float_price%3Ato%5D=380000&search%5Bfilter_enum_category%5D=imoveis",
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

                props = (
                    self._extract_nextjs(soup)
                    or self._parse_html(soup)
                    or self._heuristic_extract(soup)   # inline fallback
                )
                if not props:
                    break

                for p in props:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        properties.append(p)

                if not soup.select_one("[data-testid='pagination-forward']"):
                    break

        logger.info(f"[{self.name}] Found {len(properties)} listings")
        return properties

    def _extract_nextjs(self, soup: BeautifulSoup) -> Optional[List[Property]]:
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            title = soup.title.string if soup.title else "no title"
            logger.warning(f"[{self.name}] No __NEXT_DATA__ on page — title: {title!r}")
            return None
        try:
            data = json.loads(script.string)
            page_props = data.get("props", {}).get("pageProps", {})
            # Log top-level keys so we can fix the path if it changes again
            logger.warning(f"[{self.name}] __NEXT_DATA__ pageProps keys: {list(page_props.keys())[:10]}")
            ads = self._find_ads(page_props)
            if not ads:
                return None
            return [p for p in (self._parse_ad(ad) for ad in ads) if p]
        except Exception as e:
            logger.warning(f"[{self.name}] Next.js parse failed: {e}")
            return None

    @staticmethod
    def _find_ads(page_props: dict) -> list:
        """Try multiple known paths for OLX's ads array."""
        candidates = [
            page_props.get("ads"),
            page_props.get("listings"),
            page_props.get("data", {}).get("ads") if isinstance(page_props.get("data"), dict) else None,
            page_props.get("data", {}).get("listings") if isinstance(page_props.get("data"), dict) else None,
            (page_props.get("initialState") or {}).get("listing", {}).get("ads"),
            (page_props.get("initialState") or {}).get("listing", {}).get("listings"),
            (page_props.get("initialState") or {}).get("listing", {}).get("items"),
            (page_props.get("searchAds") or {}).get("ads"),
        ]
        for c in candidates:
            if isinstance(c, list) and c:
                return c
        return []

    def _parse_ad(self, ad: dict) -> Optional[Property]:
        url = ad.get("url", "")
        if not url.startswith("http"):
            url = self.base_url + url
        if not url:
            return None

        title = ad.get("title", "")
        price = None
        for p in ad.get("params", []):
            if p.get("key") == "price":
                try:
                    price = float(str(p.get("value", {}).get("value", "")).replace(" ", ""))
                except (ValueError, AttributeError):
                    pass
        if price and price > 380_000:
            return None

        rooms = area = None
        for p in ad.get("params", []):
            key = p.get("key", "")
            val = p.get("value", {})
            if key == "rooms":
                try:
                    rooms = int(str(val.get("key", val)).replace("T", ""))
                except ValueError:
                    pass
            elif key == "m":
                try:
                    area = float(str(val.get("value", val)))
                except ValueError:
                    pass

        location_parts = [
            ad.get("location", {}).get("city", {}).get("name", ""),
            ad.get("location", {}).get("district", {}).get("name", ""),
        ]
        location = ", ".join(p for p in location_parts if p) or "Porto"

        photos = ad.get("photos") or []
        images = [p.get("link", "").replace("{width}", "800").replace("{height}", "600") for p in photos[:3]]

        desc = ad.get("description", "")
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
            area_m2=area,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=desc[:500],
            images=[i for i in images if i],
        )

    def _parse_html(self, soup: BeautifulSoup) -> List[Property]:
        results = []
        for card in soup.select("[data-cy='l-card']") or soup.select(".offer-wrapper"):
            prop = self._parse_card(card)
            if prop:
                results.append(prop)
        return results

    def _parse_card(self, card) -> Optional[Property]:
        link = card.select_one("a[href*='/imoveis/']") or card.select_one("a")
        if not link:
            return None
        href = link.get("href", "")
        if not href.startswith("http"):
            href = self.base_url + href
        title = card.select_one("h6") or card.select_one("strong")
        title_text = title.get_text(strip=True) if title else ""

        price_el = card.select_one("[data-testid='ad-price']") or card.select_one("p.price")
        price = self.parse_price(price_el.get_text()) if price_el else None
        if price and price > 380_000:
            return None

        img = card.select_one("img[src]")
        images = [img["src"]] if img else []

        combined = title_text
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)

        return Property(
            url=href,
            source=self.name,
            title=title_text,
            price=price,
            location="Porto",
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            images=images,
        )
