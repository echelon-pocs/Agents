"""
Geocoding (Nominatim/OSM) and Haversine distance utilities.
No API key required — both services are free.
"""
import math
import time
import logging
import requests
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ERMESINDE_LAT = 41.2153
ERMESINDE_LON = -8.5507
MAX_DISTANCE_KM = 20.0

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "ErmesindPropertySearch/1.0 (personal-use)"}

# Simple in-process cache so we don't re-geocode the same address in one run
_geocode_cache: Dict[str, Optional[Tuple[float, float]]] = {}


def geocode(address: str) -> Optional[Tuple[float, float]]:
    """Returns (lat, lon) for address, or None if not found."""
    if address in _geocode_cache:
        return _geocode_cache[address]
    try:
        time.sleep(1.1)  # Nominatim rate limit: max 1 req/s
        resp = requests.get(
            _NOMINATIM_URL,
            params={"q": address + ", Portugal", "format": "json", "limit": 1},
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            coords = float(results[0]["lat"]), float(results[0]["lon"])
            _geocode_cache[address] = coords
            return coords
    except Exception as e:
        logger.debug(f"Geocoding failed for '{address}': {e}")
    _geocode_cache[address] = None
    return None


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    R = 6_371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def distance_from_ermesinde(lat: float, lon: float) -> float:
    return haversine(ERMESINDE_LAT, ERMESINDE_LON, lat, lon)


def check_distance(address: str, lat: float = None, lon: float = None) -> Tuple[bool, Optional[float]]:
    """
    Returns (within_range, distance_km).
    If geocoding fails, returns (True, None) — don't discard on uncertainty.
    Uses provided lat/lon if already known, skipping the geocode call.
    """
    if lat is None or lon is None:
        coords = geocode(address)
        if not coords:
            return True, None
        lat, lon = coords
    dist = distance_from_ermesinde(lat, lon)
    return dist <= MAX_DISTANCE_KM, round(dist, 2)
