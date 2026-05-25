from .olx import OlxScraper
from .era import EraScraper
from .remax import RemaxScraper
from .custojusto import CustoJustoScraper
from .bpi import BpiScraper
from .lugarcerto import LugarCertoScraper
from .claude_search import ClaudeSearchScraper

ALL_SCRAPERS = [
    # Claude Haiku web search — covers Idealista, Supercasa, Imovirtual
    ClaudeSearchScraper,
    # Direct HTML scrapers
    OlxScraper,
    EraScraper,
    RemaxScraper,
    CustoJustoScraper,
    BpiScraper,
    LugarCertoScraper,
]
