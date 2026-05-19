"""Tests for analysis.py — pair enumeration and distance filtering."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
import numpy as np
import pytest
from shapely.strtree import STRtree
from shapely.geometry import LineString, Polygon, Point

from backend.overpass import Anchor
from backend.geometry import haversine_m, anchor_buffer_deg, check_los, sample_terrain_types, build_terrain_index, _clearance_deg
from backend.analysis import _candidates_numpy, enumerate_pairs
from backend.overpass import parse_osm


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
    # 1° lon at 45°N ≈ 78567 m → 30 m ≈ 0.000382°
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    d = haversine_m(45.0, 11.0, 45.0, 11.0 + dlon)
    assert abs(d - 30) < 0.5, f"Expected ~30 m, got {d:.2f} m"


# ── anchor_buffer_deg ────────────────────────────────────────────────────────

def test_anchor_buffer_default():
    a = Anchor(1, 45.0, 11.0, {}, "tree")
    buf = anchor_buffer_deg(a)
    # Default is 0.5 m → 0.5 / 111111 ≈ 4.5e-6
    assert abs(buf - 0.5 / 111_111) < 1e-8

def test_anchor_buffer_with_circumference():
    # circumference 2π m → radius 1 m → 1 / 111111 ≈ 9e-6
    a = Anchor(1, 45.0, 11.0, {"circumference": str(2 * math.pi)}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 1.0 / 111_111) < 1e-7

def test_anchor_buffer_min_radius():
    # Very small circumference → clamps to 0.5 m
    a = Anchor(1, 45.0, 11.0, {"circumference": "0.01"}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 0.5 / 111_111) < 1e-8

def test_anchor_buffer_invalid_circumference():
    a = Anchor(1, 45.0, 11.0, {"circumference": "abc"}, "tree")
    buf = anchor_buffer_deg(a)
    assert abs(buf - 0.5 / 111_111) < 1e-8


# ── _candidates_numpy ────────────────────────────────────────────────────────

def _make_anchor(id, lat, lon):
    return Anchor(id, lat, lon, {}, "tree")

def test_candidates_no_anchors():
    ii, jj, dists = _candidates_numpy(np.array([]), np.array([]), 10, 50)
    assert len(ii) == 0

def test_candidates_single_anchor():
    lats = np.array([45.0])
    lons = np.array([11.0])
    ii, jj, dists = _candidates_numpy(lats, lons, 10, 50)
    assert len(ii) == 0

def test_candidates_two_anchors_in_range():
    # Two anchors ~30 m apart (east-west at 45°N)
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    lats = np.array([45.0, 45.0])
    lons = np.array([11.0, 11.0 + dlon])
    ii, jj, dists = _candidates_numpy(lats, lons, 15, 50)
    assert len(ii) == 1
    assert abs(dists[0] - 30) < 1.0

def test_candidates_two_anchors_out_of_range():
    # Two anchors ~200 m apart — outside max_m=50
    dlon = 200 / (111_111 * math.cos(math.radians(45)))
    lats = np.array([45.0, 45.0])
    lons = np.array([11.0, 11.0 + dlon])
    ii, jj, dists = _candidates_numpy(lats, lons, 15, 50)
    assert len(ii) == 0

def test_candidates_upper_triangle_only():
    # 4 anchors: ensure each pair appears exactly once
    lats = np.array([45.0, 45.0001, 45.0002, 45.0003])
    lons = np.array([11.0, 11.0, 11.0, 11.0])
    ii, jj, dists = _candidates_numpy(lats, lons, 1, 50)
    assert len(ii) == len(set(zip(ii, jj))), "Duplicate pairs"
    for i, j in zip(ii, jj):
        assert j > i, f"Lower-triangle pair found: ({i},{j})"

def test_candidates_below_min_m():
    # Anchors 1 m apart — below min_m=5
    dlon = 1 / (111_111 * math.cos(math.radians(45)))
    lats = np.array([45.0, 45.0])
    lons = np.array([11.0, 11.0 + dlon])
    ii, jj, dists = _candidates_numpy(lats, lons, 5, 50)
    assert len(ii) == 0


# ── check_los ────────────────────────────────────────────────────────────────

def _empty_tree():
    return STRtree([]), [], []

def test_check_los_clear():
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    blocking_tree = STRtree([])
    water_tree = STRtree([])
    ok, over_water = check_los(
        a, b, blocking_tree, [], [], water_tree, [], clearance_m=0
    )
    assert ok is True
    assert over_water is False

def test_check_los_blocked_by_obstacle():
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    # A wall crossing the line perpendicularly
    wall = LineString([(11.00015, 44.9999), (11.00015, 45.0001)])
    blocking_tree = STRtree([wall])
    blocking_geoms = [wall]
    blocking_anchor_ids = [None]
    water_tree = STRtree([])
    ok, over_water = check_los(
        a, b, blocking_tree, blocking_geoms, blocking_anchor_ids,
        water_tree, [], clearance_m=0
    )
    assert ok is False

def test_check_los_water_crossing():
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    river = LineString([(11.00015, 44.9999), (11.00015, 45.0001)])
    water_tree = STRtree([river])
    water_geoms = [river]
    blocking_tree = STRtree([])
    ok, over_water = check_los(
        a, b, blocking_tree, [], [], water_tree, water_geoms, clearance_m=0
    )
    assert ok is True
    assert over_water is True

def test_check_los_endpoint_anchor_not_blocked():
    """Own trunk buffer should not block the pair."""
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0003)
    # Buffers for these two anchors
    from backend.geometry import anchor_buffer_deg
    buf_a = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
    buf_b = Point(b.lon, b.lat).buffer(anchor_buffer_deg(b))
    blocking_geoms = [buf_a, buf_b]
    blocking_anchor_ids = [a.id, b.id]
    blocking_tree = STRtree(blocking_geoms)
    water_tree = STRtree([])
    ok, over_water = check_los(
        a, b, blocking_tree, blocking_geoms, blocking_anchor_ids,
        water_tree, [], clearance_m=0
    )
    assert ok is True, "Own anchor buffers must not block their own pair"

def test_check_los_intermediate_anchor_blocks():
    """An intermediate tree trunk between the two anchors should block."""
    a = _make_anchor(1, 45.0, 11.0)
    mid = _make_anchor(99, 45.0, 11.00015)  # midpoint
    b = _make_anchor(2, 45.0, 11.0003)
    from backend.geometry import anchor_buffer_deg
    buf_mid = Point(mid.lon, mid.lat).buffer(0.001)  # large buffer to guarantee intersection
    blocking_geoms = [buf_mid]
    blocking_anchor_ids = [mid.id]
    blocking_tree = STRtree(blocking_geoms)
    water_tree = STRtree([])
    ok, _ = check_los(
        a, b, blocking_tree, blocking_geoms, blocking_anchor_ids,
        water_tree, [], clearance_m=0
    )
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
    # Same node id twice: tagged version should win
    data = _make_osm_data(
        {"type": "node", "id": 5, "lat": 45.0, "lon": 11.0},            # skeleton (no tags)
        {"type": "node", "id": 5, "lat": 45.0, "lon": 11.0, "tags": {"natural": "tree"}},  # tagged
    )
    _, _, nodes, _ = parse_osm(data)
    assert nodes[5].get("tags", {}).get("natural") == "tree"

def test_parse_osm_way_stored():
    data = _make_osm_data(
        {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "path"}}
    )
    _, _, _, ways = parse_osm(data)
    assert 10 in ways


# ── enumerate_pairs ──────────────────────────────────────────────────────────

def test_enumerate_pairs_basic():
    # Two trees 30m apart, no obstacles
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0 + dlon)
    from backend.geometry import anchor_buffer_deg
    buf_a = Point(a.lon, a.lat).buffer(anchor_buffer_deg(a))
    buf_b = Point(b.lon, b.lat).buffer(anchor_buffer_deg(b))
    blocking_geoms = [buf_a, buf_b]
    blocking_anchor_ids = [a.id, b.id]
    blocking_tree = STRtree(blocking_geoms)
    water_tree = STRtree([])
    water_geoms = []
    pairs = enumerate_pairs(
        [a, b], min_m=15, max_m=50,
        blocking_tree=blocking_tree, blocking_geoms=blocking_geoms,
        blocking_anchor_ids=blocking_anchor_ids,
        water_tree=water_tree, water_geoms=water_geoms,
        clearance_m=0,
    )
    assert len(pairs) == 1
    assert abs(pairs[0].distance_m - 30) < 1.0

def test_enumerate_pairs_ordering():
    """Anchor A must always be the westernmost."""
    dlon = 30 / (111_111 * math.cos(math.radians(45)))
    west = _make_anchor(1, 45.0, 11.0)
    east = _make_anchor(2, 45.0, 11.0 + dlon)
    blocking_tree = STRtree([])
    water_tree = STRtree([])
    pairs = enumerate_pairs(
        [east, west], min_m=15, max_m=50,
        blocking_tree=blocking_tree, blocking_geoms=[], blocking_anchor_ids=[],
        water_tree=water_tree, water_geoms=[],
        clearance_m=0,
    )
    assert len(pairs) == 1
    assert pairs[0].anchor_a.id == west.id, "A must be the western anchor"
    assert pairs[0].anchor_b.id == east.id

def test_enumerate_pairs_outside_range():
    dlon = 200 / (111_111 * math.cos(math.radians(45)))  # ~200 m
    a = _make_anchor(1, 45.0, 11.0)
    b = _make_anchor(2, 45.0, 11.0 + dlon)
    blocking_tree = STRtree([])
    water_tree = STRtree([])
    pairs = enumerate_pairs(
        [a, b], min_m=15, max_m=50,
        blocking_tree=blocking_tree, blocking_geoms=[], blocking_anchor_ids=[],
        water_tree=water_tree, water_geoms=[],
        clearance_m=0,
    )
    assert len(pairs) == 0


# ── _clearance_deg ───────────────────────────────────────────────────────────

def test_clearance_deg_equator():
    # At equator, lat and lon degrees are equal (~111111 m each)
    # → average is 111111 → 1m = 1/111111 deg
    deg = _clearance_deg(1.0, 0.0)
    assert abs(deg - 1.0 / 111_111) < 1e-8

def test_clearance_deg_at_46n():
    # At 46°N, 1° lon ≈ 77,200 m; average with lat ≈ (111111+77200)/2 ≈ 94155
    import math as _math
    lon_m = 111_111 * _math.cos(_math.radians(46))
    avg = (111_111 + lon_m) / 2
    expected = 1.0 / avg
    deg = _clearance_deg(1.0, 46.0)
    assert abs(deg - expected) < 1e-10

def test_clearance_deg_isotropic_vs_naive():
    # The corrected version should give a different (smaller) value at 46°N than the naive 1/111111
    naive = 1.0 / 111_111
    corrected = _clearance_deg(1.0, 46.0)
    # corrected < naive because longitude degrees are shorter at 46°N → average divisor < 111111 → result > naive
    # Actually: lon_m_per_deg < 111111 → average < 111111 → clearance_deg > naive
    assert corrected > naive, "Corrected clearance should be larger than naive at non-equatorial lat"


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
    from backend.elevation import fetch_elevations
    from backend.analysis import Pair
    pair = Pair(
        anchor_a=_make_anchor(1, 45.0, 11.0),
        anchor_b=_make_anchor(2, 45.0, 11.0003),
        distance_m=haversine_m(45.0, 11.0, 45.0, 11.0003),
        over_water=False,
    )
    # Manually set elevations as if DEM returned 100 m everywhere
    pair.terrain_elevs = [100.0] * 10
    pair.elev_a = 100.0
    pair.elev_b = 100.0
    diff = abs(pair.elev_b - pair.elev_a)
    slope_pct = round(diff / pair.distance_m * 100, 1)
    slope_deg = round(math.degrees(math.atan(diff / pair.distance_m)), 1)
    assert slope_pct == 0.0
    assert slope_deg == 0.0

def test_slope_calculation():
    import math
    dist_m = 30.0
    elev_a, elev_b = 100.0, 103.0
    diff = abs(elev_b - elev_a)
    slope_pct = round(diff / dist_m * 100, 1)
    slope_deg = round(math.degrees(math.atan(diff / dist_m)), 1)
    assert slope_pct == 10.0
    assert abs(slope_deg - 5.7) < 0.15


# ── geometry: blocking classification ────────────────────────────────────────

from backend.geometry import _classify_blocking, _classify_water, LANDUSE_ALLOWED, LEISURE_ALLOWED

def test_classify_blocking_highway_way():
    el = {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"highway": "path"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is not None

def test_classify_blocking_highway_node_ignored():
    el = {"type": "node", "id": 1, "lat": 45.0, "lon": 11.0, "tags": {"highway": "bus_stop"}}
    g = _classify_blocking(el, "node", el["tags"], {}, {})
    assert g is None

def test_classify_blocking_building_node_ignored():
    el = {"type": "node", "id": 1, "lat": 45.0, "lon": 11.0, "tags": {"building": "yes"}}
    g = _classify_blocking(el, "node", el["tags"], {}, {})
    assert g is None

def test_classify_blocking_barrier_kerb_ignored():
    el = {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"barrier": "kerb"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is None

def test_classify_blocking_barrier_fence_blocks():
    el = {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"barrier": "fence"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is not None

def test_classify_blocking_landuse_forest_allowed():
    el = {"type": "way", "id": 1, "nodes": [1, 2, 3, 1], "tags": {"landuse": "forest"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.0}, 3: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is None  # forest is in LANDUSE_ALLOWED → not a blocker

def test_classify_blocking_landuse_residential_blocks():
    el = {"type": "way", "id": 1, "nodes": [1, 2, 3, 1], "tags": {"landuse": "residential"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.0}, 3: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is not None

def test_classify_blocking_leisure_park_allowed():
    el = {"type": "way", "id": 1, "nodes": [1, 2, 3, 1], "tags": {"leisure": "park"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.0}, 3: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is None

def test_classify_blocking_railway_active_blocks():
    el = {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"railway": "rail"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is not None

def test_classify_blocking_railway_abandoned_allowed():
    el = {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"railway": "abandoned"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    g = _classify_blocking(el, "way", el["tags"], nodes, {})
    assert g is None

def test_classify_water_waterway():
    el = {"type": "way", "id": 1, "nodes": [1, 2], "tags": {"waterway": "river"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.001}}
    g = _classify_water(el, "way", el["tags"], nodes, {})
    assert g is not None

def test_classify_water_dock_not_water():
    el = {"type": "way", "id": 1, "nodes": [1, 2, 3, 1], "tags": {"waterway": "dock"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.0}, 3: {"lat": 45.001, "lon": 11.001}}
    g = _classify_water(el, "way", el["tags"], nodes, {})
    assert g is None  # dock is in WATERWAY_BLOCKING → not a water annotation

def test_classify_water_natural_water():
    el = {"type": "way", "id": 1, "nodes": [1, 2, 3, 1], "tags": {"natural": "water"}}
    nodes = {1: {"lat": 45.0, "lon": 11.0}, 2: {"lat": 45.001, "lon": 11.0}, 3: {"lat": 45.001, "lon": 11.001}}
    g = _classify_water(el, "way", el["tags"], nodes, {})
    assert g is not None


# ── tile_bbox ─────────────────────────────────────────────────────────────────

from backend.dem import tile_bbox

def test_tile_bbox_zoom0():
    west, south, east, north = tile_bbox(0, 0, 0)
    assert abs(west - (-180)) < 0.001
    assert abs(east - 180) < 0.001

def test_tile_bbox_zoom1():
    # z=1, x=0, y=0 is top-left tile (NW quadrant)
    west, south, east, north = tile_bbox(1, 0, 0)
    assert west == -180.0
    assert abs(east) < 0.001  # should be 0
    assert north > 80  # near 85.05°

def test_tile_bbox_ordering():
    west, south, east, north = tile_bbox(10, 500, 300)
    assert west < east
    assert south < north


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
