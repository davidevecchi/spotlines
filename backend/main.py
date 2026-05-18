"""FastAPI application — /spots endpoint and static frontend serving."""
from __future__ import annotations

import asyncio
import time
from functools import partial
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from .analysis import enumerate_pairs
from .dem import get_slope_tile
from .elevation import fetch_elevations
from .geometry import build_spatial_indices, build_terrain_index
from .overpass import fetch_osm, parse_osm

app = FastAPI(title="Spotlines")


@app.get("/slope/{z}/{x}/{y}.png", response_class=Response)
async def slope_tile(z: int, x: int, y: int):
    if not (0 <= z <= 18 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(400, "Invalid tile coordinates")
    loop = asyncio.get_running_loop()
    png = await loop.run_in_executor(None, partial(get_slope_tile, z, x, y))
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


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

    t0 = time.perf_counter()

    try:
        data = await loop.run_in_executor(None, partial(fetch_osm, south, west, north, east))
    except RuntimeError as exc:
        raise HTTPException(502, f"Overpass API error: {exc}") from exc
    t1 = time.perf_counter()

    anchors, elements, nodes_by_id, ways_by_id = parse_osm(data)
    t2 = time.perf_counter()

    geom_cache: dict = {}

    (blocking_tree, blocking_geoms, blocking_anchor_ids,
     water_tree, water_geoms,
     anchor_tree, anchor_geoms, anchor_ids) = build_spatial_indices(
        elements, nodes_by_id, ways_by_id, anchors, geom_cache,
    )
    t3 = time.perf_counter()

    terrain_tree, _terrain_geoms, terrain_labels = build_terrain_index(
        elements, nodes_by_id, ways_by_id, geom_cache,
    )
    t4 = time.perf_counter()

    pairs = enumerate_pairs(
        anchors, min_m, max_m,
        blocking_tree, blocking_geoms, blocking_anchor_ids,
        water_tree, water_geoms,
        anchor_tree, anchor_geoms, anchor_ids,
        clearance_m=clearance_m,
        terrain_tree=terrain_tree,
        terrain_labels=terrain_labels,
    )
    t5 = time.perf_counter()

    if pairs:
        await loop.run_in_executor(None, partial(fetch_elevations, pairs))
    t6 = time.perf_counter()

    # Post-fetch filters
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
        f"osm={t1-t0:.2f}s  parse={t2-t1:.2f}s  spatial={t3-t2:.2f}s  "
        f"terrain={t4-t3:.2f}s  pairs={t5-t4:.2f}s  elev={t6-t5:.2f}s  "
        f"serial={t7-t6:.2f}s  TOTAL={t7-t0:.2f}s",
        flush=True,
    )

    return {"type": "FeatureCollection", "features": features}


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
        },
    }


_FRONTEND = Path(__file__).parent.parent / "frontend"

# Frontend must be mounted last so /spots is reachable
app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
