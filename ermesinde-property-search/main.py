#!/usr/bin/env python3
"""
Ermesinde property search — daily runner.

Usage:
    python main.py              # full run: scrape, filter, email
    python main.py --dry-run    # scrape and print results, no email
    python main.py --test-email # send test email with sample data
"""
import argparse
import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from scrapers import ALL_SCRAPERS
from storage import PropertyStorage
from email_sender import send_email
from amenities import enrich_property_amenities
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

# ── Minimum criteria filter ───────────────────────────────────────────────────
MIN_ROOMS = 3
MAX_PRICE = 380_000


def passes_filter(prop: Property) -> bool:
    """Hard filters — properties that clearly don't meet criteria are dropped."""
    if prop.rooms is not None and prop.rooms < MIN_ROOMS:
        return False
    if prop.price is not None and prop.price > MAX_PRICE:
        return False
    return True


def enrich_with_amenities(properties: list[Property]) -> None:
    """Queries OpenStreetMap for nearby amenities (in-place update)."""
    for prop in properties:
        if not prop.location:
            continue
        try:
            score, detail = enrich_property_amenities(prop.location)
            prop.amenities_score = score
            prop.amenities_detail = detail
            logger.debug(f"Amenities for '{prop.location}': {score}/5 — {detail}")
        except Exception as e:
            logger.warning(f"Amenities enrichment failed for '{prop.location}': {e}")


def print_summary(properties: list[Property]) -> None:
    print(f"\n{'='*60}")
    print(f"  Found {len(properties)} new properties")
    print(f"{'='*60}")
    for p in properties:
        price_str = f"{p.price:,.0f} €" if p.price else "N/A"
        rooms_str = f"T{p.rooms}" if p.rooms else "T?"
        area_str = f"{p.area_m2:.0f}m²" if p.area_m2 else "?m²"
        garage_str = f"Garagem:{p.garage_spaces}" if p.has_garage else "Sem garagem"
        outdoor_str = "Exterior" if p.has_outdoor else ""
        print(
            f"\n[{p.source}] {p.title[:60]}"
            f"\n  {price_str}  {rooms_str}  {area_str}  {garage_str}  {outdoor_str}"
            f"\n  {p.location}"
            f"\n  {p.url}"
        )
    print()


# Number of consecutive zero-result runs before escalating to a higher tier
ZERO_THRESHOLD_JSONLD = 2
ZERO_THRESHOLD_HEURISTIC = 3


def run_scraper_adaptive(scraper, storage: PropertyStorage) -> tuple[list[Property], str]:
    """
    3-tier adaptive scraping:
      Tier 1 (normal)    — CSS selectors + Next.js JSON extraction
      Tier 2 (JSON-LD)   — schema.org structured data, survives visual redesigns
      Tier 3 (heuristic) — pattern-based link/price scan, fully site-agnostic
    Escalates to the next tier when consecutive zero runs exceed the threshold.
    Returns (properties, mode_used).
    """
    health = storage.get_health(scraper.name)
    zeros = health["consecutive_zeros"]

    # Always try normal first
    mode = "normal"
    found: list[Property] = []
    try:
        found = scraper.search()
    except Exception as e:
        logger.error(f"[{scraper.name}] Normal scrape failed: {e}", exc_info=True)

    if not found and zeros >= ZERO_THRESHOLD_JSONLD:
        logger.warning(
            f"[{scraper.name}] {zeros} consecutive zero runs — trying JSON-LD tier"
        )
        mode = "jsonld"
        try:
            found = scraper.search_jsonld()
        except Exception as e:
            logger.error(f"[{scraper.name}] JSON-LD scrape failed: {e}", exc_info=True)

    if not found and zeros >= ZERO_THRESHOLD_HEURISTIC:
        logger.warning(
            f"[{scraper.name}] Still zero after JSON-LD — trying heuristic tier"
        )
        mode = "heuristic"
        try:
            found = scraper.search_heuristic()
        except Exception as e:
            logger.error(f"[{scraper.name}] Heuristic scrape failed: {e}", exc_info=True)

    return found, mode


def run(dry_run: bool = False) -> None:
    storage = PropertyStorage("data/properties.db")
    all_new: list[Property] = []
    scraper_health: dict = {}

    for ScraperClass in ALL_SCRAPERS:
        scraper = ScraperClass()
        found, mode = run_scraper_adaptive(scraper, storage)
        filtered = [p for p in found if passes_filter(p)]
        new_props = storage.filter_new(filtered)

        logger.info(
            f"[{scraper.name}] mode={mode} total={len(found)} "
            f"filtered={len(filtered)} new={len(new_props)}"
        )

        if not dry_run:
            storage.record_run(scraper.name, len(found), mode)

        health = storage.get_health(scraper.name)
        scraper_health[scraper.name] = {**health, "last_count": len(found), "last_mode": mode}

        all_new.extend(new_props)

    if not all_new:
        logger.info("No new properties found — no email sent.")
        if not dry_run:
            _maybe_send_health_alert(scraper_health, storage)
        return

    # Sort by price (ascending, unknowns last)
    all_new.sort(key=lambda p: p.price if p.price else float("inf"))

    logger.info(f"Enriching {len(all_new)} properties with nearby amenities…")
    enrich_with_amenities(all_new)

    print_summary(all_new)
    _print_health(scraper_health)

    if dry_run:
        logger.info("Dry-run mode — skipping database save and email.")
        return

    storage.save(all_new)
    total_known = storage.count()

    sent = send_email(all_new, total_known, scraper_health=scraper_health)
    if sent:
        storage.mark_sent([p.property_id for p in all_new])
        logger.info(f"Email sent. Total in DB: {total_known}")
    else:
        logger.error("Email failed — properties saved to DB but not marked as sent.")


def _maybe_send_health_alert(scraper_health: dict, storage: PropertyStorage) -> None:
    """Send a health-only email if any scraper has been broken for many days."""
    broken = {
        name: h for name, h in scraper_health.items()
        if h["consecutive_zeros"] >= ZERO_THRESHOLD_HEURISTIC + 1
    }
    if broken:
        logger.warning(f"Scrapers possibly broken: {list(broken.keys())} — sending alert")
        send_email([], total_known=storage.count(), scraper_health=scraper_health)


def _print_health(scraper_health: dict) -> None:
    print(f"\n{'─'*60}")
    print("  Scraper health")
    print(f"{'─'*60}")
    for name, h in scraper_health.items():
        zeros = h["consecutive_zeros"]
        mode = h["last_mode"]
        status = "OK" if zeros == 0 else f"⚠ {zeros} zero run(s)"
        tier = f" [mode={mode}]" if mode != "normal" else ""
        print(f"  {name:<15} {status}{tier}")
    print()


def send_test_email() -> None:
    from email_sender import send_email
    sample = Property(
        url="https://www.idealista.pt/imovel/00000000/",
        source="Idealista",
        title="Apartamento T3 com varanda e garagem — Ermesinde",
        price=320_000,
        location="Ermesinde, Valongo",
        rooms=3,
        area_m2=115,
        balcony_area_m2=25,
        has_garage=True,
        garage_spaces=2,
        has_outdoor=True,
        description="Excelente apartamento T3 com dois lugares de garagem, varanda de 25m², jardim privado e cozinha espaçosa. Perto de escolas, supermercados e parque.",
        amenities_score=4,
        amenities_detail="Supermercado: 2 | Escola: 3 | Parque/Recreio: 1 | Farmácia: 1 | Paragem: 8",
    )
    send_email([sample], total_known=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ermesinde property search")
    parser.add_argument("--dry-run", action="store_true", help="Scrape only, no email or DB write")
    parser.add_argument("--test-email", action="store_true", help="Send a test email")
    args = parser.parse_args()

    if args.test_email:
        send_test_email()
    else:
        run(dry_run=args.dry_run)
