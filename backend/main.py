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

from .analysis import enumerate_pairs
from .dem import get_slope_image, get_slope_stats, get_contour_svg, get_elevation_image, get_elevation_stats
from .elevation import fetch_elevations
from .geometry import build_all_indices
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


@app.get("/spots")
async def get_spots(
    south: float = Query(...),
    west: float = Query(...),
    north: float = Query(...),
    east: float = Query(...),
    min_m: float = Query(default=15, ge=1, le=4000),
    max_m: float = Query(default=50, ge=1, le=4000),
    water: str = Query(default="any"),
    max_slope: float = Query(default=10, ge=0, le=90),
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
                yield "data: " + json.dumps({"status": f"Querying Overpass API… ({elapsed:.0f}s)"}) + "\n\n"
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
        geom_cache: dict = {}
        (element_tree, records,
         terrain_tree, _terrain_geoms, terrain_labels,
         _anchor_tree, _anchor_geoms, _anchor_ids) = build_all_indices(
            elements, nodes_by_id, ways_by_id, anchors, geom_cache,
        )
        t3 = time.perf_counter()

        yield evt("Enumerating slackline pairs…", 63)
        pairs = enumerate_pairs(
            anchors, min_m, max_m,
            element_tree, records,
            clearance_m=clearance_m,
            terrain_tree=terrain_tree,
            terrain_labels=terrain_labels,
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

        features = [_pair_to_feature(p) for p in pairs]
        t7 = time.perf_counter()

        print(
            f"TIMING  anchors={len(anchors)}  elements={len(elements)}  "
            f"pairs={len(features)} | "
            f"osm={t1-t0:.2f}s  parse={t2-t1:.2f}s  indices={t3-t2:.2f}s  "
            f"pairs={t5-t3:.2f}s  elev={t6-t5:.2f}s  serial={t7-t6:.2f}s  TOTAL={t7-t0:.2f}s",
            flush=True,
        )

        yield (
            "data: "
            + json.dumps({"result": {"type": "FeatureCollection", "features": features}, "pct": 100})
            + "\n\n"
        )

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
            "terrain_types": p.terrain_types,
            "corridor_terrain": p.corridor_terrain,
            "corridor_features": p.corridor_features,
        },
    }


_FRONTEND = Path(__file__).parent.parent / "frontend"

# Frontend must be mounted last so /spots and /slope are reachable
app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
