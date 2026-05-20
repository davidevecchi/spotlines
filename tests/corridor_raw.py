#!/usr/bin/env python3
"""
Fetch OSM data around a line segment, infer physical widths, check corridor intersection.

Usage:
    python corridor_raw.py <node_a_id> <node_b_id> [--corridor 1.0] [--fetch 20]

Defaults:
    --corridor  half-width of corridor in metres (each side)   default: 1.0
    --fetch     fetch radius around line in metres             default: 20
"""

import argparse, math, sys, json, urllib.parse
import requests
from shapely.geometry import LineString, Point, Polygon, MultiPolygon, GeometryCollection
from shapely.ops import transform
import pyproj

# ── Width inference ───────────────────────────────────────────────────────────

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
LANE_W = 3.25  # standard European lane width (m)


def infer_width(tags):
    """Return (total_width_m, source_note)."""
    if "width" in tags:
        try:
            return float(tags["width"]), f"width={tags['width']}"
        except ValueError:
            pass
    hw = tags.get("highway", "")
    if "lanes" in tags and hw:
        try:
            w = float(tags["lanes"]) * LANE_W
            if hw not in ("footway", "path", "cycleway", "steps", "crossing"):
                w += 2.0
            return w, f"lanes={tags['lanes']}x{LANE_W}+shoulders"
        except ValueError:
            pass
    if hw in HIGHWAY_W:
        return HIGHWAY_W[hw], f"highway={hw}"
    b = tags.get("barrier", "")
    if b in BARRIER_W:
        return BARRIER_W[b], f"barrier={b}"
    r = tags.get("railway", "")
    if r in RAILWAY_W:
        return RAILWAY_W[r], f"railway={r}"
    if "building" in tags:
        return 0.0, "building polygon"
    if tags.get("power") in ("line", "minor_line", "cable"):
        return 0.5, "power line"
    if tags.get("natural") == "tree":
        try:
            radius = max(float(tags.get("circumference", 0)) / (2 * math.pi), 0.5)
        except (ValueError, TypeError):
            radius = 0.5
        return radius * 2, f"tree r={radius:.2f}m"
    if any(k in tags for k in ("amenity", "man_made", "emergency")):
        return 1.0, "node obstacle"
    return 0.2, "default"


# ── OSM fetch ─────────────────────────────────────────────────────────────────

def fetch_node(node_id):
    url = f"https://api.openstreetmap.org/api/0.6/node/{node_id}.json"
    r = requests.get(url, timeout=30, headers={"User-Agent": "spotlines/1.0"})
    r.raise_for_status()
    el = r.json()["elements"][0]
    return el["lon"], el["lat"], el.get("tags", {})


def fetch_bbox(s, w, n, e):
    query = f"""[out:json][timeout:60];
(node({s:.7f},{w:.7f},{n:.7f},{e:.7f});
way({s:.7f},{w:.7f},{n:.7f},{e:.7f}););
out body; >; out skel qt;"""
    url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(query)
    r = requests.get(url, timeout=60, headers={"User-Agent": "spotlines/1.0"})
    r.raise_for_status()
    return r.json()["elements"]


# ── Geometry helpers ──────────────────────────────────────────────────────────

def anchor_radius(tags):
    """Physical radius of an anchor node in metres."""
    try:
        return max(float(tags.get("circumference", 0)) / (2 * math.pi), 0.5)
    except (ValueError, TypeError):
        return 0.5


def make_corridor(line_utm, half_width, shrink_a=0.0, shrink_b=0.0):
    """Rectangle L×2w along line_utm, shrunk inward from each end by shrink_a/b.
    shrink_a/b are typically the anchor buffer radii so end-cap zones are excluded."""
    coords = list(line_utm.coords)
    ax, ay = coords[0]
    bx, by = coords[-1]
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    ux, uy = dx / length, dy / length          # unit along line
    px, py = -uy, ux                            # unit perpendicular
    ox, oy = px * half_width, py * half_width   # perpendicular offset

    # inset endpoints along the line axis
    a2x = ax + ux * shrink_a
    a2y = ay + uy * shrink_a
    b2x = bx - ux * shrink_b
    b2y = by - uy * shrink_b

    return Polygon([
        (a2x + ox, a2y + oy),
        (b2x + ox, b2y + oy),
        (b2x - ox, b2y - oy),
        (a2x - ox, a2y - oy),
    ])


def overlap_pct(phys, corridor, line_utm):
    """Return (start_pct, end_pct) of the feature's shadow projected onto the line."""
    overlap = phys.intersection(corridor)
    if overlap.is_empty:
        return None, None
    # collect all coordinate points from whatever geometry type came back
    def all_coords(geom):
        if geom.is_empty:
            return []
        if hasattr(geom, "exterior"):          # Polygon
            pts = list(geom.exterior.coords)
            for interior in geom.interiors:
                pts += list(interior.coords)
            return pts
        if hasattr(geom, "coords"):            # Point / LineString
            return list(geom.coords)
        if hasattr(geom, "geoms"):             # Multi* / GeometryCollection
            pts = []
            for g in geom.geoms:
                pts += all_coords(g)
            return pts
        return []

    pts = all_coords(overlap)
    if not pts:
        return None, None
    projections = [line_utm.project(Point(p)) for p in pts]
    length = line_utm.length
    start_pct = round(min(projections) / length * 100, 1)
    end_pct   = round(max(projections) / length * 100, 1)
    return start_pct, end_pct


def el_centerline(el, nodes_by_id, to_utm):
    if el["type"] == "node":
        return Point(to_utm(el["lon"], el["lat"]))
    pts = []
    for nid in el.get("nodes", []):
        n = nodes_by_id.get(nid)
        if n:
            pts.append(to_utm(n["lon"], n["lat"]))
    if len(pts) < 2:
        return None
    if el["nodes"][0] == el["nodes"][-1] and len(pts) >= 3:
        try:
            return Polygon(pts)
        except Exception:
            pass
    return LineString(pts)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("node_a", type=int)
    parser.add_argument("node_b", type=int)
    parser.add_argument("--corridor", type=float, default=1.0,
                        help="half-width of corridor in metres (default: 1.0)")
    parser.add_argument("--fetch", type=float, default=20.0,
                        help="fetch radius around line in metres (default: 20)")
    args = parser.parse_args()

    print(f"Fetching nodes {args.node_a} and {args.node_b} ...", file=sys.stderr)
    lon_a, lat_a, tags_a = fetch_node(args.node_a)
    lon_b, lat_b, tags_b = fetch_node(args.node_b)

    wgs84 = pyproj.CRS("EPSG:4326")
    utm32 = pyproj.CRS("EPSG:32632")
    to_utm   = pyproj.Transformer.from_crs(wgs84, utm32, always_xy=True).transform
    to_wgs84 = pyproj.Transformer.from_crs(utm32, wgs84, always_xy=True).transform

    A_utm = to_utm(lon_a, lat_a)
    B_utm = to_utm(lon_b, lat_b)
    line_utm  = LineString([A_utm, B_utm])
    r_a = anchor_radius(tags_a)
    r_b = anchor_radius(tags_b)
    corridor  = make_corridor(line_utm, args.corridor, shrink_a=r_a, shrink_b=r_b)

    pad_lat = args.fetch / 111000
    pad_lon = args.fetch / (111000 * math.cos(math.radians((lat_a + lat_b) / 2)))
    s = min(lat_a, lat_b) - pad_lat;  n = max(lat_a, lat_b) + pad_lat
    w = min(lon_a, lon_b) - pad_lon;  e = max(lon_a, lon_b) + pad_lon

    print(f"Fetching OSM bbox ...", file=sys.stderr)
    elements = fetch_bbox(s, w, n, e)
    nodes_by_id = {e["id"]: e for e in elements if e["type"] == "node"}

    ANCHOR_IDS = {args.node_a, args.node_b}
    rows = []
    for el in elements:
        tags = el.get("tags", {})
        if not tags:
            continue
        if el["type"] == "node" and el["id"] in ANCHOR_IDS:
            continue
        cl = el_centerline(el, nodes_by_id, to_utm)
        if cl is None:
            continue
        cl_dist = line_utm.distance(cl)
        if cl_dist > args.fetch:
            continue

        total_w, w_src = infer_width(tags)
        half_w = total_w / 2.0
        phys = cl if isinstance(cl, Polygon) else (cl.buffer(half_w) if half_w > 0 else cl)
        if not corridor.intersects(phys):
            continue

        start_pct, end_pct = overlap_pct(phys, corridor, line_utm)

        rec = {
            "type": el["type"],
            "id": el["id"],
            "cl_dist_m": round(cl_dist, 3),
            "inferred_width_m": total_w,
            "width_source": w_src,
            "overlap_start_pct": start_pct,
            "overlap_end_pct": end_pct,
            "tags": tags,
        }
        if el["type"] == "node":
            rec["lat"] = el["lat"]
            rec["lon"] = el["lon"]
        rows.append(rec)

    rows.sort(key=lambda r: r["cl_dist_m"])

    meta = {
        "node_a": {"id": args.node_a, "lon": lon_a, "lat": lat_a, "tags": tags_a},
        "node_b": {"id": args.node_b, "lon": lon_b, "lat": lat_b, "tags": tags_b},
        "line_length_m": round(line_utm.length, 3),
        "corridor_half_width_m": args.corridor,
        "corridor_total_width_m": args.corridor * 2,
        "corridor_area_m2": round(corridor.area, 2),
        "anchor_a_radius_m": r_a,
        "anchor_b_radius_m": r_b,
        "end_shrink_total_m": round(r_a + r_b, 3),
        "fetch_radius_m": args.fetch,
        "features_in_corridor": len(rows),
    }

    print(json.dumps({"meta": meta, "features": rows}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
