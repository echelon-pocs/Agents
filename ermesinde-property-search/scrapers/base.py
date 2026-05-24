import json
import re
import time
import random
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from models import Property

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MAX_PAGES = 5
MIN_DELAY = 3.0
MAX_DELAY = 7.0


class BaseScraper(ABC):
    name = "Base"
    base_url = ""
    MAX_PAGES = MAX_PAGES

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _sleep(self):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    def get_soup(self, url: str, retries: int = 3, **kwargs) -> Optional[BeautifulSoup]:
        self._sleep()
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                if resp.status_code == 429:
                    wait = (2 ** attempt) * random.uniform(8, 15)
                    logger.warning(f"[{self.name}] Rate-limited — waiting {wait:.0f}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return BeautifulSoup(resp.text, "html.parser")
            except requests.RequestException as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * random.uniform(2, 5))
                else:
                    logger.warning(f"[{self.name}] GET {url} failed after {retries} attempts: {e}")
        return None

    def get_json(self, url: str, retries: int = 2, **kwargs) -> Optional[dict]:
        self._sleep()
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt * random.uniform(2, 4))
                else:
                    logger.warning(f"[{self.name}] JSON GET {url} failed: {e}")
        return None

    # ── parsing helpers ──────────────────────────────────────────────────────

    @staticmethod
    def parse_price(text: str) -> Optional[float]:
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        if digits:
            return float(digits)
        return None

    @staticmethod
    def parse_area(text: str) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"(\d[\d\s]*(?:[.,]\d+)?)\s*m", text.replace("\xa0", " "))
        if m:
            return float(m.group(1).replace(" ", "").replace(",", "."))
        return None

    @staticmethod
    def parse_rooms(text: str) -> Optional[int]:
        if not text:
            return None
        m = re.search(r"T\s*(\d+)", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*(?:quarto|bedroom|divisão)", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def detect_garage(text: str) -> tuple:
        """Returns (has_garage, garage_spaces)."""
        text_l = text.lower()
        has = any(w in text_l for w in ["garagem", "garage", "box ", "lugar de garagem", "estacionamento"])
        spaces = 0
        m = re.search(r"(\d+)\s*lugar(?:es)?\s*(?:de\s*)?garagem", text_l)
        if m:
            spaces = int(m.group(1))
        elif has:
            spaces = 1
        return has, spaces

    @staticmethod
    def detect_outdoor(text: str) -> bool:
        keywords = ["jardim", "quintal", "terraço", "varanda", "logradouro",
                    "espaço exterior", "piscina", "campo", "outdoor"]
        t = text.lower()
        return any(k in t for k in keywords)

    @staticmethod
    def detect_balcony_area(text: str) -> Optional[float]:
        patterns = [
            r"varanda[^.]*?(\d+(?:[.,]\d+)?)\s*m",
            r"terraço[^.]*?(\d+(?:[.,]\d+)?)\s*m",
            r"(\d+(?:[.,]\d+)?)\s*m[²2]?\s*(?:de\s*)?(?:varanda|terraço)",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return float(m.group(1).replace(",", "."))
        return None

    @abstractmethod
    def search(self) -> List[Property]:
        pass

    # ── Tier 2: JSON-LD structured data ──────────────────────────────────────

    def search_jsonld(self) -> List[Property]:
        """Extract properties from JSON-LD / schema.org markup — works after redesigns."""
        results: List[Property] = []
        seen: set = set()
        search_urls = getattr(self, "SEARCH_URLS", [])
        for url in search_urls:
            for page in range(1, self.MAX_PAGES + 1):
                page_url = url if page == 1 else f"{url}&page={page}"
                soup = self.get_soup(page_url)
                if not soup:
                    break
                found = self._extract_jsonld(soup, page_url)
                if not found:
                    break
                for p in found:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        results.append(p)
        return results

    def _extract_jsonld(self, soup: BeautifulSoup, source_url: str) -> List[Property]:
        props = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw = script.string or ""
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            items = data if isinstance(data, list) else data.get("@graph", [data])
            for item in items:
                schema_type = item.get("@type", "")
                if not isinstance(schema_type, str):
                    schema_type = " ".join(schema_type)
                if not any(t in schema_type for t in ("Residence", "House", "Apartment", "Product", "RealEstateListing")):
                    continue
                p = self._jsonld_to_property(item, source_url)
                if p:
                    props.append(p)
        return props

    def _jsonld_to_property(self, item: dict, source_url: str) -> Optional[Property]:
        url = item.get("url") or item.get("mainEntityOfPage", {}).get("@id", "") or source_url
        if not url.startswith("http"):
            url = (self.base_url + url) if self.base_url else source_url

        name = item.get("name") or item.get("headline", "")
        if not name:
            return None

        price = None
        offers = item.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price_raw = offers.get("price") or item.get("price")
        if price_raw:
            try:
                price = float(str(price_raw).replace(" ", "").replace(",", "."))
            except ValueError:
                pass

        area = None
        for key in ("floorSize", "totalFloorArea"):
            val = item.get(key)
            if isinstance(val, dict):
                area = val.get("value")
            elif val:
                area = self.parse_area(str(val))
            if area:
                break

        rooms = item.get("numberOfRooms") or item.get("numberOfBedrooms")
        try:
            rooms = int(rooms) if rooms else None
        except (ValueError, TypeError):
            rooms = None

        desc = item.get("description", "")
        combined = str(name) + " " + str(desc)
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        images = []
        img = item.get("image")
        if isinstance(img, list):
            images = [i.get("url", i) if isinstance(i, dict) else i for i in img[:3]]
        elif isinstance(img, dict):
            images = [img.get("url", "")]
        elif isinstance(img, str):
            images = [img]

        return Property(
            url=url,
            source=self.name,
            title=str(name),
            price=price,
            location=item.get("address", {}).get("addressLocality", "Valongo") if isinstance(item.get("address"), dict) else "Valongo",
            rooms=rooms,
            area_m2=float(area) if area else None,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=str(desc)[:500],
            images=[i for i in images if i],
        )

    # ── Tier 3: heuristic link/price extraction ───────────────────────────────

    # URL fragments that appear in property detail links across all PT real estate sites
    _PROPERTY_URL_FRAGMENTS = [
        "/imovel/", "/imoveis/", "/property/", "/apartamento", "/moradia",
        "/casa-", "/habitacao/", "/residencial/",
    ]

    def search_heuristic(self) -> List[Property]:
        """
        Site-agnostic fallback: scan pages for links that look like property
        detail pages and extract price/typology from surrounding context.
        Survives site redesigns that break CSS selectors.
        """
        results: List[Property] = []
        seen: set = set()
        search_urls = getattr(self, "SEARCH_URLS", [])

        for url in search_urls[:2]:  # limit to avoid being too slow
            for page in range(1, 4):
                page_url = url if page == 1 else f"{url}&page={page}"
                soup = self.get_soup(page_url)
                if not soup:
                    break
                found = self._heuristic_extract(soup)
                if not found:
                    break
                for p in found:
                    if p.property_id not in seen:
                        seen.add(p.property_id)
                        results.append(p)

        logger.info(f"[{self.name}] Heuristic mode found {len(results)} candidates")
        return results

    def _heuristic_extract(self, soup: BeautifulSoup) -> List[Property]:
        props = []
        seen_hrefs: set = set()

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not any(frag in href for frag in self._PROPERTY_URL_FRAGMENTS):
                continue
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            full_url = href if href.startswith("http") else (self.base_url + href)

            # Walk up the DOM to find the containing card element
            container = None
            for tag in ("article", "li", "div", "section"):
                container = a.find_parent(tag)
                if container and len(container.get_text()) > 50:
                    break

            if not container:
                continue

            text = container.get_text(" ", strip=True)

            # Must look like a real listing — needs a price or typology signal
            price = self._heuristic_price(text)
            rooms = self.parse_rooms(text)
            if price is None and rooms is None:
                continue
            if price and price > 380_000:
                continue

            title = a.get("title") or a.get_text(strip=True) or ""
            area = self.parse_area(text)
            has_garage, garage_spaces = self.detect_garage(text)
            has_outdoor = self.detect_outdoor(text)
            balcony = self.detect_balcony_area(text)

            img = container.select_one("img[src]") or container.select_one("img[data-src]")
            images = []
            if img:
                src = img.get("src") or img.get("data-src", "")
                if src and not src.endswith(".gif"):
                    images.append(src)

            props.append(Property(
                url=full_url,
                source=self.name,
                title=title[:120],
                price=price,
                location="Valongo",
                rooms=rooms,
                area_m2=area,
                balcony_area_m2=balcony,
                has_garage=has_garage,
                garage_spaces=garage_spaces,
                has_outdoor=has_outdoor,
                images=images,
            ))

        return props

    @staticmethod
    def _heuristic_price(text: str) -> Optional[float]:
        """Finds price-like patterns: 280.000€, 280 000 €, €280000, etc."""
        patterns = [
            r"(\d{2,3})[.\s](\d{3})\s*€",   # 280.000€ or 280 000€
            r"€\s*(\d{2,3})[.\s](\d{3})",    # €280.000
            r"(\d{5,6})\s*€",                 # 280000€
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                try:
                    groups = m.groups()
                    combined = "".join(g for g in groups if g)
                    return float(combined)
                except ValueError:
                    pass
        return None
