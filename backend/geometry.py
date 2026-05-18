"""Geometry helpers: haversine, Shapely geometry building, spatial indices, LOS check."""
from __future__ import annotations

import math
from typing import Optional

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from .overpass import Anchor

EARTH_RADIUS = 6_371_000  # metres

LANDUSE_ALLOWED = frozenset({
    "education", "fairground", "allotments", "farmland", "farmyard",
    "logging", "meadow", "orchard", "basin", "grass", "greenfield",
    "recreation_ground", "winter_sports", "forest",
})
LEISURE_ALLOWED = frozenset({"nature_reserve", "park", "garden", "summer_camp", "pitch", "dog_park"})
TOURISM_ALLOWED = frozenset({"camp_pitch", "camp_site", "caravan_site", "picnic_site"})
MAN_MADE_ALLOWED = frozenset({"cutline", "clearcut", "dyke", "embankment"})
RAILWAY_OK = frozenset({"abandoned", "disused"})
WATERWAY_BLOCKING = frozenset({"dock", "boatyard", "water_point", "fuel"})

# Terrain type classification for display purposes
_NATURAL_TERRAIN = {
    "wood": "forest", "tree_row": "forest",
    "water": "water", "wetland": "wetland",
    "grassland": "grassland", "heath": "grassland", "fell": "grassland",
    "scrub": "shrub",
    "bare_rock": "rock", "cliff": "rock", "arete": "rock", "arch": "rock",
    "stone": "rock", "rock": "rock", "cave_entrance": "rock",
    "sand": "sand", "beach": "sand",
    "glacier": "snow",
}
_LANDUSE_TERRAIN = {
    "forest": "forest", "wood": "forest", "logging": "forest",
    "meadow": "grassland", "grass": "grassland", "greenfield": "grassland",
    "recreation_ground": "grassland", "winter_sports": "grassland",
    "farmland": "farmland", "farmyard": "farmland", "orchard": "farmland",
    "allotments": "farmland", "vineyard": "farmland",
    "basin": "water", "reservoir": "water",
    "residential": "urban", "commercial": "urban", "industrial": "urban",
    "retail": "urban", "construction": "urban", "education": "urban",
    "fairground": "urban",
}

_POINT_BUF = 0.00002   # ~2 m in degrees
_SHRINK_M = 0.3        # endpoint shrink in metres


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(a))


def anchor_buffer_deg(anchor: Anchor) -> float:
    """Buffer radius in degrees derived from trunk circumference, minimum 0.5 m."""
    for key in ("circumference", "circumference:est"):
        val = anchor.tags.get(key)
        if val:
            try:
                radius_m = float(val) / (2 * math.pi)
                return max(radius_m, 0.5) / 111_111
            except ValueError:
                pass
    return 0.5 / 111_111


# ---------------------------------------------------------------------------
# Low-level geometry builders (all coords are Shapely order: lon, lat)
# ---------------------------------------------------------------------------

def _node_pt(el: dict) -> Optional[Point]:
    if "lat" in el and "lon" in el:
        return Point(el["lon"], el["lat"])
    return None


def _way_geom(way: dict, nodes_by_id: dict):
    nids = way.get("nodes", [])
    coords = [
        (nodes_by_id[n]["lon"], nodes_by_id[n]["lat"])
        for n in nids
        if n in nodes_by_id and "lat" in nodes_by_id[n]
    ]
    if len(coords) < 2:
        return None
    if coords[0] == coords[-1] and len(coords) >= 4:
        try:
            p = Polygon(coords)
            return make_valid(p) if not p.is_valid else p
        except Exception:
            pass
    return LineString(coords)


def _relation_geom(rel: dict, ways_by_id: dict, nodes_by_id: dict):
    geoms = []
    for member in rel.get("members", []):
        if member["type"] != "way":
            continue
        way = ways_by_id.get(member["ref"])
        if not way:
            continue
        g = _way_geom(way, nodes_by_id)
        if g is not None:
            geoms.append(g)
    if not geoms:
        return None
    result = unary_union(geoms)
    if not result.is_valid:
        result = make_valid(result)
    return result


def _element_geom(el: dict, etype: str, nodes_by_id: dict, ways_by_id: dict,
                  _cache: dict | None = None):
    if _cache is not None:
        eid = el["id"]
        if eid in _cache:
            return _cache[eid]
    if etype == "node":
        result = _node_pt(el)
    elif etype == "way":
        result = _way_geom(el, nodes_by_id)
    elif etype == "relation":
        result = _relation_geom(el, ways_by_id, nodes_by_id)
    else:
        result = None
    if _cache is not None:
        _cache[eid] = result
    return result


# ---------------------------------------------------------------------------
# Obstacle and water classification
# ---------------------------------------------------------------------------

def _classify_blocking(el: dict, etype: str, tags: dict,
                        nodes_by_id: dict, ways_by_id: dict, _cache: dict | None = None):
    """Return a blocking Shapely geometry or None."""

    def geom():
        return _element_geom(el, etype, nodes_by_id, ways_by_id, _cache)

    def node_buf():
        pt = _node_pt(el)
        return pt.buffer(_POINT_BUF) if pt else None

    def poly_or_line():
        return geom()

    if tags.get("aeroway"):
        return node_buf() if etype == "node" else geom()

    if tags.get("amenity"):
        return node_buf() if etype == "node" else poly_or_line()

    if "barrier" in tags:
        if tags["barrier"] == "kerb":
            return None
        return node_buf() if etype == "node" else geom()

    if tags.get("building"):
        return None if etype == "node" else poly_or_line()

    if tags.get("craft"):
        return None if etype == "node" else poly_or_line()

    if tags.get("emergency"):
        return node_buf() if etype == "node" else poly_or_line()

    if tags.get("healthcare"):
        return None if etype == "node" else poly_or_line()

    if tags.get("highway"):
        return geom() if etype == "way" else None

    if tags.get("historic"):
        return node_buf() if etype == "node" else poly_or_line()

    if "landuse" in tags:
        return None if tags["landuse"] in LANDUSE_ALLOWED else poly_or_line()

    if "leisure" in tags:
        return None if tags["leisure"] in LEISURE_ALLOWED else poly_or_line()

    if "man_made" in tags:
        return None if tags["man_made"] in MAN_MADE_ALLOWED else poly_or_line()

    if tags.get("military"):
        return None if etype == "node" else poly_or_line()

    if tags.get("office"):
        return None if etype == "node" else poly_or_line()

    if tags.get("power") in {"line", "minor_line", "cable"}:
        return geom() if etype == "way" else None

    if tags.get("public_transport"):
        return node_buf() if etype == "node" else poly_or_line()

    if "railway" in tags:
        if tags["railway"] in RAILWAY_OK:
            return None
        return geom() if etype == "way" else None

    if tags.get("shop"):
        return node_buf() if etype == "node" else poly_or_line()

    if tags.get("telecom") == "line":
        return geom() if etype == "way" else None

    if "tourism" in tags:
        return None if tags["tourism"] in TOURISM_ALLOWED else poly_or_line()

    if tags.get("wastewater"):
        return None if etype == "node" else poly_or_line()

    if "waterway" in tags and tags["waterway"] in WATERWAY_BLOCKING:
        return node_buf() if etype == "node" else geom()

    return None


def _classify_water(el: dict, etype: str, tags: dict,
                    nodes_by_id: dict, ways_by_id: dict, _cache: dict | None = None):
    """Return a water Shapely geometry or None."""
    ww = tags.get("waterway")
    if ww and ww not in WATERWAY_BLOCKING:
        return _element_geom(el, etype, nodes_by_id, ways_by_id, _cache)

    if tags.get("natural") == "water":
        return _element_geom(el, etype, nodes_by_id, ways_by_id, _cache)

    return None


# ---------------------------------------------------------------------------
# Spatial index construction
# ---------------------------------------------------------------------------

def build_spatial_indices(
    elements: list[dict],
    nodes_by_id: dict,
    ways_by_id: dict,
    anchors: list[Anchor],
    geom_cache: dict | None = None,
) -> tuple:
    """
    Returns (blocking_tree, blocking_geoms, blocking_anchor_ids,
             water_tree, water_geoms,
             anchor_tree, anchor_geoms, anchor_ids).

    blocking_anchor_ids[i] is the anchor ID if blocking_geoms[i] is a tree
    trunk buffer, else None.  check_los uses this to skip the two endpoints.
    """
    blocking_geoms: list = []
    blocking_anchor_ids: list = []   # int | None, parallel to blocking_geoms
    water_geoms: list = []

    for el in elements:
        tags = el.get("tags") or {}
        if not tags:
            continue
        etype = el["type"]

        bg = _classify_blocking(el, etype, tags, nodes_by_id, ways_by_id, geom_cache)
        if bg is not None and not bg.is_empty:
            blocking_geoms.append(bg)
            blocking_anchor_ids.append(None)

        wg = _classify_water(el, etype, tags, nodes_by_id, ways_by_id, geom_cache)
        if wg is not None and not wg.is_empty:
            water_geoms.append(wg)

    # Add every tree anchor as a blocking trunk buffer so the clearance radius
    # applies to them too.  Endpoint exclusion is handled in check_los.
    anchor_geoms: list = []
    anchor_ids: list[int] = []
    for a in anchors:
        buf = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
        anchor_geoms.append(buf)
        anchor_ids.append(a.id)
        blocking_geoms.append(buf)
        blocking_anchor_ids.append(a.id)

    blocking_tree = STRtree(blocking_geoms)
    water_tree = STRtree(water_geoms)
    anchor_tree = STRtree(anchor_geoms)

    return (
        blocking_tree, blocking_geoms, blocking_anchor_ids,
        water_tree, water_geoms,
        anchor_tree, anchor_geoms, anchor_ids,
    )


# ---------------------------------------------------------------------------
# Line-of-sight
# ---------------------------------------------------------------------------

def _shrink_line(lon1: float, lat1: float, lon2: float, lat2: float) -> LineString:
    """Shrink line by _SHRINK_M from each endpoint."""
    dist_m = haversine_m(lat1, lon1, lat2, lon2)
    if dist_m < 2 * _SHRINK_M + 0.1:
        return LineString([(lon1, lat1), (lon2, lat2)])
    frac = _SHRINK_M / dist_m
    nlon1 = lon1 + (lon2 - lon1) * frac
    nlat1 = lat1 + (lat2 - lat1) * frac
    nlon2 = lon2 - (lon2 - lon1) * frac
    nlat2 = lat2 - (lat2 - lat1) * frac
    return LineString([(nlon1, nlat1), (nlon2, nlat2)])


def _shrink_line_frac(lon1: float, lat1: float, lon2: float, lat2: float,
                      frac: float) -> LineString:
    """Shrink line by `frac` of its length from each endpoint."""
    nlon1 = lon1 + (lon2 - lon1) * frac
    nlat1 = lat1 + (lat2 - lat1) * frac
    nlon2 = lon2 - (lon2 - lon1) * frac
    nlat2 = lat2 - (lat2 - lat1) * frac
    return LineString([(nlon1, nlat1), (nlon2, nlat2)])


def build_terrain_index(
    elements: list[dict],
    nodes_by_id: dict,
    ways_by_id: dict,
    geom_cache: dict | None = None,
) -> tuple:
    """Return (terrain_tree, terrain_geoms, terrain_type_labels)."""
    terrain_geoms: list = []
    terrain_labels: list[str] = []

    for el in elements:
        tags = el.get("tags") or {}
        if not tags:
            continue
        etype = el["type"]

        label = None
        natural = tags.get("natural")
        landuse = tags.get("landuse")
        waterway = tags.get("waterway")

        if natural and natural in _NATURAL_TERRAIN:
            label = _NATURAL_TERRAIN[natural]
        elif landuse and landuse in _LANDUSE_TERRAIN:
            label = _LANDUSE_TERRAIN[landuse]
        elif waterway and waterway not in WATERWAY_BLOCKING:
            label = "water"

        if label:
            geom = _element_geom(el, etype, nodes_by_id, ways_by_id, geom_cache)
            if geom is not None and not geom.is_empty:
                terrain_geoms.append(geom)
                terrain_labels.append(label)

    return STRtree(terrain_geoms), terrain_geoms, terrain_labels


def sample_terrain_types(
    a: Anchor, b: Anchor,
    terrain_tree: STRtree,
    terrain_labels: list[str],
    n: int = 10,
) -> list[str]:
    result = []
    for i in range(n):
        t = i / (n - 1)
        lon = a.lon + t * (b.lon - a.lon)
        lat = a.lat + t * (b.lat - a.lat)
        pt = Point(lon, lat)
        hits = terrain_tree.query(pt, predicate="intersects")
        result.append(terrain_labels[hits[-1]] if len(hits) else "unknown")
    return result


def check_los(
    a: Anchor, b: Anchor,
    blocking_tree: STRtree, blocking_geoms: list, blocking_anchor_ids: list,
    water_tree: STRtree, water_geoms: list,
    clearance_m: float = 0.0,
) -> tuple[bool, bool]:
    """Return (passes, over_water)."""
    shrunk = _shrink_line(a.lon, a.lat, b.lon, b.lat)
    full = LineString([(a.lon, a.lat), (b.lon, b.lat)])
    endpoints = (a.id, b.id)

    # Basic LOS: reject if anything crosses the narrow line (0.3 m endpoint shrink)
    for idx in blocking_tree.query(shrunk, predicate="intersects"):
        if blocking_anchor_ids[idx] not in endpoints:
            return False, False

    # Clearance corridor: only the middle 80% of the line (10% excluded at each end)
    # so obstacles right at the anchor attachment points don't disqualify the pair.
    if clearance_m > 0:
        mid = _shrink_line_frac(a.lon, a.lat, b.lon, b.lat, 0.1)
        corridor = mid.buffer(clearance_m / 111_111, cap_style=2)
        for idx in blocking_tree.query(corridor, predicate="intersects"):
            if blocking_anchor_ids[idx] not in endpoints:
                return False, False

    over_water = len(water_tree.query(full, predicate="intersects")) > 0
    return True, over_water
