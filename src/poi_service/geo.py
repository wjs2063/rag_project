"""Geospatial validation and distance calculation."""

import math


def point_wkt(longitude: float, latitude: float) -> str:
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        raise ValueError("coordinates must be finite")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError("coordinates outside WGS84 range")
    return f"POINT({longitude:.8f} {latitude:.8f})"


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6_371_008.8
    a1, a2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = a2 - a1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(min(1, math.sqrt(a)))
