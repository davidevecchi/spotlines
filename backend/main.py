"""FastAPI application — /spots and /slope/image endpoints, static frontend serving."""
from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .analysis import compute_corridor_features, compute_corridor_landuse, enumerate_pairs, filter_landuse_blockers
from .dem import get_slope_image, get_slope_stats, get_contour_svg, get_elevation_image, get_elevation_stats
from .elevation import fetch_elevations
from .geometry import build_all_indices, get_corridor_features
from .landuse import get_or_fetch_raster
from .overpass import fetch_osm, parse_osm

app = FastAPI(title="Spotlines")


@app.get("/slope/stats")
async def slope_stats(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()
    min_deg, max_deg = await loop.run_in_executor(
        None, partial(get_slope_stats, south, west, north, east)
    )
    return {"min_deg": min_deg, "max_deg": max_deg}


@app.get("/slope/contours")
async def slope_contours(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()
    svg = await loop.run_in_executor(
        None, partial(get_contour_svg, south, west, north, east)
    )
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


@app.get("/slope/image")
async def slope_image(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()
    png = await loop.run_in_executor(None, partial(get_slope_image, south, west, north, east))
    return Response(content=png, media_type="image/png")


@app.get("/elevation/image")
async def elevation_image(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()
    png = await loop.run_in_executor(None, partial(get_elevation_image, south, west, north, east))
    return Response(content=png, media_type="image/png")


@app.get("/elevation/stats")
async def elevation_stats_endpoint(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()
    min_m, max_m = await loop.run_in_executor(
        None, partial(get_elevation_stats, south, west, north, east)
    )
    return {"min_m": min_m, "max_m": max_m}


@app.get("/landuse/image")
async def landuse_image(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()
    _, raw = await loop.run_in_executor(
        None, partial(get_or_fetch_raster, south, west, north, east)
    )
    if raw is None:
        raise HTTPException(502, "osmlanduse.org unavailable")
    return Response(
        content=raw,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/debug/rect")
async def debug_rect(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
):
    """Return every classified OSM element in the bbox.

    Each entry: {osm_type, osm_id, tags, is_blocker, is_water, label, category}
    plus a summary of anchor count and record counts.
    """
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    loop = asyncio.get_running_loop()

    def _run():
        data = fetch_osm(south, west, north, east)
        anchors, elements, nodes_by_id, ways_by_id = parse_osm(data)
        geom_cache: dict = {}
        mid_lat = (south + north) / 2.0
        _, records, _, _, _ = build_all_indices(
            elements, nodes_by_id, ways_by_id, anchors, geom_cache, mid_lat=mid_lat,
        )
        items = []
        for rec in records:
            if rec.anchor_id is not None:
                continue  # skip synthetic anchor buffers
            items.append({
                "osm_type": rec.osm_type,
                "osm_id": rec.osm_id,
                "is_blocker": rec.is_blocker,
                "is_water": rec.is_water,
                "label": rec.label,
                "category": rec.category,
                "tags": rec.tags or {},
            })
        return {
            "bbox": {"south": south, "west": west, "north": north, "east": east},
            "n_raw_elements": len(elements),
            "n_anchors": len(anchors),
            "n_records": len(items),
            "records": items,
        }

    result = await loop.run_in_executor(None, _run)
    return result


@app.get("/debug/corridor")
async def debug_corridor(
    node_a: int = Query(...),
    node_b: int = Query(...),
    clearance_m: float = Query(default=1.0, ge=0, le=50),
    fetch_pad_m: float = Query(default=30.0, ge=5, le=200),
):
    """Return corridor feature segments for a pair of OSM node IDs.

    Fetches a small bbox around the two nodes, classifies OSM elements,
    then runs get_corridor_features and returns the result.
    """
    import requests as _req

    loop = asyncio.get_running_loop()

    def _run():
        # Fetch both nodes from the OSM API
        def _fetch_node(nid):
            r = _req.get(
                f"https://api.openstreetmap.org/api/0.6/node/{nid}.json",
                timeout=20, headers={"User-Agent": "Spotlines/1.0"},
            )
            r.raise_for_status()
            el = r.json()["elements"][0]
            return el["lon"], el["lat"], el.get("tags", {})

        lon_a, lat_a, tags_a = _fetch_node(node_a)
        lon_b, lat_b, tags_b = _fetch_node(node_b)

        import math
        mid_lat = (lat_a + lat_b) / 2.0
        pad_lat = fetch_pad_m / 111_000
        pad_lon = fetch_pad_m / (111_000 * max(math.cos(math.radians(mid_lat)), 0.001))
        south = min(lat_a, lat_b) - pad_lat
        north = max(lat_a, lat_b) + pad_lat
        west  = min(lon_a, lon_b) - pad_lon
        east  = max(lon_a, lon_b) + pad_lon

        data = fetch_osm(south, west, north, east)
        anchors, elements, nodes_by_id, ways_by_id = parse_osm(data)
        geom_cache: dict = {}
        element_tree, records, _, _, _ = build_all_indices(
            elements, nodes_by_id, ways_by_id, anchors, geom_cache, mid_lat=mid_lat,
        )

        from .overpass import Anchor
        a = Anchor(node_a, lat_a, lon_a, tags_a, "node")
        b = Anchor(node_b, lat_b, lon_b, tags_b, "node")

        feats = get_corridor_features(a, b, clearance_m, element_tree, records, mid_lat)

        dist_m = math.sqrt(
            ((lon_b - lon_a) * 111_000 * math.cos(math.radians(mid_lat))) ** 2
            + ((lat_b - lat_a) * 111_000) ** 2
        )

        return {
            "node_a": {"id": node_a, "lon": lon_a, "lat": lat_a, "tags": tags_a},
            "node_b": {"id": node_b, "lon": lon_b, "lat": lat_b, "tags": tags_b},
            "distance_m": round(dist_m, 1),
            "clearance_m": clearance_m,
            "n_features": len(feats),
            "features": feats,
        }

    try:
        result = await loop.run_in_executor(None, _run)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return result


@app.get("/spots")
async def get_spots(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
    min_m: float = Query(default=15, ge=1, le=4000),
    max_m: float = Query(default=50, ge=1, le=4000),
    water: str = Query(default="any"),
    max_slope: float = Query(default=5, ge=0, le=90),
    clearance_m: float = Query(default=1, ge=0, le=50),
):
    if abs(north - south) > 0.1 or abs(east - west) > 0.1:
        raise HTTPException(400, "Bounding box exceeds 0.1° per side")
    if water not in ("any", "only", "exclude"):
        raise HTTPException(400, "water must be 'any', 'only', or 'exclude'")
    if min_m >= max_m:
        raise HTTPException(400, "min_m must be less than max_m")

    loop = asyncio.get_running_loop()

    def evt(status: str, pct: int) -> str:
        return "data: " + json.dumps({"status": status, "pct": pct}) + "\n\n"

    async def generate():
        t0 = time.perf_counter()

        yield evt("Querying Overpass API…", 3)
        try:
            fetch_future = asyncio.ensure_future(
                loop.run_in_executor(None, partial(fetch_osm, south, west, north, east))
            )
            t_fetch = time.perf_counter()
            while not fetch_future.done():
                await asyncio.sleep(1)
                elapsed = time.perf_counter() - t_fetch
                yield "data: " + json.dumps({"status": f"Querying Overpass API… ({elapsed:.0f}s)", "pct": 3}) + "\n\n"
            data = fetch_future.result()
        except RuntimeError as exc:
            yield "data: " + json.dumps({"error": str(exc)}) + "\n\n"
            return
        t1 = time.perf_counter()

        n_el = len(data.get("elements", []))
        yield evt(f"Parsing {n_el} OSM elements…", 35)
        anchors, elements, nodes_by_id, ways_by_id = parse_osm(data)
        t2 = time.perf_counter()

        yield evt(f"Building indices ({len(anchors)} anchors)…", 50)
        geom_cache: dict[tuple[str, int], object] = {}
        mid_lat = (south + north) / 2.0
        (element_tree, records,
         _anchor_tree, _anchor_geoms, _anchor_ids) = build_all_indices(
            elements, nodes_by_id, ways_by_id, anchors, geom_cache, mid_lat=mid_lat,
        )
        t3 = time.perf_counter()

        yield evt("Enumerating slackline pairs…", 63)
        pairs = enumerate_pairs(
            anchors, min_m, max_m,
            element_tree, records,
            clearance_m=clearance_m,
            mid_lat=mid_lat,
        )
        t5 = time.perf_counter()

        if pairs:
            yield evt(f"Sampling elevation for {len(pairs)} pairs…", 75)
            await loop.run_in_executor(None, partial(fetch_elevations, pairs))
        t6 = time.perf_counter()

        if water == "only":
            pairs = [p for p in pairs if p.over_water]
        elif water == "exclude":
            pairs = [p for p in pairs if not p.over_water]
        pairs = [p for p in pairs if p.slope_deg is None or p.slope_deg <= max_slope]

        if pairs:
            yield evt(f"Building corridor features for {len(pairs)} pairs…", 80)
            await asyncio.gather(
                loop.run_in_executor(None, partial(compute_corridor_features, pairs, element_tree, records, clearance_m, mid_lat)),
                loop.run_in_executor(None, partial(compute_corridor_landuse, pairs, south, west, north, east)),
            )
            pairs = filter_landuse_blockers(pairs)
        t_grid = time.perf_counter()

        features = [_pair_to_feature(p) for p in pairs]
        t7 = time.perf_counter()

        print(
            f"TIMING  anchors={len(anchors)}  elements={len(elements)}  "
            f"pairs={len(features)} | "
            f"osm={t1-t0:.2f}s  parse={t2-t1:.2f}s  indices={t3-t2:.2f}s  "
            f"pairs={t5-t3:.2f}s  elev={t6-t5:.2f}s  "
            f"grid={t_grid-t6:.2f}s  serial={t7-t_grid:.2f}s  TOTAL={t7-t0:.2f}s",
            flush=True,
        )

        yield (
            "data: "
            + json.dumps({"result": {"type": "FeatureCollection", "features": features}, "pct": 95})
            + "\n\n"
        )

        yield evt("Done.", 100)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _pair_to_feature(p) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [p.anchor_a.lon, p.anchor_a.lat],
                [p.anchor_b.lon, p.anchor_b.lat],
            ],
        },
        "properties": {
            "distance_m": round(p.distance_m, 1),
            "over_water": p.over_water,
            "anchor_a_id": p.anchor_a.id,
            "anchor_b_id": p.anchor_b.id,
            "anchor_a_tags": p.anchor_a.tags,
            "anchor_b_tags": p.anchor_b.tags,
            "anchor_a_kind": p.anchor_a.kind,
            "anchor_b_kind": p.anchor_b.kind,
            "elev_a": p.elev_a,
            "elev_b": p.elev_b,
            "slope_pct": p.slope_pct,
            "slope_deg": p.slope_deg,
            "terrain_elevs": p.terrain_elevs,
            "corridor_features": p.corridor_features,
        },
    }


_FRONTEND = Path(__file__).parent.parent / "frontend"

# Frontend must be mounted last so /spots and /slope are reachable
app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
