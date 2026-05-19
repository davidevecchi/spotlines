"""Overpass API fetch, caching, and OSM response parsing."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_CACHE: dict = {}
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
    _CACHE[key] = {"ts": now, "data": data}
    return data


def _build_query(south: float, west: float, north: float, east: float) -> str:
    b = f"{south},{west},{north},{east}"
    # FUTURE: enable additional anchor sources by uncommenting:
    #   node[natural~"^(arch|arete|bare_rock|cave_entrance|cliff|rock|stone)$"]({b});  # geological
    #   nwr[natural~"^(wood|tree_row)$"]({b});                                          # tree_row
    #   nwr[landuse=forest]({b});                                                       # forest_edge
    #   way[barrier=guard_rail]({b});                                                   # guard_rail
    return f"""[out:json][timeout:60];
(
  node[natural=tree]({b});
  nwr[natural~"^(wood|tree_row|water)$"]({b});
  nwr[aeroway]({b});
  nwr[amenity]({b});
  nwr[barrier]({b});
  nwr[building]({b});
  nwr[craft]({b});
  nwr[emergency]({b});
  nwr[healthcare]({b});
  way[highway]({b});
  nwr[historic]({b});
  nwr[landuse]({b});
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
    anchors: list[Anchor] = []
    seen: set[int] = set()

    # Pass 1: tree nodes only
    for el in elements:
        if el["type"] != "node":
            continue
        tags = el.get("tags") or {}
        nat = tags.get("natural", "")
        nid = el["id"]
        if nat == "tree" and nid not in seen:
            anchors.append(Anchor(nid, el["lat"], el["lon"], tags, "tree"))
            seen.add(nid)
        # FUTURE: geological — elif nat in {"arch","arete","bare_rock","cave_entrance","cliff","rock","stone"} and nid not in seen: ...

    # FUTURE: Pass 2 — way-derived anchors (tree_row, guard_rail, forest_edge)
    # Each: iterate elements for the matching tag, then iterate el["nodes"] to emit one Anchor per node.
    # Kinds: "tree_row", "guard_rail", "forest_edge"

    # FUTURE: Pass 3 — relation-derived forest_edge anchors
    # Iterate relation members of role "outer"/"", collect their way nodes as forest_edge anchors.

    return anchors
