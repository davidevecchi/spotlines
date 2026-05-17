"""Open-Meteo elevation fetch with deduplication and rate-limiting."""
from __future__ import annotations

import math
import time
from typing import Optional

import requests

from .analysis import Pair

_URL = "https://api.open-meteo.com/v1/elevation"
_SAMPLES = 10
_BATCH = 100
_SLEEP = 0.15  # seconds between batches


def fetch_elevations(pairs: list[Pair]) -> None:
    """Fetch terrain elevations and mutate pairs in-place. Silently ignores errors."""
    if not pairs:
        return

    # Interpolate sample points for every pair
    pair_points: list[list[tuple[float, float]]] = []
    for p in pairs:
        pair_points.append(_interpolate(
            p.anchor_a.lat, p.anchor_a.lon,
            p.anchor_b.lat, p.anchor_b.lon,
            _SAMPLES,
        ))

    # Deduplicate by rounding to 4 decimal places (~11 m)
    key_to_idx: dict[tuple[float, float], int] = {}
    lats: list[float] = []
    lons: list[float] = []
    for pts in pair_points:
        for lat, lon in pts:
            k = (round(lat, 4), round(lon, 4))
            if k not in key_to_idx:
                key_to_idx[k] = len(lats)
                lats.append(k[0])
                lons.append(k[1])

    # Fetch in batches; each batch is independent — a timeout on one does not abort the rest
    elev_map: dict[int, float] = {}
    for start in range(0, len(lats), _BATCH):
        try:
            lat_str = ",".join(f"{v:.4f}" for v in lats[start:start + _BATCH])
            lon_str = ",".join(f"{v:.4f}" for v in lons[start:start + _BATCH])
            resp = requests.get(
                f"{_URL}?latitude={lat_str}&longitude={lon_str}",
                timeout=30,
            )
            resp.raise_for_status()
            for i, elev in enumerate(resp.json().get("elevation", [])):
                elev_map[start + i] = float(elev)
        except Exception:
            pass  # this batch's points get None elevation; other batches continue
        if start + _BATCH < len(lats):
            time.sleep(_SLEEP)

    # Assign back to pairs
    for pair, pts in zip(pairs, pair_points):
        terrain: list[Optional[float]] = []
        for lat, lon in pts:
            k = (round(lat, 4), round(lon, 4))
            idx = key_to_idx.get(k)
            e = elev_map.get(idx) if idx is not None else None
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
