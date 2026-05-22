# Spotlines

Spotlines is a geospatial web app for finding clear line-of-sight corridors between anchor points — trees, poles, and similar structures — suitable for mounting slacklines, highlines, or similar setups.

It combines OpenStreetMap data, digital elevation models (DEM), and raster landuse classification to identify viable pairs and render them on an interactive map.

## Features

- Fetches anchor points and potential obstacles from OpenStreetMap via Overpass API
- Verifies line-of-sight geometry with configurable lateral clearance
- Filters by slope, distance range, water presence, and landuse type
- Streams results progressively to the browser as they are computed
- Overlays terrain data: slope, elevation, hillshade, contours, landuse

## Stack

- **Backend:** FastAPI + Uvicorn (async Python)
- **Frontend:** Vanilla JS + Leaflet + noUiSlider
- **Geospatial:** Shapely ≥ 2.0, rasterio, Overpass API
- **Elevation:** GLO-30 / TINItaly DEMs (local cache) with Open-Meteo fallback

## Setup

```bash
pip install -r requirements.txt
```

Optionally set a cache directory for DEM tiles (defaults to `/media/twister/WD/.cache/spotfinder`):

```bash
export SPOTLINES_CACHE_DIR=/path/to/cache
```

## Running

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` in a browser. Pan and zoom to an area, adjust filters, and click **Search** to start an analysis.

## API

| Endpoint | Description |
|---|---|
| `GET /spots` | Main analysis — streams GeoJSON pairs as SSE events |
| `GET /debug/rect` | Raw OSM elements + classification for a bbox |
| `GET /debug/corridor` | Corridor features between two OSM node IDs |
| `GET /slope/image` | Slope raster image |
| `GET /elevation/image` | DEM elevation image |
| `GET /terrain/image` | Composite terrain image (elevation + slope + hillshade) |
| `GET /landuse/image` | osmlanduse.org raster overlay |
| `GET /slope/contours` | SVG contour lines |

Key query parameters for `/spots`:

| Parameter | Description |
|---|---|
| `south`, `west`, `north`, `east` | Bounding box (degrees) |
| `min_dist`, `max_dist` | Distance range between anchors (metres) |
| `max_slope` | Maximum terrain slope (degrees) |
| `clearance_m` | Lateral clearance required on each side (metres) |
| `allow_water` | Whether to allow pairs crossing water |

## Tests

```bash
pytest tests/test_analysis.py -v
```

Run a single test:

```bash
pytest tests/test_analysis.py::test_haversine_same_point -v
```
