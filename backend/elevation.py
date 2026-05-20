"""Elevation sampling: local DEM cache with Open-Meteo API fallback."""
from __future__ import annotations

import math
import time
from typing import Optional

import requests

from .analysis import Pair
from .dem import dem_cache_available, get_elevation_at_points

_SAMPLES = 10

_OM_URL   = "https://api.open-meteo.com/v1/elevation"
_OM_BATCH = 100
_OM_SLEEP = 0.15


def fetch_elevations(pairs: list[Pair]) -> None:
    """Sample terrain elevations and mutate pairs in-place.

    Tries local DEM tiles first; falls back to Open-Meteo API when the DEM
    cache is unavailable (drive not mounted) or returns all-None results.
    """
    if not pairs:
        return

    pair_points: list[list[tuple[float, float]]] = [
        _interpolate(p.anchor_a.lat, p.anchor_a.lon,
                     p.anchor_b.lat, p.anchor_b.lon, _SAMPLES)
        for p in pairs
    ]

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

    elevs = _fetch_dem(lats, lons)

    # Supplement with Open-Meteo for any point the DEM couldn't cover
    # (absent drive, missing tile, or partial tile-boundary coverage).
    missing = [i for i, e in enumerate(elevs) if e is None]
    if missing:
        om = _fetch_open_meteo([lats[i] for i in missing], [lons[i] for i in missing])
        for idx, e in zip(missing, om):
            if e is not None:
                elevs[idx] = e

    _assign(pairs, pair_points, key_to_idx, elevs)


def _fetch_dem(lats: list[float], lons: list[float]) -> list[Optional[float]]:
    if not dem_cache_available():
        return [None] * len(lats)
    return get_elevation_at_points(lats, lons)


def _fetch_open_meteo(lats: list[float], lons: list[float]) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(lats)
    for start in range(0, len(lats), _OM_BATCH):
        end = min(start + _OM_BATCH, len(lats))
        lat_str = ",".join(f"{v:.5f}" for v in lats[start:end])
        lon_str = ",".join(f"{v:.5f}" for v in lons[start:end])
        for attempt in range(3):
            try:
                resp = requests.get(
                    f"{_OM_URL}?latitude={lat_str}&longitude={lon_str}", timeout=30
                )
                resp.raise_for_status()
                for i, elev in enumerate(resp.json().get("elevation", [])):
                    result[start + i] = float(elev)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(0.5)
        if end < len(lats):
            time.sleep(_OM_SLEEP)
    return result


def _assign(
    pairs: list[Pair],
    pair_points: list[list[tuple[float, float]]],
    key_to_idx: dict[tuple[float, float], int],
    elevs: list[Optional[float]],
) -> None:
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
