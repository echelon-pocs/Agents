"""
ClaudeSearch — uses DuckDuckGo Lite for actual web search, Claude Haiku for extraction.

Previous approach (web_search_20250305 tool) silently failed: Claude answered from
memory in ~3s instead of searching. DuckDuckGo Lite is free, no API key, always works.

Covers: Idealista, Supercasa, Imovirtual, ERA, RE/MAX, and a general Ermesinde query.
"""
import json
import logging
import os
import re
import time
import random
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper, _USER_AGENTS

logger = logging.getLogger(__name__)

# (source_label, DuckDuckGo query)
_SEARCHES = [
    ("Idealista",   "apartamentos casas venda Ermesinde Valongo site:idealista.pt"),
    ("Imovirtual",  "apartamentos venda Ermesinde Valongo porto site:imovirtual.com"),
    ("Supercasa",   "imoveis venda Ermesinde Valongo site:supercasa.pt"),
    ("ERA",         "imoveis venda Ermesinde Valongo site:era.pt"),
    ("Remax",       "imoveis venda Ermesinde Valongo site:remax.pt"),
    ("General",     "apartamentos T3 T4 venda Ermesinde Valongo porto preco"),
]


class ClaudeSearchScraper(BaseScraper):
    name = "ClaudeSearch"
    base_url = ""

    def search(self) -> List[Property]:
        try:
            import anthropic
        except ImportError:
            logger.warning("[ClaudeSearch] anthropic not installed — pip install anthropic")
            return []

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("[ClaudeSearch] ANTHROPIC_API_KEY not set — skipping")
            return []

        client = anthropic.Anthropic(api_key=api_key)
        props: List[Property] = []
        seen: set = set()

        for source_label, query in _SEARCHES:
            time.sleep(random.uniform(1.5, 3.0))
            results_text = self._ddg_search(query)
            if not results_text:
                logger.warning(f"[ClaudeSearch/{source_label}] DDG returned nothing for: {query[:60]}")
                continue

            found = self._extract_with_claude(client, source_label, results_text)
            for p in found:
                if p.property_id not in seen:
                    seen.add(p.property_id)
                    props.append(p)

        logger.info(f"[ClaudeSearch] {len(props)} listings across {len(_SEARCHES)} searches")
        return props

    # ── DuckDuckGo Lite ───────────────────────────────────────────────────────

    def _ddg_search(self, query: str) -> str:
        """Fetch DuckDuckGo Lite results and return formatted snippets."""
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
        }
        try:
            resp = self.session.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query, "kl": "pt-pt"},
                headers=headers,
                timeout=15,
            )
            if not resp.ok:
                logger.debug(f"[ClaudeSearch] DDG HTTP {resp.status_code}")
                return ""
            return self._parse_ddg_html(resp.text)
        except Exception as e:
            logger.debug(f"[ClaudeSearch] DDG error: {e}")
            return ""

    @staticmethod
    def _parse_ddg_html(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        snippets = []

        # DDG Lite: result links have class "result-link", snippets in next row
        for link in soup.select("a.result-link")[:12]:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            snippet = ""
            tr = link.find_parent("tr")
            if tr:
                sib = tr.find_next_sibling("tr")
                if sib:
                    snippet = sib.get_text(" ", strip=True)[:280]
            if href and title:
                snippets.append(f"URL: {href}\nTitle: {title}\nSnippet: {snippet}")

        if not snippets:
            # Fallback: any external link with meaningful text
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                if href.startswith("http") and "duckduckgo.com" not in href:
                    title = a.get_text(strip=True)
                    if len(title) > 20:
                        snippets.append(f"URL: {href}\nTitle: {title}")
                        if len(snippets) >= 10:
                            break

        return "\n---\n".join(snippets)

    # ── Claude extraction ─────────────────────────────────────────────────────

    def _extract_with_claude(self, client, source: str, results_text: str) -> List[Property]:
        prompt = (
            "Extract property listings for sale in Ermesinde or Valongo, Portugal "
            "from these web search result snippets.\n\n"
            f"Search results:\n{results_text[:3500]}\n\n"
            "Return ONLY a JSON array. Each item must have a valid URL:\n"
            '[{"url":"https://...","title":"T3 Apt Ermesinde","price":250000,'
            '"rooms":3,"area_m2":95,"location":"Ermesinde","description":"..."}]\n'
            "Only include price/rooms/area when clearly stated in the snippet. "
            "Skip non-property URLs (news, agents home pages, etc.). "
            "Return [] if no valid listings found."
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in resp.content).strip()
            logger.warning(f"[ClaudeSearch/{source}] raw Claude response: {text[:200]!r}")

            listings = self._parse_json(text)
            result = []
            for item in listings:
                p = self._to_property(item, source)
                if p:
                    result.append(p)

            logger.info(f"[ClaudeSearch/{source}] {len(result)} listings")
            return result
        except Exception as e:
            logger.warning(f"[ClaudeSearch/{source}] Claude extraction failed: {e}")
            return []

    # ── helpers ───────────────────────────────────────────────────────────────

    def _parse_json(self, text: str) -> list:
        text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            return []
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return []

    def _to_property(self, item: dict, source: str) -> Optional[Property]:
        url = item.get("url", "")
        if not url or not url.startswith("http"):
            return None
        if any(skip in url for skip in ["duckduckgo.com", "google.com", "bing.com", "wikipedia"]):
            return None

        price = None
        try:
            price = float(item["price"]) if item.get("price") else None
        except (ValueError, TypeError):
            pass

        rooms = None
        try:
            rooms = int(item["rooms"]) if item.get("rooms") else None
        except (ValueError, TypeError):
            pass

        area = None
        try:
            area = float(item["area_m2"]) if item.get("area_m2") else None
        except (ValueError, TypeError):
            pass

        title = str(item.get("title", ""))[:120]
        location = str(item.get("location", "Valongo"))[:100]
        description = str(item.get("description", ""))[:500]
        combined = f"{title} {description}"

        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        return Property(
            url=url, source=source, title=title, price=price,
            location=location, rooms=rooms, area_m2=area,
            balcony_area_m2=balcony, has_garage=has_garage,
            garage_spaces=garage_spaces, has_outdoor=has_outdoor,
            description=description,
        )
