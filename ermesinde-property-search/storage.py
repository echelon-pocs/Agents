import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Dict
from pathlib import Path

from models import Property


class PropertyStorage:
    def __init__(self, db_path: str = "data/properties.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS properties (
                    property_id TEXT PRIMARY KEY,
                    url TEXT UNIQUE NOT NULL,
                    source TEXT,
                    title TEXT,
                    price REAL,
                    location TEXT,
                    rooms INTEGER,
                    area_m2 REAL,
                    balcony_area_m2 REAL,
                    has_garage INTEGER DEFAULT 0,
                    garage_spaces INTEGER DEFAULT 0,
                    has_outdoor INTEGER DEFAULT 0,
                    description TEXT,
                    images TEXT,
                    amenities_score INTEGER DEFAULT 0,
                    amenities_detail TEXT,
                    found_at TEXT,
                    sent_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scraper_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scraper TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    found_count INTEGER NOT NULL,
                    mode TEXT NOT NULL
                )
            """)
            conn.commit()

    def is_known(self, property_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM properties WHERE property_id = ?", (property_id,)
            ).fetchone()
            return row is not None

    def filter_new(self, properties: List[Property]) -> List[Property]:
        return [p for p in properties if not self.is_known(p.property_id)]

    def save(self, properties: List[Property]):
        with sqlite3.connect(self.db_path) as conn:
            for prop in properties:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO properties
                        (property_id, url, source, title, price, location, rooms,
                         area_m2, balcony_area_m2, has_garage, garage_spaces,
                         has_outdoor, description, images, amenities_score,
                         amenities_detail, found_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        prop.property_id, prop.url, prop.source, prop.title,
                        prop.price, prop.location, prop.rooms, prop.area_m2,
                        prop.balcony_area_m2, int(prop.has_garage),
                        prop.garage_spaces, int(prop.has_outdoor),
                        prop.description, json.dumps(prop.images),
                        prop.amenities_score, prop.amenities_detail,
                        prop.found_at.isoformat(),
                    ),
                )
            conn.commit()

    def mark_sent(self, property_ids: List[str]):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            for pid in property_ids:
                conn.execute(
                    "UPDATE properties SET sent_at = ? WHERE property_id = ?",
                    (now, pid),
                )
            conn.commit()

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]

    # ── scraper health ────────────────────────────────────────────────────────

    def record_run(self, scraper_name: str, found_count: int, mode: str = "normal"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO scraper_runs (scraper, run_at, found_count, mode) VALUES (?,?,?,?)",
                (scraper_name, datetime.now().isoformat(), found_count, mode),
            )
            conn.commit()

    def get_health(self, scraper_name: str, lookback_days: int = 7) -> Dict:
        """Returns consecutive_zeros, last_success_days_ago, total_runs, last_mode."""
        since = (datetime.now() - timedelta(days=lookback_days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT found_count, mode, run_at FROM scraper_runs "
                "WHERE scraper = ? AND run_at >= ? ORDER BY run_at DESC",
                (scraper_name, since),
            ).fetchall()

        if not rows:
            return {"consecutive_zeros": 0, "last_success_days_ago": None,
                    "total_runs": 0, "last_mode": None}

        consecutive_zeros = 0
        for count, _, _ in rows:
            if count == 0:
                consecutive_zeros += 1
            else:
                break

        last_success_days_ago = None
        for count, _, run_at in rows:
            if count > 0:
                delta = datetime.now() - datetime.fromisoformat(run_at)
                last_success_days_ago = delta.days
                break

        return {
            "consecutive_zeros": consecutive_zeros,
            "last_success_days_ago": last_success_days_ago,
            "total_runs": len(rows),
            "last_mode": rows[0][1] if rows else None,
        }

    def all_health(self) -> Dict[str, Dict]:
        with sqlite3.connect(self.db_path) as conn:
            scrapers = conn.execute(
                "SELECT DISTINCT scraper FROM scraper_runs"
            ).fetchall()
        return {row[0]: self.get_health(row[0]) for row in scrapers}
