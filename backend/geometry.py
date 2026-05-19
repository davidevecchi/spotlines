"""Geometry helpers: haversine, Shapely geometry building, spatial indices, LOS check."""
from __future__ import annotations

import math
from dataclasses import dataclass
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
_LEISURE_TERRAIN = {
    "park": "grassland", "garden": "grassland", "nature_reserve": "grassland",
    "village_green": "grassland", "common": "grassland", "golf_course": "grassland",
    "meadow": "grassland",
}
# Higher priority wins when multiple terrain polygons overlap the same point.
_TERRAIN_PRIORITY = {
    "water": 0, "wetland": 1, "snow": 2, "rock": 3,
    "forest": 4, "shrub": 5, "farmland": 6, "sand": 7,
    "urban": 8, "grassland": 9, "unknown": 99,
}

_POINT_BUF = 0.00002   # ~2 m in degrees


# ---------------------------------------------------------------------------
# Unified OSM element record
# ---------------------------------------------------------------------------

@dataclass
class OsmRecord:
    """One geometry emitted during the unified classification pass.

    is_blocker and is_water are mutually exclusive.
    anchor_id is non-None only for synthetic anchor trunk buffers.
    label/category are populated for ways/relations that have a human-readable
    identity (roads, waterways, railways, buildings, etc.).
    """
    geom: object
    is_blocker: bool
    is_water: bool
    anchor_id: Optional[int]
    terrain_type: Optional[str]
    label: Optional[str]
    category: Optional[str]


# ---------------------------------------------------------------------------
# Low-level geometry builders (all coords are Shapely order: lon, lat)
# ---------------------------------------------------------------------------

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
# Element classification helpers
# ---------------------------------------------------------------------------

def _classify_blocking(el: dict, etype: str, tags: dict,
                        nodes_by_id: dict, ways_by_id: dict, _cache: dict | None = None):
    """Return a blocking Shapely geometry or None."""

    def geom():
        return _element_geom(el, etype, nodes_by_id, ways_by_id, _cache)

    def node_buf():
        pt = _node_pt(el)
        return pt.buffer(_POINT_BUF) if pt else None

    if tags.get("aeroway"):
        return node_buf() if etype == "node" else geom()

    if tags.get("amenity"):
        return node_buf() if etype == "node" else geom()

    if "barrier" in tags:
        if tags["barrier"] == "kerb":
            return None
        return node_buf() if etype == "node" else geom()

    if tags.get("building"):
        return None if etype == "node" else geom()

    if tags.get("craft"):
        return None if etype == "node" else geom()

    if tags.get("emergency"):
        return node_buf() if etype == "node" else geom()

    if tags.get("healthcare"):
        return None if etype == "node" else geom()

    if tags.get("highway"):
        return geom() if etype == "way" else None

    if tags.get("historic"):
        return node_buf() if etype == "node" else geom()

    if "landuse" in tags:
        return None if tags["landuse"] in LANDUSE_ALLOWED else geom()

    if "leisure" in tags:
        return None if tags["leisure"] in LEISURE_ALLOWED else geom()

    if "man_made" in tags:
        return None if tags["man_made"] in MAN_MADE_ALLOWED else geom()

    if tags.get("military"):
        return None if etype == "node" else geom()

    if tags.get("office"):
        return None if etype == "node" else geom()

    if tags.get("power") in {"line", "minor_line", "cable"}:
        return geom() if etype == "way" else None

    if tags.get("public_transport"):
        return node_buf() if etype == "node" else geom()

    if "railway" in tags:
        if tags["railway"] in RAILWAY_OK:
            return None
        return geom() if etype == "way" else None

    if tags.get("shop"):
        return node_buf() if etype == "node" else geom()

    if tags.get("telecom") == "line":
        return geom() if etype == "way" else None

    if "tourism" in tags:
        return None if tags["tourism"] in TOURISM_ALLOWED else geom()

    if tags.get("wastewater"):
        return None if etype == "node" else geom()

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


def _classify_terrain_type(tags: dict, etype: str) -> Optional[str]:
    """Return terrain type string or None."""
    natural = tags.get("natural")
    landuse = tags.get("landuse")
    waterway = tags.get("waterway")
    leisure = tags.get("leisure")

    if natural and natural in _NATURAL_TERRAIN:
        return _NATURAL_TERRAIN[natural]
    if landuse and landuse in _LANDUSE_TERRAIN:
        return _LANDUSE_TERRAIN[landuse]
    if waterway and waterway not in WATERWAY_BLOCKING:
        return "water"
    if leisure and leisure in _LEISURE_TERRAIN:
        return _LEISURE_TERRAIN[leisure]
    if tags.get("building") and etype != "node":
        return "urban"
    return None


def _format_tag(val: str) -> str:
    return val.replace("_", " ").capitalize()


def _feature_label_category(tags: dict) -> tuple:
    """Return (label, category) for corridor display, or (None, None) if not relevant."""
    name = tags.get("name")

    hw = tags.get("highway")
    if hw:
        return (name or _format_tag(hw)), "highway"

    ww = tags.get("waterway")
    if ww and ww not in WATERWAY_BLOCKING:
        return (name or _format_tag(ww)), "waterway"

    if tags.get("natural") == "water":
        return (name or "Water"), "water"

    rw = tags.get("railway")
    if rw and rw not in RAILWAY_OK:
        return (name or _format_tag(rw)), "railway"

    pw = tags.get("power")
    if pw in {"line", "minor_line", "cable"}:
        return (name or (_format_tag(pw) + " line")), "power"

    if tags.get("telecom") == "line":
        return (name or "Telecom line"), "telecom"

    lu = tags.get("landuse")
    if lu and lu not in LANDUSE_ALLOWED:
        return (name or _format_tag(lu)), "landuse"

    ls = tags.get("leisure")
    if ls and ls not in LEISURE_ALLOWED:
        return (name or _format_tag(ls)), "leisure"

    bld = tags.get("building")
    if bld:
        label = name or ("Building" if bld in ("yes", "true", "1") else _format_tag(bld))
        return label, "building"

    return None, None


# ---------------------------------------------------------------------------
# Unified spatial index
# ---------------------------------------------------------------------------

def build_all_indices(
    elements: list[dict],
    nodes_by_id: dict,
    ways_by_id: dict,
    anchors: list[Anchor],
    geom_cache: dict | None = None,
) -> tuple:
    """Single classification pass over all OSM elements.

    Returns:
        element_tree  : STRtree over all OsmRecord geometries
        records       : list[OsmRecord], indexed parallel to element_tree
        terrain_tree  : STRtree over terrain-typed geometries only
        terrain_geoms : list of geometries (parallel to terrain_labels)
        terrain_labels: list[str] parallel to terrain_geoms
        anchor_tree   : STRtree over anchor trunk buffers
        anchor_geoms  : list of Shapely geometries
        anchor_ids    : list[int] parallel to anchor_geoms
    """
    records: list[OsmRecord] = []
    terrain_geoms: list = []
    terrain_labels: list[str] = []

    for el in elements:
        tags = el.get("tags") or {}
        if not tags:
            continue
        etype = el["type"]

        bg = _classify_blocking(el, etype, tags, nodes_by_id, ways_by_id, geom_cache)
        wg = _classify_water(el, etype, tags, nodes_by_id, ways_by_id, geom_cache)
        terrain_type = _classify_terrain_type(tags, etype)
        label, category = _feature_label_category(tags) if etype != "node" else (None, None)

        if bg is not None and not bg.is_empty:
            records.append(OsmRecord(bg, True, False, None, terrain_type, label, category))
            if terrain_type:
                terrain_geoms.append(bg)
                terrain_labels.append(terrain_type)
        elif wg is not None and not wg.is_empty:
            records.append(OsmRecord(wg, False, True, None, terrain_type, label, category))
            if terrain_type:
                terrain_geoms.append(wg)
                terrain_labels.append(terrain_type)
        elif terrain_type is not None or label is not None:
            geom = _element_geom(el, etype, nodes_by_id, ways_by_id, geom_cache)
            if geom is not None and not geom.is_empty:
                records.append(OsmRecord(geom, False, False, None, terrain_type, label, category))
                if terrain_type:
                    terrain_geoms.append(geom)
                    terrain_labels.append(terrain_type)

    # Anchor trunk buffers — synthetic blockers, no terrain/label
    anchor_geoms: list = []
    anchor_ids: list[int] = []
    for a in anchors:
        buf = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
        anchor_geoms.append(buf)
        anchor_ids.append(a.id)
        records.append(OsmRecord(buf, True, False, a.id, None, None, None))

    element_tree = STRtree([r.geom for r in records])
    terrain_tree = STRtree(terrain_geoms) if terrain_geoms else STRtree([])
    anchor_tree = STRtree(anchor_geoms) if anchor_geoms else STRtree([])

    return (
        element_tree, records,
        terrain_tree, terrain_geoms, terrain_labels,
        anchor_tree, anchor_geoms, anchor_ids,
    )


# ---------------------------------------------------------------------------
# Line-of-sight
# ---------------------------------------------------------------------------

def _shrink_line_frac(lon1: float, lat1: float, lon2: float, lat2: float,
                      frac: float) -> LineString:
    """Shrink line by `frac` of its length from each endpoint."""
    nlon1 = lon1 + (lon2 - lon1) * frac
    nlat1 = lat1 + (lat2 - lat1) * frac
    nlon2 = lon2 - (lon2 - lon1) * frac
    nlat2 = lat2 - (lat2 - lat1) * frac
    return LineString([(nlon1, nlat1), (nlon2, nlat2)])


def _clearance_deg(clearance_m: float, mid_lat: float) -> float:
    """Convert a clearance distance in metres to degrees at the given latitude."""
    lat_m_per_deg = 111_111.0
    lon_m_per_deg = 111_111.0 * math.cos(math.radians(mid_lat))
    avg = (lat_m_per_deg + lon_m_per_deg) / 2.0
    return clearance_m / avg


def check_los(
    a: Anchor, b: Anchor,
    element_tree: STRtree,
    records: list,
    clearance_m: float = 0.0,
) -> tuple[bool, bool]:
    """Return (passes, over_water).

    Step 1: the full line minus 0.5 m at each end must be obstacle-free.
    Step 2: if clearance_m > 0, the centre 80% buffered by clearance_m on each
            side must also be obstacle-free (lateral margin check).
    """
    dist_m = haversine_m(a.lat, a.lon, b.lat, b.lon)
    endpoints = (a.id, b.id)

    # Step 1 — LOS along the line, excluding 0.5 m at each attachment end
    frac_05 = min(0.5 / dist_m, 0.49) if dist_m > 1.0 else 0.0
    los_line = _shrink_line_frac(a.lon, a.lat, b.lon, b.lat, frac_05)
    for idx in element_tree.query(los_line, predicate="intersects"):
        rec = records[idx]
        if rec.is_blocker and rec.anchor_id not in endpoints:
            return False, False

    # Step 2 — lateral clearance corridor over the centre 80%
    if clearance_m > 0:
        mid_lat = (a.lat + b.lat) / 2.0
        center = _shrink_line_frac(a.lon, a.lat, b.lon, b.lat, 0.1)
        corridor = center.buffer(_clearance_deg(clearance_m, mid_lat), cap_style=2)
        for idx in element_tree.query(corridor, predicate="intersects"):
            rec = records[idx]
            if rec.is_blocker and rec.anchor_id not in endpoints:
                return False, False

    full = LineString([(a.lon, a.lat), (b.lon, b.lat)])
    over_water = any(records[idx].is_water
                     for idx in element_tree.query(full, predicate="intersects"))
    return True, over_water


# ---------------------------------------------------------------------------
# Terrain and corridor sampling
# ---------------------------------------------------------------------------

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
        if not len(hits):
            result.append("unknown")
            continue
        best = min(hits, key=lambda idx: _TERRAIN_PRIORITY.get(terrain_labels[idx], 50))
        result.append(terrain_labels[best])
    return result


def _project_bounds_to_axis(geom, line_geom) -> tuple[float, float]:
    """Project geom's bounding-box corners onto line_geom; return normalized (t_start, t_end)."""
    if geom.is_empty:
        return 0.0, 1.0
    minx, miny, maxx, maxy = geom.bounds
    ts = [
        line_geom.project(Point(minx, miny), normalized=True),
        line_geom.project(Point(minx, maxy), normalized=True),
        line_geom.project(Point(maxx, miny), normalized=True),
        line_geom.project(Point(maxx, maxy), normalized=True),
    ]
    return max(0.0, min(ts)), min(1.0, max(ts))


def sample_corridor(
    a: Anchor, b: Anchor,
    clearance_m: float,
    element_tree: STRtree,
    records: list,
    terrain_tree: STRtree,
    terrain_labels: list[str],
    n_lon: int = 10,
    n_lat: int = 5,
) -> tuple[dict, list]:
    """Return (corridor_terrain_pct, corridor_features).

    Queries the same element_tree used for LOS — roads/fences that are blockers
    but also carry a label now appear in corridor_features.

    corridor_terrain_pct: {terrain_type: fraction}, sorted descending.
    corridor_features: [{label, category, t_start, t_end}], deduped by (label, category).
    """
    line_geom = LineString([(a.lon, a.lat), (b.lon, b.lat)])
    mid_lat = (a.lat + b.lat) / 2.0

    # At least 1 m each side so the corridor is never degenerate.
    buf_deg = _clearance_deg(max(clearance_m, 1.0), mid_lat)
    corridor = line_geom.buffer(buf_deg, cap_style=2)

    # Perpendicular unit vector in degree space
    dlon = b.lon - a.lon
    dlat = b.lat - a.lat
    seg_len = math.sqrt(dlon ** 2 + dlat ** 2)
    perp_lon = (-dlat / seg_len) if seg_len > 0 else 0.0
    perp_lat = (dlon / seg_len) if seg_len > 0 else 0.0

    # 2D grid sampling for terrain coverage
    terrain_counts: dict[str, int] = {}
    for i in range(n_lon):
        t = i / (n_lon - 1) if n_lon > 1 else 0.5
        cx = a.lon + t * dlon
        cy = a.lat + t * dlat
        for j in range(n_lat):
            s = (j / (n_lat - 1) * 2 - 1) if n_lat > 1 else 0.0
            pt = Point(cx + s * perp_lon * buf_deg, cy + s * perp_lat * buf_deg)
            hits = terrain_tree.query(pt, predicate="intersects")
            if len(hits):
                best = min(hits, key=lambda idx: _TERRAIN_PRIORITY.get(terrain_labels[idx], 50))
                ttype = terrain_labels[best]
            else:
                ttype = "unknown"
            terrain_counts[ttype] = terrain_counts.get(ttype, 0) + 1

    total = n_lon * n_lat
    corridor_terrain = {
        k: round(v / total, 3)
        for k, v in sorted(terrain_counts.items(), key=lambda x: -x[1])
    }

    # Named features — query the unified element_tree, filter by label
    merged: dict[tuple, list] = {}
    for idx in element_tree.query(corridor, predicate="intersects"):
        rec = records[idx]
        if rec.label is None:
            continue
        try:
            inter = rec.geom.intersection(corridor)
            if inter.is_empty:
                continue
            t_start, t_end = _project_bounds_to_axis(inter, line_geom)
            key = (rec.label, rec.category)
            if key in merged:
                merged[key][0] = min(merged[key][0], t_start)
                merged[key][1] = max(merged[key][1], t_end)
            else:
                merged[key] = [t_start, t_end]
        except Exception:
            pass

    corridor_features = [
        {"label": lbl, "category": cat, "t_start": round(ts, 3), "t_end": round(te, 3)}
        for (lbl, cat), (ts, te) in merged.items()
    ]

    return corridor_terrain, corridor_features
