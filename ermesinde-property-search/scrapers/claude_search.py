"""
ClaudeSearch — uses Claude's web_search tool (forced) to find listings.

Primary: web_search_20250305 tool with tool_choice=any (forces actual web search)
Fallback: DuckDuckGo Lite → DDG HTML → Bing scraping (if web tool fails)

Covers: Idealista, Supercasa, Imovirtual, ERA, RE/MAX, and a general Ermesinde query.
"""
import json
import logging
import os
import re
import time
import random
from typing import List, Optional
from urllib.parse import unquote, parse_qs, urlparse

from bs4 import BeautifulSoup

from models import Property
from .base import BaseScraper, _USER_AGENTS

logger = logging.getLogger(__name__)

_SEARCHES = [
    ("Idealista",    "apartamentos casas venda Valongo Gondomar Maia site:idealista.pt"),
    ("Imovirtual",   "apartamentos venda Valongo Gondomar Maia porto site:imovirtual.com"),
    ("Supercasa",    "imoveis venda Valongo Gondomar Maia site:supercasa.pt"),
    ("ERA",          "imoveis venda Valongo Gondomar Maia site:era.pt"),
    ("Remax",        "imoveis venda Valongo Gondomar Maia site:remax.pt"),
    ("General-V",    "apartamentos T3 T4 venda Ermesinde Valongo Alfena porto"),
    ("General-GM",   "apartamentos T3 T4 venda Gondomar Maia porto"),
]


class ClaudeSearchScraper(BaseScraper):
    name = "ClaudeSearch"
    base_url = ""

    def search(self) -> List[Property]:
        try:
            import anthropic
        except ImportError:
            logger.warning("[ClaudeSearch] anthropic not installed")
            return []

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("[ClaudeSearch] ANTHROPIC_API_KEY not set")
            return []

        client = anthropic.Anthropic(api_key=api_key)
        props: List[Property] = []
        seen: set = set()

        for source_label, query in _SEARCHES:
            time.sleep(random.uniform(1.5, 3.0))

            # Primary: force Claude's own web search tool
            found = self._search_with_web_tool(client, source_label, query)

            # Fallback: scrape search engines if web tool returned nothing
            if not found:
                results_text = self._search_engines(query)
                if results_text:
                    found = self._extract_with_claude(client, source_label, results_text)
                else:
                    logger.warning(f"[ClaudeSearch/{source_label}] all search methods returned nothing")

            for p in found:
                if p.property_id not in seen:
                    seen.add(p.property_id)
                    props.append(p)

        logger.info(f"[ClaudeSearch] {len(props)} listings across {len(_SEARCHES)} searches")
        return props

    # ── Primary: Claude web_search tool ──────────────────────────────────────

    def _search_with_web_tool(self, client, source_label: str, query: str) -> List[Property]:
        """Force Claude to use web_search_20250305, then parse JSON output."""
        prompt = (
            f"Search the web for: {query}\n\n"
            "Find property listings for sale (apartamentos ou moradias à venda) "
            "in Ermesinde or Valongo, Portugal. "
            "Return ONLY a JSON array of listings found:\n"
            '[{"url":"https://...","title":"T3 Apt Ermesinde","price":250000,'
            '"price_from":null,"price_to":null,'
            '"rooms":3,"area_m2":95,"location":"Ermesinde","description":"..."}]\n'
            "Use price for a single fixed price. Use price_from/price_to for ranges "
            '(e.g. "a partir de 200000€" → price_from=200000, price=null). '
            "IMPORTANT: only include URLs you can see directly in the search results — "
            "do NOT construct, infer or guess URLs. Return [] if none found."
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=3000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                tool_choice={"type": "any"},
                messages=[{"role": "user", "content": prompt}],
            )

            # If the API returned tool_use stop_reason, do a second turn
            if resp.stop_reason == "tool_use":
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": resp.content},
                ]
                tool_results = [
                    {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                    for b in resp.content
                    if hasattr(b, "type") and b.type == "tool_use"
                ]
                if tool_results:
                    messages.append({"role": "user", "content": tool_results})
                    resp = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=3000,
                        tools=[{"type": "web_search_20250305", "name": "web_search"}],
                        messages=messages,
                    )

            text = "".join(getattr(b, "text", "") for b in resp.content).strip()
            logger.warning(f"[ClaudeSearch/{source_label}] web tool response: {text[:250]!r}")

            listings = self._parse_json(text)
            result = [p for p in (self._to_property(i, source_label) for i in listings) if p]
            logger.info(f"[ClaudeSearch/{source_label}] {len(result)} listings (web tool)")
            return result
        except Exception as e:
            logger.debug(f"[ClaudeSearch/{source_label}] web tool failed: {e}")
            return []

    # ── Fallback: search engine scraping ─────────────────────────────────────

    def _search_engines(self, query: str) -> str:
        """Try DDG Lite → DDG HTML → Bing, return first non-empty result."""
        result = self._ddg_lite(query)
        if result:
            return result
        time.sleep(random.uniform(1.0, 2.0))
        result = self._ddg_html(query)
        if result:
            return result
        time.sleep(random.uniform(1.0, 2.0))
        return self._bing_search(query)

    def _ddg_lite(self, query: str) -> str:
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
                return ""
            return self._parse_ddg_lite(resp.text)
        except Exception as e:
            logger.debug(f"[ClaudeSearch] DDG Lite error: {e}")
            return ""

    def _ddg_html(self, query: str) -> str:
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://duckduckgo.com/",
            "Origin": "https://duckduckgo.com",
        }
        try:
            resp = self.session.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "pt-pt"},
                headers=headers,
                timeout=15,
            )
            if not resp.ok:
                return ""
            return self._parse_ddg_html_results(resp.text)
        except Exception as e:
            logger.debug(f"[ClaudeSearch] DDG HTML error: {e}")
            return ""

    def _bing_search(self, query: str) -> str:
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
        }
        try:
            resp = self.session.get(
                "https://www.bing.com/search",
                params={"q": query, "setlang": "pt", "cc": "PT", "count": "15"},
                headers=headers,
                timeout=15,
            )
            if not resp.ok:
                return ""
            return self._parse_bing_html(resp.text)
        except Exception as e:
            logger.debug(f"[ClaudeSearch] Bing error: {e}")
            return ""

    @staticmethod
    def _parse_ddg_lite(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        snippets = []
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
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                if href.startswith("http") and "duckduckgo.com" not in href:
                    title = a.get_text(strip=True)
                    if len(title) > 20:
                        snippets.append(f"URL: {href}\nTitle: {title}")
                        if len(snippets) >= 10:
                            break
        return "\n---\n".join(snippets)

    @staticmethod
    def _parse_ddg_html_results(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        snippets = []
        for result in soup.select("div.result, .result")[:12]:
            link = result.select_one("a.result__a, h2 a, .result__title a")
            if not link:
                continue
            href = link.get("href", "")
            if "uddg=" in href:
                try:
                    params = parse_qs(urlparse(href).query)
                    href = unquote(params.get("uddg", [""])[0])
                except Exception:
                    pass
            title = link.get_text(strip=True)
            snippet_el = result.select_one(".result__snippet, a.result__snippet")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if href and href.startswith("http") and "duckduckgo.com" not in href:
                snippets.append(f"URL: {href}\nTitle: {title}\nSnippet: {snippet}")
        return "\n---\n".join(snippets)

    @staticmethod
    def _parse_bing_html(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        snippets = []
        for li in soup.select("li.b_algo")[:12]:
            link = li.select_one("h2 a")
            if not link:
                continue
            href = link.get("href", "")
            title = link.get_text(strip=True)
            snippet_el = li.select_one(".b_caption p, p")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            if href and href.startswith("http"):
                snippets.append(f"URL: {href}\nTitle: {title}\nSnippet: {snippet}")
        return "\n---\n".join(snippets)

    # ── Claude extraction from search engine text ─────────────────────────────

    def _extract_with_claude(self, client, source: str, results_text: str) -> List[Property]:
        prompt = (
            "Extract property listings for sale in Ermesinde or Valongo, Portugal "
            "from these web search result snippets.\n\n"
            f"Search results:\n{results_text[:3500]}\n\n"
            "Return ONLY a JSON array:\n"
            '[{"url":"https://...","title":"T3 Apt Ermesinde","price":250000,'
            '"price_from":null,"price_to":null,'
            '"rooms":3,"area_m2":95,"location":"Ermesinde","description":"..."}]\n'
            "Use price for a single fixed price. Use price_from/price_to for ranges. "
            "Only include price/rooms/area when clearly stated. "
            "IMPORTANT: only include URLs visible in the snippets above — do NOT construct or infer URLs. "
            "Skip non-property URLs. Return [] if none found."
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in resp.content).strip()
            listings = self._parse_json(text)
            result = [p for p in (self._to_property(i, source) for i in listings) if p]
            logger.info(f"[ClaudeSearch/{source}] {len(result)} listings (engine fallback)")
            return result
        except Exception as e:
            logger.warning(f"[ClaudeSearch/{source}] extraction failed: {e}")
            return []

    # ── URL helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Fix known bad URL patterns that Claude's web search returns."""
        # remax.pt: remove /pt/ locale prefix and trailing agent token
        # Bad:  remax.pt/pt/imoveis/venda-moradia-t3-porto-valongo/123911202-28/FIjlEkCD-sL0N2l5razp6kXspSOR9E03
        # Good: remax.pt/imoveis/venda-moradia-t3-porto-valongo/123911202-28
        if "remax.pt" in url:
            url = re.sub(r"(remax\.pt)/pt/", r"\1/", url)
            url = re.sub(r"(remax\.pt/imoveis/[^/]+/\d+-\d+)/[A-Za-z0-9_-]{8,}$", r"\1", url)
        # idealista.pt: strip trailing tracking params after listing ID
        # Bad:  idealista.pt/imovel/12345678/?utm_source=...
        # Good: idealista.pt/imovel/12345678/
        if "idealista.pt" in url:
            url = re.sub(r"(\d{8,}/).*", r"\1", url)
        # imovirtual.com: strip query strings from detail pages
        if "imovirtual.com" in url and "/anuncio/" in url:
            url = url.split("?")[0]
        return url

    def _validate_url(self, url: str) -> bool:
        """
        Returns False if the URL is a confirmed dead link.
        - HTTP 404 → dead
        - Redirected to site root/homepage → listing no longer exists
        Conservative: returns True on network errors or blocks (403/429).
        """
        try:
            resp = self.session.get(url, timeout=8, allow_redirects=True, stream=True)
            # Read a small chunk so we at least get the final redirected URL
            next(resp.iter_content(512), None)
            resp.close()
            if resp.status_code == 404:
                logger.debug(f"[ClaudeSearch] dead URL (404): {url}")
                return False
            # Detect redirect to homepage — listing no longer exists
            final_path = urlparse(resp.url).path.rstrip("/")
            if final_path in ("", "/pt", "/en") and urlparse(url).path.rstrip("/") != final_path:
                logger.debug(f"[ClaudeSearch] dead URL (→ homepage): {url}")
                return False
            return True
        except Exception:
            return True  # network error or block — keep conservatively

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

        url = self._normalize_url(url)
        if not self._validate_url(url):
            logger.info(f"[ClaudeSearch/{source}] discarding dead URL: {url}")
            return None

        price = None
        try:
            price = float(item["price"]) if item.get("price") else None
        except (ValueError, TypeError):
            pass

        price_from = None
        try:
            price_from = float(item["price_from"]) if item.get("price_from") else None
        except (ValueError, TypeError):
            pass

        price_to = None
        try:
            price_to = float(item["price_to"]) if item.get("price_to") else None
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

        raw: dict = {}
        if price_from:
            raw["price_from"] = price_from
        if price_to:
            raw["price_to"] = price_to

        return Property(
            url=url, source=source, title=title, price=price,
            location=location, rooms=rooms, area_m2=area,
            balcony_area_m2=balcony, has_garage=has_garage,
            garage_spaces=garage_spaces, has_outdoor=has_outdoor,
            description=description, raw_data=raw,
        )
