"""Overpass API fetch, caching, and OSM response parsing."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

import requests
from . import feature_map as _fm

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
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


def fetch_osm(south: float, west: float, north: float, east: float) -> dict:
    key = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    now = time.time()

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached["ts"] < _CACHE_TTL:
            return cached["data"]
        # Prune expired entries on each write to prevent unbounded growth
        for k in [k for k, v in _CACHE.items() if now - v["ts"] >= _CACHE_TTL]:
            del _CACHE[k]

    query = _build_query(south, west, north, east)
    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": "Spotlines/1.0"},
            timeout=90,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc

    data = resp.json()
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": time.time(), "data": data}
    return data


def _build_query(south: float, west: float, north: float, east: float) -> str:
    b = f"{south},{west},{north},{east}"
    # Build natural value filter from the feature map; always include tree.
    _nat_vals = sorted(_fm.DATA.get("natural", {}).keys())
    if "tree" not in _nat_vals:
        _nat_vals.insert(0, "tree")
    _nat_re = "|".join(_nat_vals)
    return f"""[out:json][timeout:60];
(
  node[natural=tree]({b});
  nwr[natural~"^({_nat_re})$"]({b});
  nwr[aerialway]({b});
  nwr[aeroway]({b});
  nwr[amenity]({b});
  nwr[barrier]({b});
  nwr[building]({b});
  nwr[craft]({b});
  nwr[emergency]({b});
  nwr[healthcare]({b});
  way[highway]({b});
  nwr[historic]({b});
  nwr[leisure]({b});
  nwr[man_made]({b});
  nwr[military]({b});
  nwr[office]({b});
  way[power~"^(line|minor_line|cable)$"]({b});
  nwr[public_transport]({b});
  way[railway]({b});
  nwr[shop]({b});
  way[telecom=line]({b});
  nwr[tourism]({b});
  nwr[wastewater]({b});
  nwr[waterway]({b});
);
out body; >; out skel qt;"""


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

    anchors = _parse_anchors(elements, nodes_by_id, ways_by_id)
    return anchors, elements, nodes_by_id, ways_by_id


def _parse_anchors(
    elements: list[dict],
    nodes_by_id: dict[int, dict],
    ways_by_id: dict[int, dict],
) -> list[Anchor]:
    """Extract anchor points from OSM elements.

    Currently only tree nodes are supported.  Planned future anchor types:
      - geological nodes (arch, arete, bare_rock, cave_entrance, cliff, rock, stone)
      - way-derived: tree_row, guard_rail, forest_edge (node per way-node)
      - relation-derived: forest_edge (outer-member way nodes)
    Each would require extending _build_query and adding a pass here.
    """
    anchors: list[Anchor] = []
    seen: set[int] = set()

    for el in elements:
        if el["type"] != "node":
            continue
        tags = el.get("tags") or {}
        nid = el["id"]
        if tags.get("natural") == "tree" and nid not in seen:
            anchors.append(Anchor(nid, el["lat"], el["lon"], tags, "tree"))
            seen.add(nid)

    return anchors
