"""Overpass API fetch, caching, and OSM response parsing."""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock

import requests
from . import feature_map as _fm

_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
_CACHE: dict = {}
_CACHE_LOCK = Lock()
_CACHE_TTL = 300  # seconds


@dataclass
class Anchor:
    id: int
    lat: float
    lon: float
    tags: dict
    kind: str  # tree  (guard_rail | tree_row | forest_edge | geological disabled)
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

    query = _build_query(south, west, north, east)
    last_exc: Exception | None = None
    for endpoint in _OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": "Spotlines/1.0"},
                timeout=120,
            )
            resp.raise_for_status()
            break
        except requests.RequestException as exc:
            last_exc = exc
            continue
    else:
        raise RuntimeError(str(last_exc)) from last_exc

    data = resp.json()
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}
    return data


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
