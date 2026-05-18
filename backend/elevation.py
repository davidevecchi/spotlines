"""Elevation sampling from cached DEM. Replaces Open-Meteo API."""
from __future__ import annotations

import math
from typing import Optional

from .analysis import Pair
from .dem import get_elevation_at_points

_SAMPLES = 10


def fetch_elevations(pairs: list[Pair]) -> None:
    """Sample terrain elevations from the DEM cache and mutate pairs in-place."""
    if not pairs:
        return

    pair_points: list[list[tuple[float, float]]] = [
        _interpolate(p.anchor_a.lat, p.anchor_a.lon,
                     p.anchor_b.lat, p.anchor_b.lon, _SAMPLES)
        for p in pairs
    ]

    # Deduplicate at ~1 m precision (5 decimal places ≈ 1.1 m)
    key_to_idx: dict[tuple[float, float], int] = {}
    lats: list[float] = []
    lons: list[float] = []
    for pts in pair_points:
        for lat, lon in pts:
            k = (round(lat, 5), round(lon, 5))
            if k not in key_to_idx:
                key_to_idx[k] = len(lats)
                lats.append(lat)
                lons.append(lon)

    elevs = get_elevation_at_points(lats, lons)

    for pair, pts in zip(pairs, pair_points):
        terrain: list[Optional[float]] = []
        for lat, lon in pts:
            k = (round(lat, 5), round(lon, 5))
            e = elevs[key_to_idx[k]]
            terrain.append(round(e, 1) if e is not None else None)

        ea, eb = terrain[0], terrain[-1]
        pair.terrain_elevs = terrain
        pair.elev_a = ea
        pair.elev_b = eb
        if ea is not None and eb is not None and pair.distance_m > 0:
            diff = abs(eb - ea)
            pair.slope_pct = round(diff / pair.distance_m * 100, 1)
            pair.slope_deg = round(math.degrees(math.atan(diff / pair.distance_m)), 1)


def _interpolate(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    n: int,
) -> list[tuple[float, float]]:
    return [
        (lat1 + (lat2 - lat1) * i / (n - 1), lon1 + (lon2 - lon1) * i / (n - 1))
        for i in range(n)
    ]
