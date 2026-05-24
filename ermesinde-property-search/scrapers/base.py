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

# Rotate through realistic browser UA strings to reduce fingerprinting
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MAX_PAGES = 5
MIN_DELAY = 3.0
MAX_DELAY = 7.0
MAX_DETAIL_FETCHES = 20  # cap detail-page requests per run


class BaseScraper(ABC):
    name = "Base"
    base_url = ""
    MAX_PAGES = MAX_PAGES

    def __init__(self):
        self.session = requests.Session()
        self._rotate_ua()

    def _rotate_ua(self):
        headers = dict(_BASE_HEADERS)
        headers["User-Agent"] = random.choice(_USER_AGENTS)
        self.session.headers.update(headers)

    def _sleep(self):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # ── HTTP helpers with retry / backoff ─────────────────────────────────────

    def get_soup(self, url: str, retries: int = 3, **kwargs) -> Optional[BeautifulSoup]:
        self._sleep()
        self._rotate_ua()  # fresh UA on every request
        for attempt in range(retries):
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
                if resp.status_code == 429:
                    wait = (2 ** attempt) * random.uniform(10, 20)
                    logger.warning(f"[{self.name}] 429 — waiting {wait:.0f}s (attempt {attempt+1})")
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
        self._rotate_ua()
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

    # ── parsing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def parse_price(text: str) -> Optional[float]:
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        return float(digits) if digits else None

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
        return any(k in text.lower() for k in keywords)

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

    @staticmethod
    def _heuristic_price(text: str) -> Optional[float]:
        patterns = [
            r"(\d{2,3})[.\s](\d{3})\s*€",
            r"€\s*(\d{2,3})[.\s](\d{3})",
            r"(\d{5,6})\s*€",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                try:
                    return float("".join(g for g in m.groups() if g))
                except ValueError:
                    pass
        return None

    # ── Tier 1.5: detail-page enrichment ─────────────────────────────────────

    def fetch_details(self, prop: Property) -> Property:
        """
        Visit the property's own detail page and fill in fields that listing
        cards don't expose: exact balcony/kitchen/living room sizes, more
        images, energy rating, fuller description.
        """
        soup = self.get_soup(prop.url)
        if not soup:
            return prop

        self._enrich_from_jsonld_detail(soup, prop)
        self._enrich_from_html_detail(soup, prop)
        return prop

    def _enrich_from_jsonld_detail(self, soup: BeautifulSoup, prop: Property) -> None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data if isinstance(data, list) else data.get("@graph", [data])
            for item in items:
                schema_type = item.get("@type", "")
                if isinstance(schema_type, list):
                    schema_type = " ".join(schema_type)
                if not any(t in schema_type for t in ("Residence", "House", "Apartment", "RealEstateListing", "Product")):
                    continue

                if not prop.description:
                    prop.description = str(item.get("description", ""))[:500]

                for key in ("floorSize", "totalFloorArea", "netArea"):
                    val = item.get(key)
                    if val and not prop.area_m2:
                        if isinstance(val, dict):
                            prop.area_m2 = float(val.get("value", 0) or 0) or None
                        else:
                            prop.area_m2 = self.parse_area(str(val))
                        break

                if not prop.rooms:
                    beds = item.get("numberOfBedrooms") or item.get("numberOfRooms")
                    if beds:
                        try:
                            prop.rooms = int(beds)
                        except (ValueError, TypeError):
                            pass

                if not prop.images:
                    img = item.get("image", [])
                    if isinstance(img, list):
                        prop.images = [i.get("url", i) if isinstance(i, dict) else i for i in img[:5]]
                    elif isinstance(img, dict):
                        prop.images = [img.get("url", "")]
                    elif isinstance(img, str):
                        prop.images = [img]

    def _enrich_from_html_detail(self, soup: BeautifulSoup, prop: Property) -> None:
        full_text = soup.get_text(" ", strip=True)

        if not prop.balcony_area_m2:
            prop.balcony_area_m2 = self.detect_balcony_area(full_text)
        if not prop.has_garage:
            prop.has_garage, prop.garage_spaces = self.detect_garage(full_text)
        if not prop.has_outdoor:
            prop.has_outdoor = self.detect_outdoor(full_text)
        if not prop.description:
            for sel in ("[class*='description']", "[itemprop='description']", "p.description"):
                el = soup.select_one(sel)
                if el:
                    prop.description = el.get_text(strip=True)[:500]
                    break

        # Extract kitchen + living room areas separately for scoring
        kitchen_area = self._extract_room_area(full_text, ["cozinha", "kitchen"])
        living_area = self._extract_room_area(full_text, ["sala de estar", "sala", "living"])
        if kitchen_area:
            prop.raw_data["kitchen_area_m2"] = kitchen_area
        if living_area:
            prop.raw_data["living_area_m2"] = living_area
        if kitchen_area and living_area:
            prop.raw_data["kitchen_living_combined_m2"] = kitchen_area + living_area

        # Collect more images
        if len(prop.images) < 3:
            for img in soup.select("img[src]")[:5]:
                src = img.get("src", "")
                if src and not src.endswith(".gif") and src not in prop.images:
                    prop.images.append(src)

    @staticmethod
    def _extract_room_area(text: str, keywords: list) -> Optional[float]:
        for kw in keywords:
            patterns = [
                rf"{kw}[^.{{}}]*?(\d+(?:[.,]\d+)?)\s*m",
                rf"(\d+(?:[.,]\d+)?)\s*m[²2]?\s*(?:de\s*)?{kw}",
            ]
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    try:
                        return float(m.group(1).replace(",", "."))
                    except ValueError:
                        pass
        return None

    # ── Tier 2: JSON-LD search results ───────────────────────────────────────

    def search_jsonld(self) -> List[Property]:
        results: List[Property] = []
        seen: set = set()
        for url in getattr(self, "SEARCH_URLS", []):
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
                if isinstance(schema_type, list):
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

        addr = item.get("address", {})
        location = addr.get("addressLocality", "Valongo") if isinstance(addr, dict) else "Valongo"

        return Property(
            url=url, source=self.name, title=str(name), price=price,
            location=location, rooms=rooms, area_m2=float(area) if area else None,
            balcony_area_m2=balcony, has_garage=has_garage, garage_spaces=garage_spaces,
            has_outdoor=has_outdoor, description=str(desc)[:500],
            images=[i for i in images if i],
        )

    # ── Tier 3: heuristic link/price scan ────────────────────────────────────

    _PROPERTY_URL_FRAGMENTS = [
        "/imovel/", "/imoveis/", "/property/", "/apartamento", "/moradia",
        "/casa-", "/habitacao/", "/residencial/",
    ]

    def search_heuristic(self) -> List[Property]:
        results: List[Property] = []
        seen: set = set()
        for url in getattr(self, "SEARCH_URLS", [])[:2]:
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
        logger.info(f"[{self.name}] Heuristic mode: {len(results)} candidates")
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

            container = None
            for tag in ("article", "li", "div", "section"):
                container = a.find_parent(tag)
                if container and len(container.get_text()) > 50:
                    break
            if not container:
                continue

            text = container.get_text(" ", strip=True)
            price = self._heuristic_price(text)
            rooms = self.parse_rooms(text)
            if price is None and rooms is None:
                continue
            if price and price > 380_000:
                continue

            area = self.parse_area(text)
            has_garage, garage_spaces = self.detect_garage(text)
            has_outdoor = self.detect_outdoor(text)
            balcony = self.detect_balcony_area(text)
            title = a.get("title") or a.get_text(strip=True) or ""

            img = container.select_one("img[src]") or container.select_one("img[data-src]")
            images = []
            if img:
                src = img.get("src") or img.get("data-src", "")
                if src and not src.endswith(".gif"):
                    images.append(src)

            props.append(Property(
                url=full_url, source=self.name, title=title[:120], price=price,
                location="Valongo", rooms=rooms, area_m2=area, balcony_area_m2=balcony,
                has_garage=has_garage, garage_spaces=garage_spaces,
                has_outdoor=has_outdoor, images=images,
            ))
        return props

    # ── Tier 4: Playwright (optional) ────────────────────────────────────────

    def search_playwright(self) -> List[Property]:
        """
        Uses a headless Chromium browser to fully render the page, then
        falls through to JSON-LD + heuristic extraction on the rendered HTML.
        Requires: pip install playwright && playwright install chromium
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning(f"[{self.name}] Playwright not installed — skipping Tier 4")
            return []

        results: List[Property] = []
        seen: set = set()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                locale="pt-PT",
                extra_http_headers={"Accept-Language": "pt-PT,pt;q=0.9"},
            )
            page = ctx.new_page()

            for url in getattr(self, "SEARCH_URLS", [])[:2]:
                try:
                    page.goto(url, wait_until="networkidle", timeout=45_000)
                    time.sleep(random.uniform(2, 4))
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")

                    found = self._extract_jsonld(soup, url) or self._heuristic_extract(soup)
                    for p in found:
                        if p.property_id not in seen:
                            seen.add(p.property_id)
                            results.append(p)
                except Exception as e:
                    logger.warning(f"[{self.name}] Playwright failed on {url}: {e}")

            browser.close()

        logger.info(f"[{self.name}] Playwright tier: {len(results)} candidates")
        return results

    @abstractmethod
    def search(self) -> List[Property]:
        pass
