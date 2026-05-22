# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Spotlines** is a geospatial web application that identifies clear line-of-sight corridors between anchor points (trees, poles, etc.) for mounting purposes. It combines OpenStreetMap data, terrain analysis (DEM), and landuse classification to find viable placement locations.

**Tech Stack:**
- **Backend:** FastAPI + Uvicorn (async Python)
- **Frontend:** Vanilla JS + Leaflet + noUiSlider (browser-based map)
- **Geospatial:** Shapely (geometry), rasterio (DEM processing), Overpass API (OSM data)
- **Elevation:** GLO-30 / TINItaly DEMs (cached locally) + Open-Meteo fallback

## Architecture & Key Data Flows

### 1. Request Flow (`/spots` endpoint)

The main analysis happens in a streaming response pipeline:

```
GET /spots (bbox + filters) 
  ↓
[Fetch OSM + DEM + Landuse in parallel]
  ↓ parse_osm() → anchors, elements
  ↓ build_all_indices() → STRtree spatial index
  ↓ get_distance_candidates() → pairs within range
  ↓ fetch_elevations() → sample terrain
  ↓ check_los() → line-of-sight + lateral buffer verification
  ↓ get_corridor_features() → extract OSM/landuse blocking features
  ↓ Serialize to GeoJSON + stream progress
```

### 2. Core Modules

**`overpass.py`**
- Fetches OSM data via Overpass API with 5-min caching
- Parses response into anchors (nodes with `anchor: true` flag) and generic elements
- Anchors: extracted from OSM features (trees, poles, etc.) tagged in `feature_map.json`

**`geometry.py`**
- `build_all_indices()`: Single-pass classification of all OSM elements → `OsmRecord` dataclass
- Classification flags: `is_los_blocker`, `is_water`, geometry type (node/way/relation)
- `check_los()`: Two-zone verification:
  1. LOS rectangle (full line ± 0.5 m endpoints, ± 0.25 m lateral)
  2. Lateral buffer ring (80% of line, 0.25 m → `clearance_m`)
- `get_corridor_features()`: Samples 50 centerline points; returns intersecting features + segments (t_start/t_end)
- Width inference: `infer_width()` uses OSM tags (lanes, highway type, etc.) to estimate physical extent

**`analysis.py`**
- `_candidates_numpy()`: Vectorized haversine distance filtering (chunked to avoid memory spikes)
- `enumerate_pairs()`: Distance candidates → LOS filtering → returns valid pairs
- `apply_los_buffer()`: Parallel LOS check (up to 4 workers per slice)
- `compute_corridor_features()`: Parallel feature extraction (thread-safe STRtree in Shapely 2.x)

**`landuse.py`**
- Queries osmlanduse.org WMS; caches images (5 min TTL) keyed by bbox
- `_nearest_type()`: Pixel RGB → landuse label via hardcoded legend palette
- `sample_landuse_along_line()`: 50 samples along centerline; merges consecutive runs
- `check_landuse_blocker()`: Pixel-by-pixel corridor check (uses perpendicular distance to handle pixel grid)
- **Allowed landuse types:** arable, forest, park, meadow, scrub, water, bare, wetland, coastal_wetland

**`dem.py` & `elevation.py`**
- `dem.py`: Manages DEM tile cache (1°×1° GeoTIFFs at 10–30 m resolution)
  - Downloads from USGS/Copernicus on demand; stores in `SPOTLINES_CACHE_DIR`
  - Handles Mercator projection conversion
- `elevation.py`: Samples 10 interpolated points per pair
  - Tries local DEM first; falls back to Open-Meteo API
  - Calculates `slope_deg` and `slope_pct` from elevation difference

**`feature_map.py`**
- Loads `feature_map.json` (auto-generated from OSM checklist)
- Provides `DATA` dict (classification flags), `KEYS` (OSM keys queried), `COLORS` (for UI)
- `props(key, value)`: Lookup returns `{los: bool, water: bool, anchor: bool}` flags

### 3. Frontend (`index.html`)

- **Map:** Leaflet.js with streaming GeoJSON updates
- **Left panel:** Filters (distance range, slope, water, clearance) + detail inspector
- **Right panel:** Legend + terrain overlays (slope, elevation, hillshade, landuse)
- **Event stream:** Server sends `{status, pct, result}` JSON events; client updates map in real-time

## Running & Testing

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Optional: DEM cache (set environment variable)
export SPOTLINES_CACHE_DIR=/path/to/cache  # default: /media/twister/WD/.cache/spotfinder

# Run server
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### Tests
```bash
# Run all tests
pytest tests/test_analysis.py -v

# Run single test
pytest tests/test_analysis.py::test_haversine_same_point -v

# Run with output on failure
pytest tests/test_analysis.py -vv -s
```

### Debugging

**View OSM classification for a bbox:**
```
GET /debug/rect?south=45.0&west=11.0&north=45.1&east=11.1
```
Returns raw elements, anchors, and per-element classification.

**Check corridor features for two nodes:**
```
GET /debug/corridor?node_a=123456&node_b=789012&clearance_m=1.0
```

### Imagery Endpoints
- `/slope/image` / `/slope/stats`: Slope visualization
- `/elevation/image` / `/elevation/stats`: DEM elevation
- `/terrain/image`: Composite (elevation + slope + hillshade)
- `/landuse/image`: osmlanduse.org overlay
- `/slope/contours`: SVG contour lines

## Important Conventions & Patterns

### Coordinate Order
- **Input/OSM API:** lat, lon
- **Shapely geometries:** lon, lat (GIS order)
- **Haversine distance:** lat/lon separate
- Always double-check when mixing sources.

### Feature Classification
1. **Anchors** (kind: "node"): Extracted at parse time; stored as `Anchor` dataclass
2. **Elements** (kind: "way"/"relation"): Classified in `build_all_indices()` → `OsmRecord`
3. **Classification is exclusive:**
   - If `anchor` flag set → skip general classification
   - Else check `los` and `water` flags (can be true independently)
   - Unrecognized elements are blockers (default is blocking)

### Width Inference
Precedence (stop at first match):
1. Direct `width` tag
2. `lanes` + shoulder buffer
3. Highway/railway/power type lookup table
4. Default 0.2 m

### Asyncio Pattern in FastAPI
- Heavy I/O (OSM fetch, DEM read) runs in `loop.run_in_executor()`
- Parallel futures for simultaneous OSM/DEM/raster fetches
- Progress streamed as `text/event-stream` JSON events

### Caching
- **OSM:** 5 min per bbox
- **Landuse raster:** 5 min per bbox
- **DEM tiles:** Indefinite (local disk)
- All caches are thread-safe (use locks)

### Geometry Buffers & Tolerances
- Anchor trunk buffer: from `circumference` tag or 0.5 m default
- LOS rect: full line − 0.5 m endpoints, ± 0.25 m lateral
- Lateral buffer: 0.25 m → `clearance_m` (parameter, default 1.0 m)
- Degree-to-meter conversion: `111_111 m/degree` (isotropic for small distances)

### Error Handling
- Fetch timeouts: 90 s for Overpass, 15 s for landuse WMS
- HTTP 400: Bbox exceeds 0.2° per side
- HTTP 502: Data service unavailable (Overpass, WMS)
- Streaming: Errors sent as `{error: str}` in event stream

## Feature Map & OSM Keys

`feature_map.json` is generated from an OSM Features Checklist (HTML export in repo root). It defines:
- Which OSM tag keys are queried (for Overpass efficiency)
- Per-tag classification (los/water/anchor flags)
- Display colors

To regenerate or inspect, see `tests/build_osm_checklist.py`.

## Terrain & Landuse Filter Logic

**Anchor filtering** (`filter_anchors_by_terrain`):
- Drops anchors on blocking landuse (urban, industrial, quarry, permanent_crops)
- Stores `terrain` label on anchor for display

**Corridor filtering** (`filter_landuse_blockers`):
- Post-feature-extraction: drops pairs if any corridor feature is both blocker=True AND has segments
- Allows crossing unblocking landuse types

**Overlap:**
- OSM features (geometry-based blocking) checked first
- Landuse (raster-based) checked second
- Both must pass for a pair to survive

## Local Development Notes

- **DEM cache drive:** Currently hardcoded to `/media/twister/WD/.cache/spotfinder`
  - Set `SPOTLINES_CACHE_DIR` to override
  - If unavailable, elevation sampling falls back to Open-Meteo API (slower, rate-limited)
- **Overpass timeout:** High-density queries (urban areas) may timeout; monitor logs
- **Frontend dev:** Open `http://localhost:8000` after starting server; live reload via Leaflet
- **Coordinate precision:** GeoJSON uses 5–6 decimal places; rounding applied in serialization

