"""Overpass API fetch, caching, and OSM response parsing."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

import requests
from . import feature_map as _fm
from . import osm_local
from .dem import CACHE_ROOT

# Mutable order: a mirror that fails is moved to the back, so dead ones stop costing a timeout.
_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
_CACHE: dict = {}
_CACHE_LOCK = Lock()
_CACHE_TTL = 300  # seconds, merged result per bbox

# OSM is cached on disk in fixed tiles (like the DEM), so any rectangle reuses what's already fetched.
_TILE_DEG = 0.025         # ~2.8 x 1.9 km: stays well under Overpass' 90 s limit even in towns
_TILE_TTL = 30 * 86400
_TILE_WORKERS = 2         # overpass-api.de allows 2 concurrent slots per IP
_RETRY_WAIT_S = 10
# Key hash in the path: editing feature_map's key set invalidates old tiles.
_TILE_DIR = CACHE_ROOT / "osm" / hashlib.sha1(",".join(sorted(_fm.ALL_KEYS)).encode()).hexdigest()[:8]


@dataclass
class Anchor:
    id: int
    lat: float
    lon: float
    tags: dict
    kind: str
    terrain: str = ""


def fetch_osm(south: float, west: float, north: float, east: float) -> dict:
    key = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    now = time.time()

    with _CACHE_LOCK:
        # Prune expired entries on every call (hit or miss) to prevent unbounded growth
        for k in [k for k, v in _CACHE.items() if now - v["ts"] >= _CACHE_TTL]:
            del _CACHE[k]
        cached = _CACHE.get(key)
        if cached and now - cached["ts"] < _CACHE_TTL:
            return cached["data"]

    if osm_local.covers(south, west, north, east):
        data = {"elements": osm_local.extract(south, west, north, east), "bbox": (south, west, north, east)}
        with _CACHE_LOCK:
            _CACHE[key] = {"ts": time.time(), "data": data}
        return data

    # Outside the local extract: Overpass, cached in tiles.
    def idx(deg: float) -> int:  # epsilon: 46.05 / 0.025 is 1841.999… in floats, which adds a tile row
        return math.floor(deg / _TILE_DEG + 1e-9)
    tiles = [(i, j)
             for i in range(idx(south), idx(north) + 1)
             for j in range(idx(west), idx(east) + 1)]
    with ThreadPoolExecutor(_TILE_WORKERS) as pool:
        parts = list(pool.map(_load_tile, tiles))

    # Tiles overlap on ways/relations crossing their edges: dedupe, preferring the tagged copy
    # (an element can arrive as a bare `out skel` member in one tile and with tags in another).
    merged: dict[tuple[str, int], dict] = {}
    for elements in parts:
        for el in elements:
            k = (el["type"], el["id"])
            if k not in merged or (el.get("tags") and not merged[k].get("tags")):
                merged[k] = el
    data = {"elements": list(merged.values()), "bbox": (south, west, north, east)}
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}
    return data


def _load_tile(tile: tuple[int, int]) -> list[dict]:
    path = _TILE_DIR / f"{tile[0]}_{tile[1]}.json.gz"
    if path.exists() and time.time() - path.stat().st_mtime < _TILE_TTL:
        return json.loads(gzip.decompress(path.read_bytes()))["elements"]

    s, w = round(tile[0] * _TILE_DEG, 6), round(tile[1] * _TILE_DEG, 6)
    n, e = round(s + _TILE_DEG, 6), round(w + _TILE_DEG, 6)
    query = _build_query(s, w, n, e)
    last_exc: Exception | None = None
    # Two passes over the mirrors; public servers often 429/504 under load and recover in seconds.
    for attempt, endpoint in enumerate(list(_OVERPASS_ENDPOINTS) * 2):
        if attempt == len(_OVERPASS_ENDPOINTS):
            time.sleep(_RETRY_WAIT_S)
        try:
            resp = requests.post(endpoint, data={"data": query},
                                 headers={"User-Agent": "Spotlines/1.0"}, timeout=100)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data.get("remark", ""):  # 200 with partial data on server timeout
                raise RuntimeError(data["remark"])
            break
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            print(f"Overpass tile {tile} failed on {endpoint}: {exc}", flush=True)
            with _CACHE_LOCK:
                if endpoint in _OVERPASS_ENDPOINTS:
                    _OVERPASS_ENDPOINTS.remove(endpoint)
                    _OVERPASS_ENDPOINTS.append(endpoint)
    else:
        # Tiles already saved stay cached, so a retry only refetches what's missing.
        raise RuntimeError(f"Overpass failed for OSM tile {tile}: {last_exc}") from last_exc

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(gzip.compress(resp.content, compresslevel=3))
    tmp.replace(path)
    return data["elements"]


def _build_query(south: float, west: float, north: float, east: float) -> str:
    b = f"{south},{west},{north},{east}"
    parts = [f"  nwr[{key}]({b});" for key in sorted(_fm.ALL_KEYS)]
    return "[out:json][timeout:90];\n(\n" + "\n".join(parts) + "\n);\nout body; >; out skel qt;"


def parse_osm(
    data: dict,
) -> tuple[list[Anchor], list[dict], dict[int, dict], dict[int, dict]]:
    """Return (anchors, all_elements, nodes_by_id, ways_by_id)."""
    elements: list[dict] = data.get("elements", [])

    nodes_by_id: dict[int, dict] = {}
    ways_by_id: dict[int, dict] = {}

    for el in elements:
        etype = el["type"]
        eid = el["id"]
        if etype == "node":
            # Prefer tagged entries over skeleton entries
            existing = nodes_by_id.get(eid)
            if existing is None or (not existing.get("tags") and el.get("tags")):
                nodes_by_id[eid] = el
        elif etype == "way":
            ways_by_id[eid] = el

    anchors = _parse_anchors(elements)
    if bbox := data.get("bbox"):  # tiles extend past the request; keep anchors inside it
        s, w, n, e = bbox
        anchors = [a for a in anchors if s <= a.lat <= n and w <= a.lon <= e]
    return anchors, elements, nodes_by_id, ways_by_id


def _parse_anchors(
    elements: list[dict],
) -> list[Anchor]:
    """Extract anchor points from OSM elements based on feature_map anchor flag."""
    anchors: list[Anchor] = []
    seen: set[int] = set()

    for el in elements:
        if el["type"] != "node":
            continue
        tags = el.get("tags") or {}
        nid = el["id"]
        if nid in seen:
            continue
        for key, val in tags.items():
            if _fm.props(key, val).get("anchor"):
                anchors.append(Anchor(nid, el["lat"], el["lon"], tags, val))
                seen.add(nid)
                break

    return anchors
