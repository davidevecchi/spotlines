"""cProfile run of the full /spots pipeline over a central Trento bounding box.

Usage:
    cd /home/pi/Code/spotlines
    python -m tests.profile_trento
"""
from __future__ import annotations

import cProfile
import io
import pstats
import sys
import time
from functools import partial

# Trento city-centre bbox, 0.05° per side (~5.5 km)
SOUTH, WEST, NORTH, EAST = 46.055, 11.095, 46.105, 11.145
MIN_M, MAX_M, CLEARANCE_M = 15.0, 50.0, 1.0

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from backend.overpass import fetch_osm, parse_osm
from backend.geometry import build_all_indices
from backend.analysis import compute_corridor_features, enumerate_pairs
from backend.elevation import fetch_elevations


def pipeline():
    t0 = time.perf_counter()

    print("  [1/4] fetch_osm ...", flush=True)
    data = fetch_osm(SOUTH, WEST, NORTH, EAST)
    t1 = time.perf_counter()
    n_el = len(data.get("elements", []))
    print(f"        {n_el} elements  {t1-t0:.2f}s")

    print("  [2/4] parse_osm ...", flush=True)
    anchors, elements, nodes_by_id, ways_by_id = parse_osm(data)
    t2 = time.perf_counter()
    print(f"        {len(anchors)} anchors, {len(elements)} classified elements  {t2-t1:.2f}s")

    print("  [3/4] build_all_indices ...", flush=True)
    geom_cache: dict = {}
    mid_lat = (SOUTH + NORTH) / 2.0
    element_tree, records, _at, _ag, _ai = build_all_indices(
        elements, nodes_by_id, ways_by_id, anchors, geom_cache, mid_lat=mid_lat,
    )
    t3 = time.perf_counter()
    print(f"        {len(records)} records  {t3-t2:.2f}s")

    print("  [4/4] enumerate_pairs ...", flush=True)
    pairs = enumerate_pairs(
        anchors, MIN_M, MAX_M,
        element_tree, records,
        clearance_m=CLEARANCE_M,
        mid_lat=mid_lat,
    )
    t4 = time.perf_counter()
    print(f"        {len(pairs)} pairs  {t4-t3:.2f}s")

    if pairs:
        print("  [5/5] fetch_elevations ...", flush=True)
        fetch_elevations(pairs)
        t5 = time.perf_counter()
        print(f"        {t5-t4:.2f}s")
    else:
        t5 = t4

    # slope filter (mirror main.py)
    survivors = [p for p in pairs if p.slope_deg is None or p.slope_deg <= 10.0]

    if survivors:
        print(f"  [6/6] compute_corridor_features ({len(survivors)} pairs) ...", flush=True)
        mid_lat = (SOUTH + NORTH) / 2.0
        compute_corridor_features(survivors, element_tree, records, CLEARANCE_M, mid_lat)
        t6 = time.perf_counter()
        print(f"        {t6-t5:.2f}s")
    else:
        t6 = t5

    print(f"\n  TOTAL  {t6-t0:.2f}s  (excl. landuse)")


def main():
    print(f"=== Spotlines profile  bbox={SOUTH},{WEST},{NORTH},{EAST} ===\n")

    # --- warm run without profiler so OSM fetch lands in cache ---
    print("--- warm-up (populates Overpass cache) ---")
    pipeline()

    # --- profiled run (uses cached OSM data) ---
    print("\n--- profiled run ---")
    pr = cProfile.Profile()
    pr.enable()
    pipeline()
    pr.disable()

    buf = io.StringIO()
    ps = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
    ps.print_stats(40)
    print(buf.getvalue())


if __name__ == "__main__":
    main()
