from .olx import OlxScraper
from .sapo_casa import SapoCasaScraper
from .era import EraScraper
from .remax import RemaxScraper
from .custojusto import CustoJustoScraper
from .century21 import Century21Scraper
from .bpi import BpiScraper
from .predimed import PredimedScraper
from .lugarcerto import LugarCertoScraper
from .claude_search import ClaudeSearchScraper

ALL_SCRAPERS = [
    # Claude Haiku web search — covers Idealista, Supercasa, Imovirtual
    # (those three block direct HTTP; Claude bypasses bot protection)
    ClaudeSearchScraper,
    # Direct HTML scrapers
    OlxScraper,
    SapoCasaScraper,
    EraScraper,
    RemaxScraper,
    CustoJustoScraper,
    Century21Scraper,
    BpiScraper,
    PredimedScraper,
    LugarCertoScraper,
]
