"""
Nearby amenity scoring via OpenStreetMap Overpass API.
Geocoding is delegated to geo.py so coordinates are shared across modules.
"""
import logging
import time
import requests
from typing import Tuple

from geo import geocode

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_HEADERS = {"User-Agent": "ErmesindPropertySearch/1.0 (personal-use)"}
RADIUS_M = 800

_AMENITY_QUERIES = {
    "supermarket": '["shop"~"supermarket|convenience|greengrocer"]',
    "school": '["amenity"~"school|kindergarten"]',
    "park": '["leisure"~"park|playground"]',
    "pharmacy": '["amenity"="pharmacy"]',
    "bus_stop": '["highway"="bus_stop"]',
}

_LABELS = {
    "supermarket": "Supermercado",
    "school": "Escola",
    "park": "Parque/Recreio",
    "pharmacy": "Farmácia",
    "bus_stop": "Paragem",
}


def check_amenities(lat: float, lon: float) -> Tuple[int, str]:
    """Returns (score 0-5, human-readable summary string)."""
    scores: dict[str, int] = {}

    for name, tag_filter in _AMENITY_QUERIES.items():
        try:
            parts = [
                f"{node_type}{tag_filter}(around:{RADIUS_M},{lat},{lon});"
                for node_type in ("node", "way", "relation")
            ]
            query = f"[out:json][timeout:10];({''.join(parts)});out count;"
            time.sleep(1)
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=15, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()
            scores[name] = int(data.get("elements", [{}])[0].get("tags", {}).get("total", 0))
        except Exception as e:
            logger.debug(f"Overpass query for {name} at ({lat},{lon}) failed: {e}")
            scores[name] = -1

    score = 0
    parts = []
    for key, label in _LABELS.items():
        count = scores.get(key, -1)
        if count > 0:
            score += 1
            parts.append(f"{label}: {count}")
        elif count == 0:
            parts.append(f"{label}: 0")
        else:
            parts.append(f"{label}: ?")

    return score, " | ".join(parts)


def enrich_property_amenities(location: str, lat: float = None, lon: float = None) -> Tuple[int, str]:
    """Geocodes if lat/lon not provided, then queries Overpass."""
    if lat is None or lon is None:
        coords = geocode(location)
        if not coords:
            return 0, "Localização não encontrada"
        lat, lon = coords
    return check_amenities(lat, lon)
