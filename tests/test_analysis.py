"""Tests for analysis.py — pair enumeration and distance filtering."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
import numpy as np
import pytest
from shapely.strtree import STRtree
from shapely.geometry import LineString, Polygon, Point

from backend.overpass import Anchor
from backend.geometry import (
    haversine_m, anchor_buffer_deg, check_los, _clearance_deg,
    OsmRecord, build_all_indices,
)
from backend.analysis import _candidates_numpy, enumerate_pairs
from backend.overpass import parse_osm


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_anchor(id, lat, lon):
    return Anchor(id, lat, lon, {}, "tree")


def _empty_indices():
    """Return (element_tree, records) with no obstacles."""
    return STRtree([]), []


def _blocker_indices(geom, anchor_id=None):
    """Return (element_tree, records) with a single blocking geometry (blocks both zones)."""
    rec = OsmRecord(geom, True, True, False, anchor_id, None, None)
    return STRtree([rec.geom]), [rec]


def _water_indices(geom):
    """Return (element_tree, records) with a single water geometry."""
    rec = OsmRecord(geom, False, False, True, None, None, None)
    return STRtree([rec.geom]), [rec]


# ── haversine_m ──────────────────────────────────────────────────────────────

def test_haversine_same_point():
    assert haversine_m(45.0, 11.0, 45.0, 11.0) == 0.0

def test_haversine_known_distance():
    # 1 degree latitude at 45°N ≈ 111_111 m
    d = haversine_m(45.0, 11.0, 46.0, 11.0)
    assert 110_000 < d < 112_000, f"Expected ~111 km, got {d:.0f} m"

def test_haversine_symmetry():
    d1 = haversine_m(45.0, 11.0, 45.01, 11.01)
    d2 = haversine_m(45.01, 11.01, 45.0, 11.0)
    assert abs(d1 - d2) < 0.001

def test_haversine_short_distance():
    # 30 m east along latitude 45°
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    d = haversine_m(45.0, 11.0, 45.0, 11.0 + dlon)
    assert abs(d - 30) < 0.5, f"Expected ~30 m, got {d:.2f} m"


# ── anchor_buffer_deg ────────────────────────────────────────────────────────

def test_anchor_buffer_default():
    a = Anchor(1, 45.0, 11.0, {}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 0.5 / 111_111) < 1e-8

def test_anchor_buffer_with_circumference():
    a = Anchor(1, 45.0, 11.0, {"circumference": str(2 * math.pi)}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 1.0 / 111_111) < 1e-7

def test_anchor_buffer_min_radius():
    a = Anchor(1, 45.0, 11.0, {"circumference": "0.01"}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 0.5 / 111_111) < 1e-8

def test_anchor_buffer_invalid_circumference():
    a = Anchor(1, 45.0, 11.0, {"circumference": "abc"}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 0.5 / 111_111) < 1e-8


# ── _candidates_numpy ────────────────────────────────────────────────────────

def test_candidates_no_anchors():
    ii, jj, dists = _candidates_numpy(np.array([]), np.array([]), 10, 50)
    assert len(ii) == 0

def test_candidates_single_anchor():
    lats = np.array([45.0])
    lons = np.array([11.0])
    ii, jj, dists = _candidates_numpy(lats, lons, 10, 50)
    assert len(ii) == 0

def test_candidates_two_anchors_in_range():
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    lats = np.array([45.0, 45.0])
    lons = np.array([11.0, 11.0 + dlon])
    ii, jj, dists = _candidates_numpy(lats, lons, 15, 50)
    assert len(ii) == 1
    assert abs(dists[0] - 30) < 1.0

def test_candidates_two_anchors_out_of_range():
    dlon = 200 / (111_111 * math.cos(math.radians(45)))
    lats = np.array([45.0, 45.0])
    lons = np.array([11.0, 11.0 + dlon])
    ii, jj, dists = _candidates_numpy(lats, lons, 15, 50)
    assert len(ii) == 0

def test_candidates_upper_triangle_only():
    lats = np.array([45.0, 45.0001, 45.0002, 45.0003])
    lons = np.array([11.0, 11.0, 11.0, 11.0])
    ii, jj, dists = _candidates_numpy(lats, lons, 1, 50)
    assert len(ii) == len(set(zip(ii, jj))), "Duplicate pairs"
    for i, j in zip(ii, jj):
        assert j > i, f"Lower-triangle pair found: ({i},{j})"

def test_candidates_below_min_m():
    dlon = 1 / (111_111 * math.cos(math.radians(45)))
    lats = np.array([45.0, 45.0])
    lons = np.array([11.0, 11.0 + dlon])
    ii, jj, dists = _candidates_numpy(lats, lons, 5, 50)
    assert len(ii) == 0


# ── check_los (unified API) ──────────────────────────────────────────────────

def test_check_los_clear():
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    element_tree, records = _empty_indices()
    ok, over_water = check_los(a, b, element_tree, records, clearance_m=0)
    assert ok is True
    assert over_water is False

def test_check_los_blocked_by_obstacle():
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    wall = LineString([(11.00015, 44.9999), (11.00015, 45.0001)])
    element_tree, records = _blocker_indices(wall)
    ok, over_water = check_los(a, b, element_tree, records, clearance_m=0)
    assert ok is False

def test_check_los_water_crossing():
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    river = LineString([(11.00015, 44.9999), (11.00015, 45.0001)])
    element_tree, records = _water_indices(river)
    ok, over_water = check_los(a, b, element_tree, records, clearance_m=0)
    assert ok is True
    assert over_water is True

def test_check_los_endpoint_anchor_not_blocked():
    """Own trunk buffer should not block the pair."""
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    buf_a = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
    buf_b = Point(b.lon, b.lat).buffer(anchor_buffer_deg(b))
    rec_a = OsmRecord(buf_a, True, True, False, a.id, None, None)
    rec_b = OsmRecord(buf_b, True, True, False, b.id, None, None)
    records = [rec_a, rec_b]
    element_tree = STRtree([r.geom for r in records])
    ok, over_water = check_los(a, b, element_tree, records, clearance_m=0)
    assert ok is True, "Own anchor buffers must not block their own pair"

def test_check_los_intermediate_anchor_blocks():
    """An intermediate tree trunk between the two anchors should block."""
    a = _make_anchor(1, 45.0, 11.0)
    mid = _make_anchor(99, 45.0, 11.00015)
    b = _make_anchor(2, 45.0, 11.0003)
    buf_mid = Point(mid.lon, mid.lat).buffer(0.001)
    rec_mid = OsmRecord(buf_mid, True, True, False, mid.id, None, None)
    records = [rec_mid]
    element_tree = STRtree([r.geom for r in records])
    ok, _ = check_los(a, b, element_tree, records, clearance_m=0)
    assert ok is False, "Intermediate anchor trunk should block LOS"


# ── parse_osm ────────────────────────────────────────────────────────────────

def _make_osm_data(*elements):
    return {"elements": list(elements)}

def test_parse_osm_empty():
    anchors, elements, nodes, ways = parse_osm({"elements": []})
    assert anchors == []
    assert elements == []
    assert nodes == {}
    assert ways == {}

def test_parse_osm_tree_node():
    data = _make_osm_data(
        {"type": "node", "id": 1, "lat": 45.0, "lon": 11.0, "tags": {"natural": "tree"}}
    )
    anchors, elements, nodes, ways = parse_osm(data)
    assert len(anchors) == 1
    assert anchors[0].kind == "tree"
    assert anchors[0].id == 1

def test_parse_osm_non_tree_node():
    data = _make_osm_data(
        {"type": "node", "id": 2, "lat": 45.0, "lon": 11.0, "tags": {"natural": "rock"}}
    )
    anchors, _, _, _ = parse_osm(data)
    assert len(anchors) == 0  # geological anchors are FUTURE/disabled

def test_parse_osm_deduplication():
    data = _make_osm_data(
        {"type": "node", "id": 5, "lat": 45.0, "lon": 11.0},
        {"type": "node", "id": 5, "lat": 45.0, "lon": 11.0, "tags": {"natural": "tree"}},
    )
    _, _, nodes, _ = parse_osm(data)
    assert nodes[5].get("tags", {}).get("natural") == "tree"

def test_parse_osm_way_stored():
    data = _make_osm_data(
        {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "path"}}
    )
    _, _, _, ways = parse_osm(data)
    assert 10 in ways


# ── enumerate_pairs (unified API) ────────────────────────────────────────────

def _anchor_records(anchors):
    """Build anchor-buffer OsmRecords (synthetic blockers) for a list of anchors."""
    records = []
    for a in anchors:
        buf = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
        records.append(OsmRecord(buf, True, True, False, a.id, None, None))
    return records


def test_enumerate_pairs_basic():
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0 + dlon)
    records = _anchor_records([a, b])
    element_tree = STRtree([r.geom for r in records])
    pairs = enumerate_pairs(
        [a, b], min_m=15, max_m=50,
        element_tree=element_tree, records=records,
        clearance_m=0,
    )
    assert len(pairs) == 1
    assert abs(pairs[0].distance_m - 30) < 1.0

def test_enumerate_pairs_ordering():
    """Anchor A must always be the westernmost."""
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    west = _make_anchor(1, 45.0, 11.0)
    east = _make_anchor(2, 45.0, 11.0 + dlon)
    element_tree, records = _empty_indices()
    pairs = enumerate_pairs(
        [east, west], min_m=15, max_m=50,
        element_tree=element_tree, records=records,
        clearance_m=0,
    )
    assert len(pairs) == 1
    assert pairs[0].anchor_a.id == west.id, "A must be the western anchor"
    assert pairs[0].anchor_b.id == east.id

def test_enumerate_pairs_outside_range():
    dlon = 200 / (111_111 * math.cos(math.radians(45)))
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0 + dlon)
    element_tree, records = _empty_indices()
    pairs = enumerate_pairs(
        [a, b], min_m=15, max_m=50,
        element_tree=element_tree, records=records,
        clearance_m=0,
    )
    assert len(pairs) == 0


# ── _clearance_deg ───────────────────────────────────────────────────────────

def test_clearance_deg_equator():
    # At equator, lat and lon degrees are equal → geometric mean = 111111
    deg = _clearance_deg(1.0, 0.0)
    assert abs(deg - 1.0 / 111_111) < 1e-8

def test_clearance_deg_at_46n():
    import math as _math
    lat_mpd = 111_111.0
    lon_mpd = 111_111.0 * _math.cos(_math.radians(46))
    geom_mean = _math.sqrt(lat_mpd * lon_mpd)
    expected = 1.0 / geom_mean
    deg = _clearance_deg(1.0, 46.0)
    assert abs(deg - expected) < 1e-10

def test_clearance_deg_larger_at_46n_than_equator():
    # At 46°N longitude degrees are shorter → geometric mean < 111111 → result > 1/111111
    equator = _clearance_deg(1.0, 0.0)
    north46 = _clearance_deg(1.0, 46.0)
    assert north46 > equator, "Clearance should be larger (looser) at higher latitudes"


# ── elevation interpolation ──────────────────────────────────────────────────

from backend.elevation import _interpolate

def test_interpolate_endpoints():
    pts = _interpolate(45.0, 11.0, 45.1, 11.1, 10)
    assert len(pts) == 10
    assert pts[0] == (45.0, 11.0)
    assert pts[-1] == (45.1, 11.1)

def test_interpolate_midpoint():
    pts = _interpolate(0.0, 0.0, 1.0, 1.0, 3)
    assert abs(pts[1][0] - 0.5) < 1e-9
    assert abs(pts[1][1] - 0.5) < 1e-9

def test_interpolate_n_samples():
    for n in [2, 5, 10, 20]:
        pts = _interpolate(45.0, 11.0, 45.1, 11.1, n)
        assert len(pts) == n


# ── slope calculation ─────────────────────────────────────────────────────────

def test_slope_flat_terrain():
    from backend.analysis import Pair
    pair = Pair(
        anchor_a=_make_anchor(1, 45.0, 11.0),
        anchor_b=_make_anchor(2, 45.0, 11.0003),
        distance_m=haversine_m(45.0, 11.0, 45.0, 11.0003),
        over_water=False,
    )
    pair.terrain_elevs = [100.0] * 10
    pair.elev_a = 100.0
    pair.elev_b = 100.0
    diff = abs(pair.elev_b - pair.elev_a)
    slope_pct = round(diff / pair.distance_m * 100, 1)
    slope_deg = round(math.degrees(math.atan(diff / pair.distance_m)), 1)
    assert slope_pct == 0.0
    assert slope_deg == 0.0

def test_slope_calculation():
    dist_m = 30.0
    elev_a, elev_b = 100.0, 103.0
    diff = abs(elev_b - elev_a)
    slope_pct = round(diff / dist_m * 100, 1)
    slope_deg = round(math.degrees(math.atan(diff / dist_m)), 1)
    assert slope_pct == 10.0
    assert abs(slope_deg - 5.7) < 0.15


# ── geometry: classification flags ───────────────────────────────────────────

from backend.geometry import _classify_flags, _blocking_geom

def test_classify_flags_highway_major_both_blockers():
    # motorway not listed in JSON → both zones blocked
    is_los_b, is_buf_b, is_water, is_anchor = _classify_flags({"highway": "motorway"})
    assert is_los_b is True
    assert is_buf_b is True

def test_classify_flags_highway_path_los_blocker_only():
    # path: los=false, buffer=true → blocks LOS but not buffer
    is_los_b, is_buf_b, is_water, is_anchor = _classify_flags({"highway": "path"})
    assert is_los_b is True
    assert is_buf_b is False

def test_classify_flags_highway_node_blocks():
    # bus_stop not listed in JSON → both blockers (physical presence)
    is_los_b, is_buf_b, _, _ = _classify_flags({"highway": "bus_stop"})
    assert is_los_b is True
    assert is_buf_b is True

def test_blocking_geom_highway_node_returns_buffer():
    el = {"type": "node", "id": 1, "lat": 45.0, "lon": 11.0, "tags": {"highway": "bus_stop"}}
    g = _blocking_geom(el, "node", el["tags"], {}, {}, None, 45.0)
    assert g is not None

def test_classify_flags_barrier_fence_both_blockers():
    # barrier not in JSON → both blockers
    is_los_b, is_buf_b, _, _ = _classify_flags({"barrier": "fence"})
    assert is_los_b is True
    assert is_buf_b is True

def test_classify_flags_leisure_park_not_blocking():
    # park: los=true, buffer=true → neither zone blocked
    is_los_b, is_buf_b, is_water, _ = _classify_flags({"leisure": "park"})
    assert is_los_b is False
    assert is_buf_b is False
    assert is_water is False

def test_classify_flags_railway_active_both_blockers():
    # rail not listed in JSON (only abandoned/disused are) → both blockers
    is_los_b, is_buf_b, _, _ = _classify_flags({"railway": "rail"})
    assert is_los_b is True
    assert is_buf_b is True

def test_classify_flags_railway_abandoned_not_blocking():
    # abandoned: los=true → not blocking
    is_los_b, is_buf_b, _, _ = _classify_flags({"railway": "abandoned"})
    assert is_los_b is False
    assert is_buf_b is False

def test_classify_flags_waterway_river_is_water():
    is_los_b, is_buf_b, is_water, _ = _classify_flags({"waterway": "river"})
    assert is_water is True
    assert is_los_b is False
    assert is_buf_b is False

def test_classify_flags_waterway_dock_not_water():
    # dock not listed in JSON → both blockers, not water
    is_los_b, is_buf_b, is_water, _ = _classify_flags({"waterway": "dock"})
    assert is_water is False
    assert is_los_b is True

def test_classify_flags_natural_water_is_water():
    is_los_b, _, is_water, _ = _classify_flags({"natural": "water"})
    assert is_water is True
    assert is_los_b is False

def test_classify_flags_natural_tree_is_anchor():
    _, _, _, is_anchor = _classify_flags({"natural": "tree"})
    assert is_anchor is True


# ── tile_bbox ─────────────────────────────────────────────────────────────────

from backend.dem import tile_bbox

def test_tile_bbox_zoom0():
    west, south, east, north = tile_bbox(0, 0, 0)
    assert abs(west - (-180)) < 0.001
    assert abs(east - 180) < 0.001

def test_tile_bbox_zoom1():
    west, south, east, north = tile_bbox(1, 0, 0)
    assert west == -180.0
    assert abs(east) < 0.001
    assert north > 80

def test_tile_bbox_ordering():
    west, south, east, north = tile_bbox(10, 500, 300)
    assert west < east
    assert south < north


# ── OSM cache key uniqueness (regression for node/way ID collision) ───────────

def test_osm_cache_key_collision_prevention():
    """Node and way with the same numeric ID must not share a cache entry."""
    node_el = {"type": "node", "id": 100, "lat": 45.0, "lon": 11.0, "tags": {}}
    way_el  = {"type": "way",  "id": 100, "nodes": [1, 2], "tags": {}}
    nodes_by_id = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    cache: dict[tuple[str, int], object] = {}

    from backend.geometry import _element_geom
    g_node = _element_geom(node_el, "node", nodes_by_id, {}, cache)
    g_way  = _element_geom(way_el,  "way",  nodes_by_id, {}, cache)

    assert len(cache) == 2, "Node and way with same ID must produce separate cache entries"
    assert cache[("node", 100)] is g_node
    assert cache[("way",  100)] is g_way
    assert g_node is not g_way or (g_node is None and g_way is None)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
