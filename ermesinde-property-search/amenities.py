"""
Checks nearby amenities via OpenStreetMap Overpass API + Nominatim geocoding.
Both services are free and require no API key.
"""
import logging
import time
import requests
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

HEADERS = {"User-Agent": "ErmesindPropertySearch/1.0 (personal-use)"}

AMENITY_QUERIES = {
    "supermarket": '["shop"~"supermarket|convenience|greengrocer"]',
    "school": '["amenity"~"school|kindergarten"]',
    "park": '["leisure"~"park|playground"]',
    "pharmacy": '["amenity"="pharmacy"]',
    "bus_stop": '["highway"="bus_stop"]',
}

RADIUS_M = 800


def geocode(address: str) -> Optional[Tuple[float, float]]:
    try:
        time.sleep(1)  # Nominatim rate limit: 1 req/s
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": address + ", Portugal", "format": "json", "limit": 1},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        logger.debug(f"Geocoding failed for '{address}': {e}")
    return None


def check_amenities(lat: float, lon: float) -> Tuple[int, str]:
    """Returns (score 0-5, human-readable detail string)."""
    found = []
    try:
        parts = []
        for tag_filter in AMENITY_QUERIES.values():
            for node_type in ("node", "way", "relation"):
                parts.append(
                    f'{node_type}{tag_filter}(around:{RADIUS_M},{lat},{lon});'
                )
        query = f"[out:json][timeout:15];({''.join(parts)});out count;"
        time.sleep(1)
        resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=20, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        total = data.get("elements", [{}])[0].get("tags", {}).get("total", 0)
        # Individually check each category
    except Exception as e:
        logger.debug(f"Overpass bulk query failed: {e}")

    # Fall back to per-category queries
    scores = {}
    for name, tag_filter in AMENITY_QUERIES.items():
        try:
            parts = []
            for node_type in ("node", "way", "relation"):
                parts.append(f'{node_type}{tag_filter}(around:{RADIUS_M},{lat},{lon});')
            query = f"[out:json][timeout:10];({''.join(parts)});out count;"
            time.sleep(1)
            resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=15, headers=HEADERS)
            resp.raise_for_status()
            data = resp.json()
            count = int(data.get("elements", [{}])[0].get("tags", {}).get("total", 0))
            scores[name] = count
        except Exception as e:
            logger.debug(f"Overpass query for {name} failed: {e}")
            scores[name] = -1

    detail_parts = []
    score = 0
    labels = {
        "supermarket": "Supermercado",
        "school": "Escola",
        "park": "Parque/Recreio",
        "pharmacy": "Farmácia",
        "bus_stop": "Paragem de autocarro",
    }
    for key, label in labels.items():
        count = scores.get(key, -1)
        if count > 0:
            score += 1
            detail_parts.append(f"{label}: {count}")
        elif count == 0:
            detail_parts.append(f"{label}: nenhum num raio de {RADIUS_M}m")
        else:
            detail_parts.append(f"{label}: não verificado")

    return score, " | ".join(detail_parts)


def enrich_property_amenities(location: str, lat: float = None, lon: float = None):
    """Returns (amenities_score, amenities_detail). Geocodes location if coords missing."""
    if lat is None or lon is None:
        coords = geocode(location)
        if not coords:
            return 0, "Localização não encontrada"
        lat, lon = coords
    return check_amenities(lat, lon)
