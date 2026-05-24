"""Pair enumeration: distance filter + line-of-sight check."""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

import numpy as np
from shapely.strtree import STRtree

from .geometry import EARTH_RADIUS, OsmRecord, check_los, get_corridor_features, haversine_m
from .overpass import Anchor


@dataclass
class Pair:
    anchor_a: Anchor
    anchor_b: Anchor
    distance_m: float
    over_water: bool
    elev_a: Optional[float] = None
    elev_b: Optional[float] = None
    slope_pct: Optional[float] = None
    slope_deg: Optional[float] = None
    terrain_elevs: Optional[list] = None
    corridor_features: Optional[list] = None


def _candidates_numpy(
    lats: np.ndarray, lons: np.ndarray, min_m: float, max_m: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (ii, jj, dists) for all upper-triangle pairs within [min_m, max_m]."""
    n = len(lats)
    if n < 2:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([])

    # mean-lat approximation: valid for the ≤0.1° bbox enforced by the API
    mean_lat_rad = math.radians(float(np.mean(lats)))
    max_dlat = max_m / 111_111
    max_dlon = max_m / (111_111 * max(math.cos(mean_lat_rad), 0.001))

    CHUNK = 500
    ii_out, jj_out, dist_out = [], [], []

    for start in range(0, n, CHUNK):
        end = min(start + CHUNK, n)
        row_lats = lats[start:end, np.newaxis]
        row_lons = lons[start:end, np.newaxis]
        for col_start in range(0, n, CHUNK):
            col_end = min(col_start + CHUNK, n)
            col_lats = lats[np.newaxis, col_start:col_end]
            col_lons = lons[np.newaxis, col_start:col_end]

            dlat = np.abs(row_lats - col_lats)
            dlon = np.abs(row_lons - col_lons)
            bbox = (dlat <= max_dlat) & (dlon <= max_dlon)

            rows_g = np.arange(start, end)[:, np.newaxis]
            cols_g = np.arange(col_start, col_end)[np.newaxis, :]
            upper = cols_g > rows_g

            mask = bbox & upper
            ri, ci = np.where(mask)
            if ri.size == 0:
                continue

            gi = rows_g.ravel()[ri]
            gj = cols_g.ravel()[ci]

            lat1 = np.radians(lats[gi])
            lat2 = np.radians(lats[gj])
            dlt = np.radians(lats[gj] - lats[gi])
            dln = np.radians(lons[gj] - lons[gi])
            a = np.sin(dlt / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dln / 2) ** 2
            dists = 2 * EARTH_RADIUS * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

            range_mask = (dists >= min_m) & (dists <= max_m)
            if not np.any(range_mask):
                continue

            ii_out.append(gi[range_mask])
            jj_out.append(gj[range_mask])
            dist_out.append(dists[range_mask])

    if not ii_out:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([])

    return np.concatenate(ii_out), np.concatenate(jj_out), np.concatenate(dist_out)


def get_distance_candidates(
    anchors: list[Anchor],
    min_m: float,
    max_m: float,
) -> list[Pair]:
    """Return all anchor pairs within [min_m, max_m] with no geometry checks."""
    n = len(anchors)
    if n < 2:
        return []

    lats = np.array([a.lat for a in anchors])
    lons = np.array([a.lon for a in anchors])

    ii, jj, dists = _candidates_numpy(lats, lons, min_m, max_m)
    if len(ii) == 0:
        return []

    pairs = []
    for k in range(len(ii)):
        a = anchors[int(ii[k])]
        b = anchors[int(jj[k])]
        dist = float(dists[k])
        if b.lon < a.lon or (b.lon == a.lon and b.lat > a.lat):
            a, b = b, a
        pairs.append(Pair(a, b, dist, False))
    return pairs


def apply_los_buffer(
    pairs: list[Pair],
    element_tree: STRtree,
    records: list,
    clearance_m: float = 0.0,
) -> list[Pair]:
    """Apply LOS + buffer check to a list of distance-valid pairs.

    Mutates each surviving pair's over_water field in-place.
    Runs in parallel across up to 4 workers (one contiguous slice each).
    """
    n_pairs = len(pairs)
    if n_pairs == 0:
        return []

    n_workers = min(os.cpu_count() or 1, 4)
    chunk_size = max(1, (n_pairs + n_workers - 1) // n_workers)

    def _check_slice(start: int, end: int) -> list[Optional[Pair]]:
        out = []
        for p in pairs[start:end]:
            ok, over_water = check_los(p.anchor_a, p.anchor_b, element_tree, records, clearance_m=clearance_m)
            if ok:
                p.over_water = over_water
                out.append(p)
            else:
                out.append(None)
        return out

    slices = [(i, min(i + chunk_size, n_pairs)) for i in range(0, n_pairs, chunk_size)]
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        chunk_results = pool.map(lambda s: _check_slice(*s), slices)

    return [p for chunk in chunk_results for p in chunk if p is not None]


def enumerate_pairs(
    anchors: list[Anchor],
    min_m: float,
    max_m: float,
    element_tree: STRtree,
    records: list,
    clearance_m: float = 0.0,
    mid_lat: float = 45.0,
) -> list[Pair]:
    pairs = get_distance_candidates(anchors, min_m, max_m)
    return apply_los_buffer(pairs, element_tree, records, clearance_m)



def compute_corridor_landuse(
    pairs: list[Pair],
    raster,
    south: float, west: float, north: float, east: float,
    clearance_m: float = 0.0,
) -> None:
    """Append raster-based landuse features to each pair's corridor_features."""
    from .landuse import sample_landuse_along_line
    for p in pairs:
        lu = sample_landuse_along_line(
            p.anchor_a.lat, p.anchor_a.lon,
            p.anchor_b.lat, p.anchor_b.lon,
            south, west, north, east,
            img=raster,
            clearance_m=clearance_m,
        )
        if lu:
            p.corridor_features = (p.corridor_features or []) + lu


def compute_corridor_features(
    pairs: list[Pair],
    element_tree: STRtree,
    records: list,
    clearance_m: float,
    mid_lat: float,
) -> None:
    """Populate corridor_features on each pair in parallel (called after slope filtering).

    STRtree is read-only and thread-safe in Shapely 2.x.
    """
    if not pairs:
        return

    n_workers = min(os.cpu_count() or 1, 4)

    def _one(p: Pair):
        return get_corridor_features(p.anchor_a, p.anchor_b, clearance_m, element_tree, records, mid_lat)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for p, features in zip(pairs, pool.map(_one, pairs)):
            p.corridor_features = features


def filter_anchors_by_terrain(
    anchors: list,
    raster,
    south: float, west: float, north: float, east: float,
) -> list:
    """Drop anchors whose position falls in a blocking landuse zone.

    Accepts a pre-fetched PIL RGBA image so no additional HTTP requests are made.
    """
    from .landuse import _nearest_type, _ALLOWED
    if raster is None:
        return anchors
    pixels = raster.load()
    img_w, img_h = raster.size
    bbox_w = east - west
    bbox_h = north - south
    kept = []
    for a in anchors:
        px = max(0, min(img_w - 1, int((a.lon - west) / bbox_w * img_w)))
        py = max(0, min(img_h - 1, int((north - a.lat) / bbox_h * img_h)))
        r, g, b, alpha = pixels[px, py]
        lu = _nearest_type(r, g, b) if alpha >= 128 else ""
        a.terrain = lu
        if not lu or lu in _ALLOWED:
            kept.append(a)
    return kept


def filter_landuse_blockers(pairs: list[Pair]) -> list[Pair]:
    """Drop pairs whose centerline crosses a blocking landuse zone."""
    return [
        p for p in pairs
        if not any(
            f["is_blocker"] and f["segments"] and f["category"] == "osmlanduse"
            for f in (p.corridor_features or [])
        )
    ]
