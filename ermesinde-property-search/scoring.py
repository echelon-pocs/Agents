"""
Match score for properties — higher score = better fit for the requirements.
Max achievable score: 14 points.
"""
from models import Property


def score_property(prop: Property) -> int:
    s = 0

    # Garage — any garage counts, 2+ spots is a bonus
    if prop.garage_spaces >= 2:
        s += 3
    elif prop.has_garage:
        s += 2

    # Outdoor space / balcony / terrace
    if prop.balcony_area_m2 is not None:
        if prop.balcony_area_m2 >= 20:
            s += 2
        elif prop.balcony_area_m2 >= 10:
            s += 1
    elif prop.has_outdoor:
        s += 1

    # Kitchen + living room combined ≥ 20 m² (from detail scraping)
    combined = prop.raw_data.get("kitchen_living_combined_m2")
    if combined is not None:
        if combined >= 35:
            s += 2
        elif combined >= 20:
            s += 1

    # Typology: T4+ is a bonus
    if prop.rooms is not None and prop.rooms >= 4:
        s += 1

    # Total area ≥ 110 m² as proxy when detail data is unavailable
    if prop.area_m2 is not None and prop.area_m2 >= 110:
        s += 1

    # Amenities score from OpenStreetMap
    if prop.amenities_score >= 4:
        s += 2
    elif prop.amenities_score >= 2:
        s += 1

    # Price headroom below budget
    if prop.price is not None and prop.price <= 310_000:
        s += 1

    # Proximity to Ermesinde center (≤ 5 km = within the town itself / direct vicinity)
    if prop.distance_km is not None and prop.distance_km <= 5:
        s += 1

    return s


def score_label(score: int) -> str:
    if score >= 10:
        return "Excelente"
    if score >= 7:
        return "Muito bom"
    if score >= 4:
        return "Bom"
    return "A verificar"


def score_color(score: int) -> str:
    if score >= 10:
        return "#1b5e20"
    if score >= 7:
        return "#2e7d32"
    if score >= 4:
        return "#f57f17"
    return "#757575"
