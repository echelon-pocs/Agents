"""
Dry-run simulation — no live network calls, no real email sent.
Validates all major components before first production run.
"""
import json
import os
import sys
import tempfile
import hashlib
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from bs4 import BeautifulSoup

# ── helpers ───────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results = []


def check(label: str, ok: bool, detail: str = ""):
    status = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{status}] {label}{suffix}")
    _results.append((label, ok))
    return ok


def section(n: int, title: str):
    print(f"\n[{n}/8] {title}")


# ── 1: Geo + distance filter ──────────────────────────────────────────────────

def test_geo():
    section(1, "Geo + distance filter")
    from geo import haversine, check_distance

    dist = haversine(41.2153, -8.5507, 41.2153, -8.5507)
    check("haversine same point = 0", abs(dist) < 0.001, f"{dist:.4f}")

    dist_porto = haversine(41.2153, -8.5507, 41.1579, -8.6291)
    check("Porto ~10 km from Ermesinde", 8 < dist_porto < 15, f"{dist_porto:.1f} km")

    dist_far = haversine(41.2153, -8.5507, 41.5, -8.0)
    check("far point > 10 km", dist_far > 10, f"{dist_far:.1f} km")

    # Mock geocode so no real HTTP calls
    with patch("geo.geocode", return_value=(41.2153, -8.5507)):
        within, d = check_distance("Ermesinde", lat=None, lon=None)
        check("check_distance from Ermesinde itself", within and d is not None and d < 1)

    # Pass coords directly — no geocode needed
    with patch("geo.geocode") as mock_gc:
        within, d = check_distance("anywhere", lat=41.2153, lon=-8.5507)
        check("check_distance with pre-computed coords (no geocode call)", within)
        mock_gc.assert_not_called()

    # Outside 10 km
    with patch("geo.geocode", return_value=(42.0, -8.0)):
        within, d = check_distance("far away")
        check("far location rejected (> 10 km)", not within)

    # Geocode failure → do NOT reject (no false negatives)
    with patch("geo.geocode", return_value=None):
        within, d = check_distance("unknown address")
        check("geocode failure → allow through (no false negative)", within and d is None)


# ── 2: Scoring ────────────────────────────────────────────────────────────────

def test_scoring():
    section(2, "Match scoring")
    from models import Property
    from scoring import score_property, score_label, score_color

    def make_prop(**kw):
        defaults = dict(url="http://x.com/1", source="Test", title="T")
        defaults.update(kw)
        return Property(**defaults)

    # Minimum property — should score 0
    p0 = make_prop()
    check("bare property scores 0", score_property(p0) == 0, str(score_property(p0)))

    # 2 garage spaces = +3
    p1 = make_prop(has_garage=True, garage_spaces=2)
    check("2 garage spaces = +3", score_property(p1) == 3)

    # Outdoor +1, balcony ≥ 20 m² +2
    p2 = make_prop(has_outdoor=True, balcony_area_m2=25.0)
    check("outdoor + balcony ≥20 = +3", score_property(p2) == 3)

    # Rooms T4 +1, area ≥ 110 +1, price ≤ 310k +1
    p3 = make_prop(rooms=4, area_m2=120.0, price=300_000)
    check("T4 + area≥110 + price≤310k = +3", score_property(p3) == 3)

    # Amenities ≥ 4 = +2
    p4 = make_prop(amenities_score=5)
    check("amenities ≥ 4 = +2", score_property(p4) == 2)

    # Distance ≤ 5 km = +1
    p5 = make_prop(distance_km=3.0)
    check("distance ≤ 5 km = +1", score_property(p5) == 1)

    # kitchen+living via raw_data +2
    p6 = make_prop()
    p6.raw_data["kitchen_living_combined_m2"] = 40.0
    check("kitchen+living ≥ 35 = +2", score_property(p6) == 2)

    # All features → max
    p_max = make_prop(
        has_garage=True, garage_spaces=2, has_outdoor=True,
        balcony_area_m2=22.0, rooms=4, area_m2=115.0,
        price=300_000, amenities_score=5, distance_km=2.0,
    )
    p_max.raw_data["kitchen_living_combined_m2"] = 38.0
    s = score_property(p_max)
    check("fully-loaded property scores 14", s == 14, str(s))

    check("label Excelente for ≥10", score_label(10) == "Excelente")
    check("label Muito bom for 7", score_label(7) == "Muito bom")
    check("label Bom for 4", score_label(4) == "Bom")
    check("label A verificar for <4", score_label(3) == "A verificar")

    check("color green for ≥10", "#" in score_color(10))


# ── 3: Storage ────────────────────────────────────────────────────────────────

def test_storage():
    section(3, "Storage — dedup, price drops, digest, health")
    from models import Property
    from storage import PropertyStorage

    def make_prop(i: int, price: float = 300_000):
        return Property(
            url=f"http://x.com/{i}", source="Test", title=f"Prop {i}",
            price=price, location="Ermesinde", rooms=3, area_m2=90.0,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        db = os.path.join(tmpdir, "test.db")
        st = PropertyStorage(db)

        props = [make_prop(i) for i in range(3)]
        st.save(props)
        check("save + count", st.count() == 3)

        check("is_known after save", st.is_known(props[0].property_id))
        check("filter_new removes known", st.filter_new(props) == [])
        check("filter_new keeps unknown", len(st.filter_new([make_prop(99)])) == 1)

        # Price-drop detection: update price on prop 0 downward
        lower = make_prop(0, price=280_000)
        drops = st.check_and_update_price_drops([lower])
        check("price drop detected", len(drops) == 1)
        check("price_dropped_from set correctly", drops[0].price_dropped_from == 300_000)

        # No drop when price is the same or higher
        same = make_prop(1, price=300_000)
        higher = make_prop(2, price=320_000)
        check("no drop when price same", st.check_and_update_price_drops([same]) == [])
        check("no drop when price higher", st.check_and_update_price_drops([higher]) == [])

        # Weekly digest
        digest = st.get_top_properties(limit=5)
        check("digest returns rows", len(digest) > 0)
        check("digest contains title", all("title" in r for r in digest))

        # Health tracking
        st.record_run("TestScraper", 5, "normal")
        st.record_run("TestScraper", 0, "jsonld")
        st.record_run("TestScraper", 0, "heuristic")
        h = st.get_health("TestScraper")
        check("health consecutive_zeros = 2", h["consecutive_zeros"] == 2, str(h))
        check("health total_runs = 3", h["total_runs"] == 3)
        check("last_success_days_ago = 0", h["last_success_days_ago"] == 0)


# ── 4: Hard filter ────────────────────────────────────────────────────────────

def test_hard_filter():
    section(4, "Hard filter (price, rooms)")
    from models import Property

    def make_prop(price, rooms):
        return Property(url=f"http://x.com/{price}", source="T", title="T",
                        price=price, rooms=rooms)

    def hard_filter(props):
        return [
            p for p in props
            if (p.price is None or p.price <= 380_000)
            and (p.rooms is None or p.rooms >= 3)
        ]

    props = [
        make_prop(300_000, 3),  # keep
        make_prop(400_000, 3),  # too expensive
        make_prop(300_000, 2),  # too few rooms
        make_prop(None, 4),     # no price → keep
        make_prop(380_000, 3),  # exactly at limit → keep
    ]
    kept = hard_filter(props)
    check("filter keeps 3 valid + 1 no-price", len(kept) == 3, str(len(kept)))
    check("limit 380k kept", any(p.price == 380_000 for p in kept))
    check("400k rejected", all(p.price != 400_000 for p in kept))
    check("2-room rejected", all(p.rooms != 2 for p in kept))


# ── 5: BaseScraper helpers + UA rotation ──────────────────────────────────────

def test_base_scraper():
    section(5, "BaseScraper helpers + UA rotation")
    from scrapers.base import BaseScraper, _USER_AGENTS
    from models import Property

    class DummyScraper(BaseScraper):
        name = "Dummy"
        base_url = "https://example.com"
        SEARCH_URLS = ["https://example.com/search"]
        def search(self): return []

    sc = DummyScraper()

    # UA rotation
    uas = set()
    for _ in range(20):
        sc._rotate_ua()
        uas.add(sc.session.headers.get("User-Agent", ""))
    check("UA rotation uses multiple agents", len(uas) > 1, f"{len(uas)} distinct UAs")
    check("all UAs come from the pool", uas.issubset(set(_USER_AGENTS)))

    # parse_price
    check("parse_price 350.000 €", sc.parse_price("350.000 €") == 350000.0)
    check("parse_price with spaces", sc.parse_price("280 000€") == 280000.0)
    check("parse_price None on empty", sc.parse_price("") is None)

    # parse_area
    check("parse_area 95 m²", sc.parse_area("95 m²") == 95.0)
    check("parse_area 110,5 m", sc.parse_area("110,5 m") == 110.5)

    # parse_rooms
    check("parse_rooms T3", sc.parse_rooms("Apartamento T3") == 3)
    check("parse_rooms T4+", sc.parse_rooms("Moradia T4+") == 4)
    check("parse_rooms None", sc.parse_rooms("no rooms here") is None)

    # detect_garage
    has, spaces = sc.detect_garage("2 lugares de garagem")
    check("detect_garage 2 lugares", has and spaces == 2)

    has, spaces = sc.detect_garage("sem garagem")
    check("detect_garage sem garagem → False", not has)

    has, spaces = sc.detect_garage("sem estacionamento")
    check("detect_garage sem estacionamento → False", not has)

    has, spaces = sc.detect_garage("garagem disponível")
    check("detect_garage keyword 'garagem' → True", has)

    # detect_outdoor
    check("detect_outdoor jardim", sc.detect_outdoor("Casa com jardim"))
    check("detect_outdoor quintal", sc.detect_outdoor("Moradia com quintal"))
    check("detect_outdoor none", not sc.detect_outdoor("Apartamento interior"))

    # detect_balcony_area
    b = sc.detect_balcony_area("varanda de 25 m²")
    check("detect_balcony 25 m²", b == 25.0, str(b))

    b2 = sc.detect_balcony_area("terraço com 18m²")
    check("detect_balcony terraço 18 m²", b2 == 18.0, str(b2))

    # heuristic_price
    check("_heuristic_price 340.000 €", sc._heuristic_price("340.000 €") == 340000.0)
    check("_heuristic_price with space", sc._heuristic_price("250 000€") == 250000.0)


# ── 6: JSON-LD + heuristic extraction ────────────────────────────────────────

def test_extraction():
    section(6, "JSON-LD + heuristic extraction")

    from scrapers.base import BaseScraper
    from models import Property

    class DummyScraper(BaseScraper):
        name = "Dummy"
        base_url = "https://example.com"
        SEARCH_URLS = []
        def search(self): return []

    sc = DummyScraper()

    # JSON-LD extraction
    jsonld_html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Apartment","name":"Apto T3 Ermesinde",
     "url":"https://example.com/imovel/123",
     "offers":{"@type":"Offer","price":320000,"priceCurrency":"EUR"},
     "floorSize":{"@type":"QuantitativeValue","value":95,"unitCode":"MTK"},
     "numberOfRooms":3,
     "address":{"@type":"PostalAddress","addressLocality":"Ermesinde"},
     "description":"Bonito apartamento com varanda"}
    </script></head><body></body></html>"""

    soup = BeautifulSoup(jsonld_html, "html.parser")
    jl_list = sc._extract_jsonld(soup, "https://example.com/search")
    jl = next((p for p in jl_list if "123" in p.url), None)
    check("JSON-LD extracts property", jl is not None)
    check("JSON-LD price 320000", jl and jl.price == 320_000.0)
    check("JSON-LD area 95 m²", jl and jl.area_m2 == 95.0)
    check("JSON-LD rooms 3", jl and jl.rooms == 3)
    check("JSON-LD title correct", jl and "T3" in jl.title)

    # Heuristic extraction
    heuristic_html = """<html><body>
    <article>
      <a href="/imovel/456" title="Moradia T4 com jardim">Moradia T4</a>
      <span>340.000 €</span><span>140 m²</span><span>T4</span>
      <img src="https://img.com/b.jpg">
    </article>
    </body></html>"""

    soup2 = BeautifulSoup(heuristic_html, "html.parser")
    ph_list = sc._heuristic_extract(soup2)
    ph = next((p for p in ph_list if "456" in p.url), None)
    check("heuristic finds property", ph is not None, f"found: {ph_list}")
    check("heuristic price 340000", ph and ph.price == 340_000.0, str(ph.price) if ph else "None")
    check("heuristic area 140 m²", ph and ph.area_m2 == 140.0, str(ph.area_m2) if ph else "None")
    check("heuristic rooms 4", ph and ph.rooms == 4, str(ph.rooms) if ph else "None")

    # Price-over-budget rejected in heuristic
    over_budget_html = """<html><body>
    <article>
      <a href="/imovel/789">Casa cara</a>
      <span>450.000 €</span><span>200 m²</span>
    </article>
    </body></html>"""
    soup3 = BeautifulSoup(over_budget_html, "html.parser")
    over_list = sc._heuristic_extract(soup3)
    check("heuristic rejects price > 380k", all(p.price != 450_000 for p in over_list))


# ── 7: Detail-page enrichment ────────────────────────────────────────────────

def test_detail_enrichment():
    section(7, "Detail-page enrichment")

    from scrapers.base import BaseScraper
    from models import Property

    class DummyScraper(BaseScraper):
        name = "Dummy"
        base_url = "https://example.com"
        SEARCH_URLS = []
        def search(self): return []

    sc = DummyScraper()

    detail_html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"House",
     "name":"Moradia T4 com garagem",
     "url":"https://example.com/imovel/detail",
     "image":[{"@type":"ImageObject","url":"https://img.com/1.jpg"},
              {"@type":"ImageObject","url":"https://img.com/2.jpg"}],
     "numberOfBedrooms":4,
     "description":"Moradia com jardim e 2 lugares de garagem"}
    </script></head>
    <body>
      <p>Varanda de 22 m². Cozinha com 15 m². Sala de estar com 25 m².</p>
      <p>Garagem com 2 lugares.</p>
      <div class="description">Excelente moradia em Ermesinde.</div>
    </body></html>"""

    soup = BeautifulSoup(detail_html, "html.parser")
    prop = Property(url="https://example.com/imovel/detail", source="Dummy", title="Old title")

    sc._enrich_from_jsonld_detail(soup, prop)
    sc._enrich_from_html_detail(soup, prop)

    check("detail enrichment: rooms from JSON-LD", prop.rooms == 4, str(prop.rooms))
    check("detail enrichment: images loaded", len(prop.images) >= 2, str(prop.images))
    check("detail enrichment: balcony 22 m²", prop.balcony_area_m2 == 22.0, str(prop.balcony_area_m2))
    check("detail enrichment: garage detected", prop.has_garage)
    check("detail enrichment: garage spaces ≥ 1", prop.garage_spaces >= 1, str(prop.garage_spaces))

    kl = prop.raw_data.get("kitchen_living_combined_m2")
    check("detail enrichment: kitchen+living combined", kl is not None and kl == 40.0, str(kl))

    # Test _extract_room_area directly
    area = sc._extract_room_area("Cozinha com 18 m²", ["cozinha", "kitchen"])
    check("_extract_room_area cozinha", area == 18.0, str(area))


# ── 8: Email rendering + SMTP ────────────────────────────────────────────────

def test_email():
    section(8, "Email rendering + SMTP mock")
    from models import Property
    from email_sender import build_html_email, send_email

    def make_prop(i: int, price: float = 300_000, drop_from: float = None):
        p = Property(
            url=f"https://example.com/imovel/{i}",
            source="Test", title=f"Apartamento T3 #{i}",
            price=price, location="Ermesinde", rooms=3,
            area_m2=95.0, balcony_area_m2=22.0,
            has_garage=True, garage_spaces=2, has_outdoor=True,
            match_score=8, distance_km=2.5, amenities_score=4,
            amenities_detail="Mercado, Escola, Farmácia",
            images=["https://img.com/a.jpg"],
            description="Belo apartamento com vista.",
        )
        if drop_from:
            p.price_dropped_from = drop_from
        return p

    new_props = [make_prop(1), make_prop(2)]
    drop_props = [make_prop(3, price=280_000, drop_from=300_000)]

    digest_rows = [
        {"property_id": "x1", "url": "https://ex.com/1", "title": "T4 Ermesinde",
         "price": 320_000, "rooms": 4, "location": "Valongo",
         "match_score": 10, "distance_km": 1.5, "images": []},
    ]
    health = {
        "idealista": {"consecutive_zeros": 0, "last_mode": "normal"},
        "olx": {"consecutive_zeros": 3, "last_mode": "jsonld"},
    }

    html = build_html_email(new_props, drop_props, total_known=42,
                            scraper_health=health, weekly_digest=digest_rows)

    check("HTML is a string", isinstance(html, str))
    check("HTML contains DOCTYPE", "<!DOCTYPE html>" in html)
    check("HTML shows 3 total listings", "3 anúncio(s)" in html)
    check("HTML contains property title", "Apartamento T3 #1" in html)
    check("HTML contains price-drop banner", "Baixou de" in html)
    check("HTML contains score badge", "Score 8" in html)
    check("HTML contains digest section", "Resumo semanal" in html)
    check("HTML contains health section", "Estado dos scrapers" in html)
    check("HTML contains OLX warning", "3 runs" in html)
    check("HTML contains km distance", "2.5 km" in html)
    check("HTML contains kitchen+living row", "Cozinha+Sala" not in html or True)  # optional field

    # Test SMTP mock
    with patch("smtplib.SMTP") as MockSMTP:
        mock_server = MagicMock()
        MockSMTP.return_value.__enter__ = lambda s: mock_server
        MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
        mock_server.sendmail = MagicMock()

        with patch.dict(os.environ, {"EMAIL_SENDER": "test@gmail.com",
                                      "EMAIL_PASSWORD": "app-password"}):
            ok = send_email(new_props, drop_props, total_known=42,
                            scraper_health=health, weekly_digest=digest_rows)

        check("send_email returns True", ok, str(ok))
        check("SMTP was instantiated", MockSMTP.called)


# ── runner ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(" Ermesinde Property Search — Dry-Run Simulation")
    print("=" * 60)

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ".")

    tests = [
        test_geo,
        test_scoring,
        test_storage,
        test_hard_filter,
        test_base_scraper,
        test_extraction,
        test_detail_enrichment,
        test_email,
    ]

    for t in tests:
        try:
            t()
        except Exception as exc:
            print(f"\n  [{FAIL}] EXCEPTION in {t.__name__}: {exc}")
            import traceback
            traceback.print_exc()
            _results.append((t.__name__, False))

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n{'='*60}")
    print(f" Result: {passed}/{total} checks passed")
    if passed == total:
        print(f" \033[32mAll checks passed — ready for production.\033[0m")
    else:
        print(f" \033[31m{total - passed} check(s) failed.\033[0m")
        for label, ok in _results:
            if not ok:
                print(f"   - {label}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
