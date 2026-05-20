"""Geometry helpers: haversine, Shapely geometry building, spatial indices, LOS check."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import shapely as _shapely_ufuncs
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from .overpass import Anchor
from . import feature_map as _fm

EARTH_RADIUS = 6_371_000  # metres


_POINT_BUF = 0.00002   # ~2 m in degrees

# ---------------------------------------------------------------------------
# Physical width tables (ported from tests/corridor_raw.py)
# ---------------------------------------------------------------------------

HIGHWAY_W = {
    "motorway": 15.0, "motorway_link": 6.0,
    "trunk": 12.0,    "trunk_link": 5.0,
    "primary": 9.0,   "primary_link": 4.0,
    "secondary": 7.5, "secondary_link": 4.0,
    "tertiary": 6.0,  "tertiary_link": 3.5,
    "unclassified": 5.5, "residential": 5.5,
    "living_street": 4.5, "service": 4.0,
    "pedestrian": 5.0, "track": 3.0,
    "cycleway": 1.8, "footway": 1.5, "path": 1.0,
    "bridleway": 2.0, "steps": 2.0, "crossing": 4.0,
}
BARRIER_W = {
    "fence": 0.1, "wall": 0.5, "hedge": 1.5,
    "guard_rail": 0.3, "kerb": 0.1, "gate": 0.1,
    "bollard": 0.2, "block": 0.5,
}
RAILWAY_W = {
    "rail": 1.7, "light_rail": 1.5, "tram": 1.4,
    "subway": 1.5, "narrow_gauge": 1.0,
}
_LANE_W = 3.25


def infer_width(tags: dict) -> float:
    """Return total physical width in metres inferred from OSM tags."""
    if "width" in tags:
        try:
            return float(tags["width"])
        except ValueError:
            pass
    hw = tags.get("highway", "")
    if "lanes" in tags and hw:
        try:
            w = float(tags["lanes"]) * _LANE_W
            if hw not in ("footway", "path", "cycleway", "steps", "crossing"):
                w += 2.0
            return w
        except ValueError:
            pass
    if hw in HIGHWAY_W:
        return HIGHWAY_W[hw]
    b = tags.get("barrier", "")
    if b in BARRIER_W:
        return BARRIER_W[b]
    r = tags.get("railway", "")
    if r in RAILWAY_W:
        return RAILWAY_W[r]
    if "building" in tags:
        return 0.0
    if tags.get("power") in ("line", "minor_line", "cable"):
        return 0.5
    if tags.get("natural") == "tree":
        try:
            radius = max(float(tags.get("circumference", 0)) / (2 * math.pi), 0.3)
        except (ValueError, TypeError):
            radius = 0.3
        return radius * 2
    if any(k in tags for k in ("amenity", "man_made", "emergency")):
        return 1.0
    return 0.2


# ---------------------------------------------------------------------------
# Unified OSM element record
# ---------------------------------------------------------------------------

@dataclass
class OsmRecord:
    """One geometry emitted during the unified classification pass.

    is_blocker and is_water are mutually exclusive (never both True).
    anchor_id is non-None only for synthetic anchor trunk buffers.
    label/category are populated for any element (node, way, or relation)
    with a human-readable identity (roads, waterways, railways, buildings, etc.).
    tags/osm_id/osm_type carry the raw OSM data for corridor feature extraction.
    """
    geom: object
    is_blocker: bool
    is_water: bool
    anchor_id: Optional[int]
    label: Optional[str]
    category: Optional[str]
    tags: Optional[dict] = None
    osm_id: Optional[int] = None
    osm_type: Optional[str] = None


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
        cache_key = (etype, el["id"])  # tuple key: node/way/relation share numeric ID space
        if cache_key in _cache:
            return _cache[cache_key]
    if etype == "node":
        result = _node_pt(el)
    elif etype == "way":
        result = _way_geom(el, nodes_by_id)
    elif etype == "relation":
        result = _relation_geom(el, ways_by_id, nodes_by_id)
    else:
        result = None
    if _cache is not None:
        _cache[cache_key] = result
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

    # Pass through only features explicitly allowed by the JSON (los, water, or anchor).
    for jkey in _fm.KEYS:
        val = tags.get(jkey)
        if val is not None and val in _fm.DATA.get(jkey, {}):
            p = _fm.props(jkey, val)
            if p.get("los") or p.get("water") or p.get("anchor"):
                return None

    # Physical node obstacles (benches, bollards, poles, etc.) block with an inferred radius.
    if etype == "node":
        if any(k in tags for k in ("amenity", "man_made", "emergency")):
            pt = _node_pt(el)
            if pt:
                radius_deg = infer_width(tags) / 2.0 / 111_111
                return pt.buffer(radius_deg)
        return None
    return geom()


def _classify_water(el: dict, etype: str, tags: dict,
                    nodes_by_id: dict, ways_by_id: dict, _cache: dict | None = None):
    """Return a water Shapely geometry or None."""
    for jkey in _fm.KEYS:
        val = tags.get(jkey)
        if val is not None and val in _fm.DATA.get(jkey, {}):
            if _fm.props(jkey, val).get("water"):
                return _element_geom(el, etype, nodes_by_id, ways_by_id, _cache)
    return None


def _format_tag(val: str) -> str:
    return val.replace("_", " ").capitalize()


# Non-JSON OSM keys handled by hardcoded logic.
# amenity is intentionally excluded: minor values like bench are buffer-only;
# display-worthy amenity values are enumerated in feature_map.json (los=true).
_SIMPLE_CATEGORIES = (
    "highway", "railway", "leisure", "barrier", "man_made",
    "aeroway", "craft", "healthcare",
    "military", "office", "public_transport", "shop", "wastewater",
)

# natural checked last so water=<sub> tags (water=lake, etc.) take precedence
# over natural=water when both are present on the same element.
_JSON_LABEL_KEYS: list[str] = sorted(_fm.KEYS - {"natural"}) + (
    ["natural"] if "natural" in _fm.KEYS else []
)


def _feature_label_category(tags: dict, etype: str) -> tuple:
    """Return (label, category) for corridor display, or (None, None) if not relevant."""
    name = tags.get("name")

    for jkey in _JSON_LABEL_KEYS:
        val = tags.get(jkey)
        if val is not None and val in _fm.DATA.get(jkey, {}):
            p = _fm.props(jkey, val)
            if p.get("los") or p.get("water"):
                return (name or _format_tag(val)), jkey

    for key in _SIMPLE_CATEGORIES:
        val = tags.get(key)
        if val:
            return (name or _format_tag(val)), key

    return None, None


# ---------------------------------------------------------------------------
# Unified spatial index
# ---------------------------------------------------------------------------

def build_all_indices(
    elements: list[dict],
    nodes_by_id: dict,
    ways_by_id: dict,
    anchors: list[Anchor],
    geom_cache: dict[tuple[str, int], object] | None = None,
    mid_lat: float = 45.0,
) -> tuple:
    """Single classification pass over all OSM elements.

    Returns:
        element_tree : STRtree over all OsmRecord geometries
        records      : list[OsmRecord], indexed parallel to element_tree
        anchor_tree  : STRtree over anchor trunk buffers
        anchor_geoms : list of Shapely geometries
        anchor_ids   : list[int] parallel to anchor_geoms
    """
    records: list[OsmRecord] = []
    raw_tags: dict  # reuse variable in loop

    for el in elements:
        raw_tags = el.get("tags") or {}
        if not raw_tags:
            continue
        etype = el["type"]
        eid = el["id"]

        bg = _classify_blocking(el, etype, raw_tags, nodes_by_id, ways_by_id, geom_cache)
        wg = _classify_water(el, etype, raw_tags, nodes_by_id, ways_by_id, geom_cache)
        label, category = _feature_label_category(raw_tags, etype)

        if bg is not None and not bg.is_empty:
            if bg.geom_type in ("LineString", "MultiLineString"):
                half_m = infer_width(raw_tags) / 2.0
                if half_m > 0:
                    bg = bg.buffer(_clearance_deg(half_m, mid_lat), cap_style=2)
            records.append(OsmRecord(
                bg, True, False, None, label, category,
                tags=raw_tags, osm_id=eid, osm_type=etype,
            ))
        elif wg is not None and not wg.is_empty:
            records.append(OsmRecord(
                wg, False, True, None, label, category,
                tags=raw_tags, osm_id=eid, osm_type=etype,
            ))
        elif label is not None:
            geom = _element_geom(el, etype, nodes_by_id, ways_by_id, geom_cache)
            if geom is not None and not geom.is_empty:
                records.append(OsmRecord(
                    geom, False, False, None, label, category,
                    tags=raw_tags, osm_id=eid, osm_type=etype,
                ))

    # Anchor trunk buffers — synthetic blockers, no tags
    anchor_geoms: list = []
    anchor_ids: list[int] = []
    for a in anchors:
        buf = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
        anchor_geoms.append(buf)
        anchor_ids.append(a.id)
        records.append(OsmRecord(buf, True, False, a.id, None, None))

    element_tree = STRtree([r.geom for r in records])
    anchor_tree = STRtree(anchor_geoms) if anchor_geoms else STRtree([])

    return element_tree, records, anchor_tree, anchor_geoms, anchor_ids


# ---------------------------------------------------------------------------
# Line-of-sight
# ---------------------------------------------------------------------------

_LOS_CENTER_FRAC = 0.8  # fraction of line length (centred) used for LOS and landuse blocking checks


def _shrink_line_frac(lon1: float, lat1: float, lon2: float, lat2: float,
                      frac: float) -> LineString:
    """Shrink line by `frac` of its length from each endpoint."""
    nlon1 = lon1 + (lon2 - lon1) * frac
    nlat1 = lat1 + (lat2 - lat1) * frac
    nlon2 = lon2 - (lon2 - lon1) * frac
    nlat2 = lat2 - (lat2 - lat1) * frac
    return LineString([(nlon1, nlat1), (nlon2, nlat2)])


def _clearance_deg(clearance_m: float, mid_lat: float) -> float:
    """Convert a clearance distance in metres to degrees at the given latitude.

    Uses the geometric mean of lat/lon m-per-deg so the resulting Shapely buffer
    is approximately isotropic (equal E-W and N-S clearance in metres).
    """
    lat_mpd = 111_111.0
    lon_mpd = 111_111.0 * math.cos(math.radians(mid_lat))
    return clearance_m / math.sqrt(lat_mpd * lon_mpd)


def check_los(
    a: Anchor, b: Anchor,
    element_tree: STRtree,
    records: list,
    clearance_m: float = 0.0,
) -> tuple[bool, bool]:
    """Return (passes, over_water).

    Step 1: full centerline must be clear of blockers (anchor buffers at endpoints excluded).
    Step 2: clearance corridor (80% of line, shrunk from each end) must also be clear.
    """
    endpoints = (a.id, b.id)
    full = LineString([(a.lon, a.lat), (b.lon, b.lat)])

    # Step 1: full centerline — any blocker (except own anchor buffers) rejects
    for idx in element_tree.query(full, predicate="intersects"):
        rec = records[idx]
        if rec.is_blocker and rec.anchor_id not in endpoints:
            return False, False

    # Step 2: clearance corridor (central 80%) — physical clearance check
    if clearance_m > 0:
        mid_lat = (a.lat + b.lat) / 2.0
        buf_deg = _clearance_deg(clearance_m, mid_lat)
        center = _shrink_line_frac(a.lon, a.lat, b.lon, b.lat, (1.0 - _LOS_CENTER_FRAC) / 2)
        corridor = center.buffer(buf_deg, cap_style=2)
        for idx in element_tree.query(corridor, predicate="intersects"):
            rec = records[idx]
            if rec.is_blocker and rec.anchor_id not in endpoints:
                return False, False

    over_water = any(records[idx].is_water
                     for idx in element_tree.query(full, predicate="intersects"))
    return True, over_water


# ---------------------------------------------------------------------------
# Corridor feature extraction (replaces hex-grid sampling)
# ---------------------------------------------------------------------------

_N_CENTERLINE = 50  # sample points along the centerline for segment computation


def _hits_to_segments(hits: np.ndarray, ts: np.ndarray) -> list[dict]:
    """Convert a boolean hit array to [{t_start, t_end}] run-length segments."""
    segments: list[dict] = []
    in_seg = False
    t_start = 0.0
    for i, hit in enumerate(hits):
        if hit and not in_seg:
            t_start = float(ts[i])
            in_seg = True
        elif not hit and in_seg:
            segments.append({"t_start": round(t_start, 3), "t_end": round(float(ts[i - 1]), 3)})
            in_seg = False
    if in_seg:
        segments.append({"t_start": round(t_start, 3), "t_end": round(float(ts[-1]), 3)})
    return segments


def get_corridor_features(
    a: Anchor, b: Anchor,
    clearance_m: float,
    element_tree: STRtree,
    records: list,
    mid_lat: float,
) -> list[dict]:
    """Return features intersecting the corridor as [{category, label, is_blocker,
    is_water, tags, segments: [{t_start, t_end}]}].

    One STRtree query on the corridor polygon (rotated rect) → candidate records.
    Segments are computed by sampling 50 centerline points with the Shapely 2.x
    intersects() ufunc (vectorised, GIL-free). Features present only in the lateral
    clearance buffer (not on the centerline) are included with segments=[].
    """
    buf_deg = _clearance_deg(max(clearance_m, 0.5), mid_lat)
    corridor = LineString([(a.lon, a.lat), (b.lon, b.lat)]).buffer(buf_deg, cap_style=2)
    cand_indices = element_tree.query(corridor, predicate="intersects")
    if len(cand_indices) == 0:
        return []

    dlon = b.lon - a.lon
    dlat = b.lat - a.lat
    ts = np.linspace(0.0, 1.0, _N_CENTERLINE)
    sample_pts = _shapely_ufuncs.points(a.lon + ts * dlon, a.lat + ts * dlat)

    features: list[dict] = []
    seen: dict[tuple, int] = {}  # (category, label) → index in features

    for idx in cand_indices:
        rec = records[idx]
        if rec.anchor_id is not None or rec.category is None:
            continue

        hits = _shapely_ufuncs.intersects(rec.geom, sample_pts)
        segments = _hits_to_segments(hits, ts) if np.any(hits) else []

        key = (rec.category, rec.label)
        if key in seen:
            features[seen[key]]["segments"].extend(segments)
        else:
            seen[key] = len(features)
            features.append({
                "category": rec.category,
                "label": rec.label,
                "is_blocker": rec.is_blocker,
                "is_water": rec.is_water,
                "tags": rec.tags or {},
                "segments": segments,
            })

    features.sort(key=lambda f: f["segments"][0]["t_start"] if f["segments"] else 2.0)
    return features
