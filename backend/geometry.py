"""Geometry helpers: haversine, Shapely geometry building, spatial indices, LOS check."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
import shapely as _shapely_ufuncs
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from .overpass import Anchor
from . import feature_map as _fm

EARTH_RADIUS = 6_371_000  # metres


_POINT_BUF = 0.00002   # ~2 m in degrees

# ---------------------------------------------------------------------------
# Physical width tables
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
RAILWAY_W = {
    "rail": 1.7, "light_rail": 1.5, "tram": 1.4,
    "subway": 1.5, "narrow_gauge": 1.0,
}
POWER_W = {
    "line": 0.5, "minor_line": 0.5, "cable": 0.5,
}
_LANE_W = 3.25


def infer_width(tags: dict) -> float:
    """Return total physical width in metres inferred from OSM tags."""
    return infer_width_with_note(tags)[0]


def infer_width_with_note(tags: dict) -> tuple[float, str]:
    """Return (width_m, source_note) inferred from OSM tags."""
    if "width" in tags:
        try:
            return float(tags["width"]), f"width={tags['width']}"
        except ValueError:
            pass
    hw = tags.get("highway", "")
    if "lanes" in tags and hw:
        try:
            w = float(tags["lanes"]) * _LANE_W
            if hw not in ("footway", "path", "cycleway", "steps", "crossing"):
                w += 2.0
            return w, f"lanes={tags['lanes']}x{_LANE_W}+shoulders"
        except ValueError:
            pass
    if hw in HIGHWAY_W:
        return HIGHWAY_W[hw], f"highway={hw}"
    r = tags.get("railway", "")
    if r in RAILWAY_W:
        return RAILWAY_W[r], f"railway={r}"
    p = tags.get("power", "")
    if p in POWER_W:
        return POWER_W[p], f"power={p}"
    return 0.2, "default"


# ---------------------------------------------------------------------------
# Unified OSM element record
# ---------------------------------------------------------------------------

@dataclass
class OsmRecord:
    """One geometry emitted during the unified classification pass.

    is_los_blocker / is_buf_blocker track the two corridor zones separately.
    is_water and the blocker flags are mutually exclusive (water is never blocking).
    anchor_id is non-None only for synthetic anchor trunk buffers.
    label/category are populated only for JSON-recognised elements.
    tags/osm_id/osm_type carry the raw OSM data for corridor feature extraction.
    """
    geom: object
    is_los_blocker: bool
    is_buf_blocker: bool
    is_water: bool
    anchor_id: Optional[int]
    label: Optional[str]
    name: Optional[str]
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


def anchor_radius_m(tags: dict) -> float:
    """Physical radius in metres from circumference tags, minimum 0.5 m."""
    for key in ("circumference", "circumference:est"):
        val = tags.get(key)
        if val:
            try:
                return max(float(val) / (2 * math.pi), 0.5)
            except ValueError:
                pass
    return 0.5


def anchor_buffer_deg(anchor: Anchor) -> float:
    return anchor_radius_m(anchor.tags) / 111_111


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
    # Multipolygon relations often have their outer ring split into open ways,
    # which become LineStrings. Polygonize them so the geometry is filled area,
    # not just boundary lines.
    if result.geom_type in ("LineString", "MultiLineString"):
        polys = list(polygonize(result))
        if polys:
            result = unary_union(polys)
    elif result.geom_type == "GeometryCollection":
        lines = [g for g in result.geoms if g.geom_type in ("LineString", "MultiLineString")]
        polys = [g for g in result.geoms if g.geom_type.endswith("Polygon")]
        if lines:
            polys.extend(polygonize(unary_union(lines)))
        if polys:
            result = unary_union(polys)
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

def _format_tag(val: str) -> str:
    return val.replace("_", " ").capitalize()


_JSON_LABEL_KEYS: list[str] = sorted(_fm.KEYS)


def _classify_flags(tags: dict) -> tuple[bool, bool, bool, bool]:
    """Return (is_los_blocker, is_buf_blocker, is_water, is_anchor).

    Looks up the first matching JSON key.  Any value not listed in the JSON
    produces both blocker flags True (the 'not in JSON → blocks both zones' rule).
    """
    for jkey in _JSON_LABEL_KEYS:
        val = tags.get(jkey)
        if val is not None and _fm.covered(jkey, val):
            p = _fm.props(jkey, val)
            if p.get("anchor"):
                return False, False, False, True
            is_water = bool(p.get("water"))
            return not bool(p.get("los")), not bool(p.get("buffer")), is_water, False
    return True, True, False, False


def _blocking_geom(el: dict, etype: str, tags: dict,
                   nodes_by_id: dict, ways_by_id: dict,
                   _cache: dict | None, mid_lat: float):
    """Return a buffered geometry for blocking elements, or None."""
    if etype == "node":
        pt = _node_pt(el)
        if pt is None:
            return None
        radius_deg = infer_width(tags) / 2.0 / 111_111
        return pt.buffer(max(radius_deg, _POINT_BUF))
    geom = _element_geom(el, etype, nodes_by_id, ways_by_id, _cache)
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("LineString", "MultiLineString"):
        half_m = infer_width(tags) / 2.0
        if half_m > 0:
            geom = geom.buffer(_clearance_deg(half_m, mid_lat), cap_style=2)
    return geom


def _feature_label_category(tags: dict) -> tuple:
    """Return (label, category) for corridor display, or (None, None)."""
    name = tags.get("name")
    for jkey in _JSON_LABEL_KEYS:
        val = tags.get(jkey)
        if val is not None and _fm.covered(jkey, val):
            p = _fm.props(jkey, val)
            if p.get("anchor"):
                return None, None, None
            return _format_tag(val), name, jkey
    return None, None, None


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

    for el in elements:
        raw_tags = el.get("tags") or {}
        if not raw_tags:
            continue
        etype = el["type"]
        eid = el["id"]

        is_los_b, is_buf_b, is_water, is_anchor = _classify_flags(raw_tags)
        if is_anchor:
            continue  # handled as trunk buffers below

        label, osm_name, category = _feature_label_category(raw_tags)

        if not is_los_b and not is_buf_b and not is_water and label is None:
            continue  # no classification, no display — skip

        if is_los_b or is_buf_b:
            geom = _blocking_geom(el, etype, raw_tags, nodes_by_id, ways_by_id, geom_cache, mid_lat)
        else:
            geom = _element_geom(el, etype, nodes_by_id, ways_by_id, geom_cache)
            if geom is not None and not geom.is_empty and geom.geom_type == "Point":
                radius_deg = infer_width(raw_tags) / 2.0 / 111_111
                geom = geom.buffer(max(radius_deg, _POINT_BUF))

        if geom is None or geom.is_empty:
            continue

        records.append(OsmRecord(
            geom, is_los_b, is_buf_b, is_water, None, label, osm_name, category,
            tags=raw_tags, osm_id=eid, osm_type=etype,
        ))

    # Anchor trunk buffers — synthetic blockers for both zones, no tags
    anchor_geoms: list = []
    anchor_ids: list[int] = []
    for a in anchors:
        buf = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
        anchor_geoms.append(buf)
        anchor_ids.append(a.id)
        records.append(OsmRecord(buf, True, True, False, a.id, None, None, None))

    element_tree = STRtree([r.geom for r in records])
    anchor_tree = STRtree(anchor_geoms) if anchor_geoms else STRtree([])

    return element_tree, records, anchor_tree, anchor_geoms, anchor_ids


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
    """Convert a clearance distance in metres to degrees (isotropic geometric mean)."""
    lat_mpd = 111_111.0
    lon_mpd = 111_111.0 * math.cos(math.radians(mid_lat))
    return clearance_m / math.sqrt(lat_mpd * lon_mpd)


def _los_rect(a: Anchor, b: Anchor, mid_lat: float) -> Polygon:
    """Thin rectangle covering the full direct line of sight, buffered ±0.25 m laterally."""
    line = LineString([(a.lon, a.lat), (b.lon, b.lat)])
    return line.buffer(_clearance_deg(0.25, mid_lat), cap_style=2)


def _buffer_rect(a: Anchor, b: Anchor, mid_lat: float, clearance_m: float):
    """Rectangle covering the central 80% of the line, ±clearance_m wide.

    Returns None when clearance_m ≤ 0.0 (no buffer zone).
    """
    if clearance_m <= 0.0:
        return None
    center = _shrink_line_frac(a.lon, a.lat, b.lon, b.lat, 0.1)
    return center.buffer(_clearance_deg(clearance_m, mid_lat), cap_style=2)


def check_los(
    a: Anchor, b: Anchor,
    element_tree: STRtree,
    records: list,
    clearance_m: float = 0.0,
) -> tuple[bool, bool]:
    """Return (passes, over_water).

    Step 1: LOS rectangle (full length − 0.5 m each end, ±0.25 m wide) must be
            clear of is_los_blocker elements.
    Step 2: Lateral buffer ring (central 80%, 0.25 m → clearance_m) must be
            clear of is_buf_blocker elements.
    """
    endpoints = (a.id, b.id)
    mid_lat = (a.lat + b.lat) / 2.0

    los = _los_rect(a, b, mid_lat)
    for idx in element_tree.query(los, predicate="intersects"):
        rec = records[idx]
        if rec.is_los_blocker and rec.anchor_id not in endpoints:
            return False, False

    lateral = _buffer_rect(a, b, mid_lat, clearance_m)
    if lateral is not None:
        for idx in element_tree.query(lateral, predicate="intersects"):
            rec = records[idx]
            if rec.is_buf_blocker and rec.anchor_id not in endpoints:
                return False, False

    full_area = los.union(lateral) if lateral is not None else los
    over_water = any(records[idx].is_water
                     for idx in element_tree.query(full_area, predicate="intersects"))
    return True, over_water


# ---------------------------------------------------------------------------
# Corridor feature extraction
# ---------------------------------------------------------------------------

def _intersection_to_t_segments(intersection, baseline: LineString) -> list[dict]:
    """Project an intersection geometry onto the normalized baseline → [{t_start, t_end}]."""
    if intersection is None or intersection.is_empty:
        return []

    sub: list[tuple[float, float]] = []

    def _add(g):
        coords = _shapely_ufuncs.get_coordinates(g)
        if len(coords) == 0:
            return
        pts = _shapely_ufuncs.points(coords[:, 0], coords[:, 1])
        ts = np.clip(_shapely_ufuncs.line_locate_point(baseline, pts, normalized=True), 0.0, 1.0)
        sub.append((float(ts.min()), float(ts.max())))

    if hasattr(intersection, "geoms"):
        for g in intersection.geoms:
            _add(g)
    else:
        _add(intersection)

    if not sub:
        return []

    sub.sort()
    merged: list[list[float]] = [list(sub[0])]
    for s, e in sub[1:]:
        if s <= merged[-1][1] + 1e-4:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    return [{"t_start": round(s, 3), "t_end": round(e, 3)} for s, e in merged]


def get_corridor_features(
    a: Anchor, b: Anchor,
    clearance_m: float,
    element_tree: STRtree,
    records: list,
    mid_lat: float,
) -> list[dict]:
    """Return features intersecting the corridor as [{category, label, is_blocker,
    is_water, tags, segments: [{t_start, t_end}]}].

    Segments are derived by intersecting each feature geometry with the corridor
    polygon and projecting the result onto the normalized baseline — no sampling.
    """
    los = _los_rect(a, b, mid_lat)
    lateral = _buffer_rect(a, b, mid_lat, clearance_m)
    corridor = los.union(lateral) if lateral is not None else los

    cand_indices = element_tree.query(corridor, predicate="intersects")
    if len(cand_indices) == 0:
        return []

    baseline = LineString([(a.lon, a.lat), (b.lon, b.lat)])

    features: list[dict] = []
    seen: dict[tuple, int] = {}

    for idx in cand_indices:
        rec = records[idx]
        if rec.anchor_id is not None or rec.category is None:
            continue

        try:
            intersection = rec.geom.intersection(corridor)
        except Exception:
            continue

        segments = _intersection_to_t_segments(intersection, baseline)

        key = (rec.category, rec.label, rec.name)
        if key in seen:
            existing = features[seen[key]]["segments"]
            existing.extend(segments)
        else:
            seen[key] = len(features)
            features.append({
                "category": rec.category,
                "label": rec.label,
                "name": rec.name,
                "is_blocker": rec.is_los_blocker or rec.is_buf_blocker,
                "is_water": rec.is_water,
                "tags": rec.tags or {},
                "segments": segments,
            })

    # Merge overlapping segments within each feature and sort by first appearance
    for f in features:
        segs = sorted(f["segments"], key=lambda s: s["t_start"])
        merged: list[dict] = []
        for s in segs:
            if merged and s["t_start"] <= merged[-1]["t_end"] + 1e-4:
                merged[-1]["t_end"] = max(merged[-1]["t_end"], s["t_end"])
            else:
                merged.append(dict(s))
        f["segments"] = merged

    features.sort(key=lambda f: f["segments"][0]["t_start"] if f["segments"] else 2.0)
    return features
