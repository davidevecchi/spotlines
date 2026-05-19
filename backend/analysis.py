"""Pair enumeration: distance filter + line-of-sight check."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from shapely.strtree import STRtree

from .geometry import EARTH_RADIUS, OsmRecord, check_los, haversine_m, sample_corridor, sample_terrain_types
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
    terrain_types: Optional[list] = None
    corridor_terrain: Optional[dict] = None
    corridor_features: Optional[list] = None


def _candidates_numpy(
    lats: np.ndarray, lons: np.ndarray, min_m: float, max_m: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (ii, jj, dists) for all upper-triangle pairs within [min_m, max_m]."""
    n = len(lats)
    if n < 2:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([])

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


def enumerate_pairs(
    anchors: list[Anchor],
    min_m: float,
    max_m: float,
    element_tree: STRtree,
    records: list,
    clearance_m: float = 0.0,
    terrain_tree=None,
    terrain_labels: list = None,
) -> list[Pair]:
    n = len(anchors)
    if n < 2:
        return []

    lats = np.array([a.lat for a in anchors])
    lons = np.array([a.lon for a in anchors])

    ii, jj, dists = _candidates_numpy(lats, lons, min_m, max_m)

    pairs: list[Pair] = []
    for k in range(len(ii)):
        a = anchors[int(ii[k])]
        b = anchors[int(jj[k])]
        dist = float(dists[k])

        ok, over_water = check_los(a, b, element_tree, records, clearance_m=clearance_m)
        if ok:
            ttypes = (
                sample_terrain_types(a, b, terrain_tree, terrain_labels)
                if terrain_tree is not None else None
            )
            corr_terrain, corr_features = (
                sample_corridor(a, b, clearance_m, element_tree, records, terrain_tree, terrain_labels)
                if terrain_tree is not None else ({}, [])
            )
            # A is always the westernmost anchor; tie-break: northernmost
            swapped = b.lon < a.lon or (b.lon == a.lon and b.lat > a.lat)
            if swapped:
                a, b = b, a
                if ttypes:
                    ttypes = ttypes[::-1]
                if corr_features:
                    corr_features = [
                        {**f, "t_start": round(1 - f["t_end"], 3), "t_end": round(1 - f["t_start"], 3)}
                        for f in corr_features
                    ]
            pairs.append(Pair(
                a, b, dist, over_water,
                terrain_types=ttypes,
                corridor_terrain=corr_terrain,
                corridor_features=corr_features,
            ))
    return pairs
