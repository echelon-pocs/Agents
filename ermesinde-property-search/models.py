from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
import hashlib


@dataclass
class Property:
    url: str
    source: str
    title: str
    price: Optional[float] = None
    location: str = ""
    rooms: Optional[int] = None
    area_m2: Optional[float] = None
    balcony_area_m2: Optional[float] = None
    has_garage: bool = False
    garage_spaces: int = 0
    has_outdoor: bool = False
    description: str = ""
    images: List[str] = field(default_factory=list)
    found_at: datetime = field(default_factory=datetime.now)
    amenities_score: int = 0
    amenities_detail: str = ""
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def property_id(self) -> str:
        return hashlib.md5(self.url.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "property_id": self.property_id,
            "url": self.url,
            "source": self.source,
            "title": self.title,
            "price": self.price,
            "location": self.location,
            "rooms": self.rooms,
            "area_m2": self.area_m2,
            "balcony_area_m2": self.balcony_area_m2,
            "has_garage": self.has_garage,
            "garage_spaces": self.garage_spaces,
            "has_outdoor": self.has_outdoor,
            "description": self.description,
            "images": self.images,
            "found_at": self.found_at.isoformat(),
            "amenities_score": self.amenities_score,
            "amenities_detail": self.amenities_detail,
        }
