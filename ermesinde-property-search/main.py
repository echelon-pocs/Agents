#!/usr/bin/env python3
"""
Ermesinde property search — daily runner.

Usage:
    python main.py              # full run: scrape, filter, email
    python main.py --dry-run    # scrape + print, no email or DB writes
    python main.py --test-email # send a test email with sample data
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

from dotenv import load_dotenv

from scrapers import ALL_SCRAPERS
from storage import PropertyStorage
from email_sender import send_email
from amenities import enrich_property_amenities
from geo import check_distance, geocode
from scoring import score_property
from models import Property

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/search.log"),
    ],
)
logger = logging.getLogger("main")

# ── constants ─────────────────────────────────────────────────────────────────
MIN_ROOMS = 3
MAX_PRICE = 380_000
MAX_DETAIL_FETCHES = 20   # max detail-page HTTP calls per run
ZERO_THRESHOLD_JSONLD = 1
ZERO_THRESHOLD_HEURISTIC = 2
ZERO_THRESHOLD_PLAYWRIGHT = 3


# ── filtering ─────────────────────────────────────────────────────────────────

def passes_hard_filter(prop: Property) -> bool:
    if prop.rooms is not None and prop.rooms < MIN_ROOMS:
        return False
    if prop.price is not None and prop.price > MAX_PRICE:
        return False
    return True


# ── geocoding + distance ──────────────────────────────────────────────────────

def apply_distance_filter(properties: List[Property]) -> List[Property]:
    """
    Geocode each property, store lat/lon and distance_km on the object.
    Discard properties > 10 km from Ermesinde centre.
    If geocoding fails, keep the property (don't discard on uncertainty).
    """
    kept = []
    for prop in properties:
        within, dist = check_distance(prop.location, lat=prop.lat, lon=prop.lon)
        if dist is not None:
            prop.distance_km = dist
            coords = geocode(prop.location)
            if coords:
                prop.lat, prop.lon = coords
        if within:
            kept.append(prop)
        else:
            logger.debug(f"Skipping '{prop.title}' — {dist:.1f} km from Ermesinde")
    return kept


# ── detail-page enrichment ────────────────────────────────────────────────────

def fetch_details_for(properties: List[Property]) -> None:
    """Visit individual listing pages (in-place) up to MAX_DETAIL_FETCHES."""
    scraper_map = {cls.name: cls() for cls in ALL_SCRAPERS}
    count = 0
    for prop in properties:
        if count >= MAX_DETAIL_FETCHES:
            break
        scraper = scraper_map.get(prop.source)
        if scraper is None:
            continue
        try:
            scraper.fetch_details(prop)
            count += 1
        except Exception as e:
            logger.warning(f"Detail fetch failed for {prop.url}: {e}")


# ── amenity enrichment ────────────────────────────────────────────────────────

def enrich_with_amenities(properties: List[Property]) -> None:
    for prop in properties:
        if not prop.location:
            continue
        try:
            score, detail = enrich_property_amenities(
                prop.location, lat=prop.lat, lon=prop.lon
            )
            prop.amenities_score = score
            prop.amenities_detail = detail
        except Exception as e:
            logger.warning(f"Amenities enrichment failed for '{prop.location}': {e}")


# ── adaptive scraper runner ───────────────────────────────────────────────────

def run_scraper_adaptive(scraper, storage: PropertyStorage) -> Tuple[List[Property], str]:
    """
    3 (+1 optional) tier fallback:
      Tier 1: CSS selectors + Next.js JSON
      Tier 2: JSON-LD structured data        (after 2 zero runs)
      Tier 3: heuristic link/price scan      (after 3 zero runs)
      Tier 4: Playwright headless browser    (after 4 zero runs, if installed)
    """
    health = storage.get_health(scraper.name)
    zeros = health["consecutive_zeros"]
    found: List[Property] = []
    mode = "normal"

    try:
        found = scraper.search()
    except Exception as e:
        logger.error(f"[{scraper.name}] Tier 1 failed: {e}", exc_info=True)

    if not found and zeros >= ZERO_THRESHOLD_JSONLD:
        logger.warning(f"[{scraper.name}] {zeros} zero runs — escalating to JSON-LD tier")
        mode = "jsonld"
        try:
            found = scraper.search_jsonld()
        except Exception as e:
            logger.error(f"[{scraper.name}] JSON-LD tier failed: {e}", exc_info=True)

    if not found and zeros >= ZERO_THRESHOLD_HEURISTIC:
        logger.warning(f"[{scraper.name}] Escalating to heuristic tier")
        mode = "heuristic"
        try:
            found = scraper.search_heuristic()
        except Exception as e:
            logger.error(f"[{scraper.name}] Heuristic tier failed: {e}", exc_info=True)

    if not found and zeros >= ZERO_THRESHOLD_PLAYWRIGHT:
        logger.warning(f"[{scraper.name}] Escalating to Playwright tier")
        mode = "playwright"
        try:
            found = scraper.search_playwright()
        except Exception as e:
            logger.error(f"[{scraper.name}] Playwright tier failed: {e}", exc_info=True)

    return found, mode


# ── weekly digest ─────────────────────────────────────────────────────────────

def is_sunday() -> bool:
    return datetime.now().weekday() == 6


def build_weekly_digest(storage: PropertyStorage, exclude_ids: List[str]) -> List[dict]:
    if not is_sunday():
        return []
    return storage.get_top_properties(limit=5, exclude_ids=exclude_ids)


# ── main loop ─────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    storage = PropertyStorage("data/properties.db")
    all_new: List[Property] = []
    price_drops: List[Property] = []
    scraper_health: dict = {}

    # ── 1. scrape all sources ─────────────────────────────────────────────────
    all_scraped: List[Property] = []
    for ScraperClass in ALL_SCRAPERS:
        scraper = ScraperClass()
        found, mode = run_scraper_adaptive(scraper, storage)
        filtered = [p for p in found if passes_hard_filter(p)]
        logger.info(f"[{scraper.name}] mode={mode} total={len(found)} filtered={len(filtered)}")
        if not dry_run:
            storage.record_run(scraper.name, len(found), mode)
        health = storage.get_health(scraper.name)
        scraper_health[scraper.name] = {**health, "last_count": len(found), "last_mode": mode}
        all_scraped.extend(filtered)

    # ── 2. distance filter ────────────────────────────────────────────────────
    logger.info(f"Applying distance filter to {len(all_scraped)} properties…")
    within_range = apply_distance_filter(all_scraped)
    logger.info(f"{len(within_range)} properties within 20 km of Ermesinde")

    # ── 3. split new vs price drops ───────────────────────────────────────────
    price_drops = storage.check_and_update_price_drops(within_range)
    all_new = storage.filter_new(within_range)
    logger.info(f"New: {len(all_new)}  Price drops: {len(price_drops)}")

    to_process = all_new + price_drops
    if not to_process:
        logger.info("Nothing new or changed today.")
        if not dry_run:
            _maybe_send_health_alert(scraper_health, storage)
        return

    # ── 4. detail-page enrichment ─────────────────────────────────────────────
    logger.info(f"Fetching detail pages for up to {MAX_DETAIL_FETCHES} properties…")
    fetch_details_for(to_process)

    # ── 5. amenity enrichment ─────────────────────────────────────────────────
    logger.info("Enriching with nearby amenities…")
    enrich_with_amenities(to_process)

    # ── 6. score + sort ───────────────────────────────────────────────────────
    for prop in to_process:
        prop.match_score = score_property(prop)
    to_process.sort(key=lambda p: (-p.match_score, p.price or float("inf")))

    _print_summary(all_new, price_drops)
    _print_health(scraper_health)

    if dry_run:
        logger.info("Dry-run — skipping DB write and email.")
        return

    # ── 7. persist ────────────────────────────────────────────────────────────
    if all_new:
        storage.save(all_new)
    # Update scores / enriched fields for price drops (already in DB)
    if price_drops:
        storage.update_scores(price_drops)
    # Also update scores for newly saved properties
    if all_new:
        storage.update_scores(all_new)

    total_known = storage.count()

    # ── 8. weekly digest ──────────────────────────────────────────────────────
    sent_ids = [p.property_id for p in to_process]
    weekly_digest = build_weekly_digest(storage, exclude_ids=sent_ids)
    if weekly_digest:
        logger.info(f"Sunday digest: including top {len(weekly_digest)} properties")

    # ── 9. email ──────────────────────────────────────────────────────────────
    sent = send_email(
        new_properties=all_new,
        price_drops=price_drops,
        total_known=total_known,
        scraper_health=scraper_health,
        weekly_digest=weekly_digest,
    )
    if sent:
        storage.mark_sent(sent_ids)
        logger.info(f"Email sent. DB total: {total_known}")
    else:
        logger.error("Email failed — DB updated but not marked as sent.")


def _maybe_send_health_alert(scraper_health: dict, storage: PropertyStorage) -> None:
    broken = {n: h for n, h in scraper_health.items() if h["consecutive_zeros"] >= ZERO_THRESHOLD_PLAYWRIGHT}
    if broken:
        logger.warning(f"Broken scrapers: {list(broken.keys())} — sending alert email")
        send_email(new_properties=[], price_drops=[], total_known=storage.count(),
                   scraper_health=scraper_health, weekly_digest=[])


def _print_summary(new_props: List[Property], price_drops: List[Property]) -> None:
    print(f"\n{'='*65}")
    print(f"  {len(new_props)} new properties  |  {len(price_drops)} price drops")
    print(f"{'='*65}")
    for label, group in [("NEW", new_props), ("PRICE DROP", price_drops)]:
        for p in group:
            price_str = f"{p.price:,.0f} €" if p.price else "N/A"
            drop_str = f" (era {p.price_dropped_from:,.0f} €)" if p.price_dropped_from else ""
            print(f"\n[{label}][{p.source}] score={p.match_score}  {p.title[:55]}")
            print(f"  {price_str}{drop_str}  T{p.rooms or '?'}  {p.area_m2 or '?'}m²  {p.distance_km or '?'} km")
            print(f"  {p.url}")
    print()


def _print_health(scraper_health: dict) -> None:
    print(f"{'─'*65}")
    for name, h in scraper_health.items():
        z = h["consecutive_zeros"]
        mode = h["last_mode"]
        status = "OK" if z == 0 else f"⚠ {z} zero(s) — mode={mode}"
        print(f"  {name:<18} {status}")
    print()


# ── test helpers ──────────────────────────────────────────────────────────────

def send_test_email() -> None:
    sample = Property(
        url="https://www.idealista.pt/imovel/00000000/",
        source="Idealista",
        title="Apartamento T3 com varanda e 2 garagens — Ermesinde",
        price=295_000,
        location="Ermesinde, Valongo",
        rooms=3, area_m2=118, balcony_area_m2=24,
        has_garage=True, garage_spaces=2, has_outdoor=True,
        description="T3 espaçoso com 2 lugares de garagem, varanda de 24m², jardim privativo e cozinha open-space. Perto de escolas, farmácia e parque.",
        amenities_score=4, amenities_detail="Supermercado: 2 | Escola: 3 | Parque: 1 | Farmácia: 1 | Paragem: 8",
        match_score=9, distance_km=1.2,
        raw_data={"kitchen_living_combined_m2": 40},
    )
    drop = Property(
        url="https://www.imovirtual.com/imovel/99999999/",
        source="Imovirtual",
        title="Moradia T4 com jardim — Alfena",
        price=340_000, price_dropped_from=365_000,
        location="Alfena, Valongo",
        rooms=4, area_m2=145, balcony_area_m2=30,
        has_garage=True, garage_spaces=2, has_outdoor=True,
        amenities_score=3, amenities_detail="Supermercado: 1 | Escola: 2 | Parque: 1 | Farmácia: 0 | Paragem: 5",
        match_score=8, distance_km=4.5,
    )
    send_email(
        new_properties=[sample],
        price_drops=[drop],
        total_known=42,
        scraper_health={
            "Idealista": {"consecutive_zeros": 0, "last_mode": "normal"},
            "OLX": {"consecutive_zeros": 3, "last_mode": "heuristic"},
        },
        weekly_digest=[],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ermesinde property search")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    args = parser.parse_args()

    if args.test_email:
        send_test_email()
    else:
        run(dry_run=args.dry_run)
