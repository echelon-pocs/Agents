"""
Scraper that uses Claude Haiku + Anthropic web search to find listings
on sites that block direct HTTP requests (403/Cloudflare bot protection).

Targets: Idealista, Supercasa, Imovirtual.
Requires: ANTHROPIC_API_KEY in environment / .env
"""
import json
import logging
import os
from typing import List, Optional

from models import Property
from .base import BaseScraper

logger = logging.getLogger(__name__)

_SITES = [
    "idealista.pt",
    "supercasa.pt",
    "imovirtual.com",
]


class ClaudeSearchScraper(BaseScraper):
    """
    One Claude Haiku call per site with web_search tool enabled.
    Claude searches, extracts, and returns structured JSON — no HTML parsing needed.
    """
    name = "ClaudeSearch"
    base_url = ""

    def search(self) -> List[Property]:
        try:
            import anthropic
        except ImportError:
            logger.warning("[ClaudeSearch] anthropic package not installed — run: pip install anthropic")
            return []

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("[ClaudeSearch] ANTHROPIC_API_KEY not set in .env — skipping")
            return []

        client = anthropic.Anthropic(api_key=api_key)
        props: List[Property] = []
        seen: set = set()

        for site in _SITES:
            found = self._search_site(client, site)
            for p in found:
                if p.property_id not in seen:
                    seen.add(p.property_id)
                    props.append(p)

        logger.info(f"[ClaudeSearch] {len(props)} listings across {len(_SITES)} sites")
        return props

    def _search_site(self, client, site: str) -> List[Property]:
        prompt = (
            f"Search {site} for properties (apartments and houses) for sale in or near "
            f"Ermesinde, Valongo, Porto, Portugal. Include T2, T3, T4 and larger. "
            f"Price up to 400,000 euros.\n\n"
            f"Return a JSON array with ALL listings you find. Each item:\n"
            f'[{{"url":"https://...","title":"...","price":250000,"rooms":3,'
            f'"area_m2":95,"location":"Ermesinde","description":"..."}}]\n\n'
            f"Return only raw JSON, no markdown fences, no explanation. "
            f"If nothing found, return []."
        )

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )

            # Collect text from all text blocks (web_search tool may add ToolUseBlocks)
            text = "".join(
                getattr(b, "text", "") for b in resp.content
            ).strip()

            if not text:
                logger.debug(f"[ClaudeSearch/{site}] empty response (stop_reason={resp.stop_reason})")
                return []

            listings = self._parse_json(text)
            if not listings:
                logger.debug(f"[ClaudeSearch/{site}] JSON parse failed; raw text: {text[:300]}")

            props = []
            site_label = site.split(".")[0].capitalize()
            for item in listings:
                p = self._to_property(item, site_label)
                if p:
                    props.append(p)

            logger.info(f"[ClaudeSearch/{site}] {len(props)} listings")
            return props

        except Exception as e:
            logger.warning(f"[ClaudeSearch/{site}] Failed: {e}")
            return []

    def _parse_json(self, text: str) -> list:
        import re
        # Strip markdown code fences
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

        price = None
        try:
            price = float(item["price"]) if item.get("price") else None
        except (ValueError, TypeError):
            pass
        if price and price > 380_000:
            return None

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
        location = str(item.get("location", "Valongo"))
        description = str(item.get("description", ""))[:500]

        combined = f"{title} {description}"
        has_garage, garage_spaces = self.detect_garage(combined)
        has_outdoor = self.detect_outdoor(combined)
        balcony = self.detect_balcony_area(combined)

        return Property(
            url=url,
            source=source,
            title=title,
            price=price,
            location=location,
            rooms=rooms,
            area_m2=area,
            balcony_area_m2=balcony,
            has_garage=has_garage,
            garage_spaces=garage_spaces,
            has_outdoor=has_outdoor,
            description=description,
        )
