import sqlite3
import json
from datetime import datetime
from typing import List
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
