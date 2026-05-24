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

    def get_soup(self, url: str, **kwargs) -> Optional[BeautifulSoup]:
        self._sleep()
        try:
            resp = self.session.get(url, timeout=30, **kwargs)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            logger.warning(f"[{self.name}] GET {url} failed: {e}")
            return None

    def get_json(self, url: str, **kwargs) -> Optional[dict]:
        self._sleep()
        try:
            resp = self.session.get(url, timeout=30, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
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
