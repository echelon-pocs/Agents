from .idealista import IdealistaScraper
from .imovirtual import ImovirtualScraper
from .olx import OlxScraper
from .sapo_casa import SapoCasaScraper
from .supercasa import SupercasaScraper
from .era import EraScraper
from .remax import RemaxScraper
from .custojusto import CustoJustoScraper

ALL_SCRAPERS = [
    IdealistaScraper,
    ImovirtualScraper,
    OlxScraper,
    SapoCasaScraper,
    SupercasaScraper,
    EraScraper,
    RemaxScraper,
    CustoJustoScraper,
]
