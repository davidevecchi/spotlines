# CLAUDE.md — Spotlines

You are building **Spotlines** from scratch. This file is your full specification.

---

## What to build

A web app that finds valid slackline spots in any geographic area. The user draws a bounding box on a map; the backend returns every pair of anchor points (trees, guard rails, geological features, forest edges) that are within a chosen distance range and have a clear line of sight between them, annotated with elevation data.

**Stack:** Python backend (FastAPI), plain HTML/JS frontend (Leaflet). No database — geodata from Overpass API, elevation from a local DEM cache.

---

## Project structure

```
backend/          Python package
frontend/         Static files served by FastAPI
requirements.txt
```

Backend modules:
- **main** — FastAPI app, `/spots` and `/slope/{z}/{x}/{y}.png` endpoints, GeoJSON serialisation, post-fetch filters
- **overpass** — Overpass QL query, OSM response parsing, anchor and obstacle models
- **geometry** — haversine distance, spatial index construction, line-of-sight check
- **analysis** — pair enumeration loop, Pair model
- **elevation** — elevation sampling from DEM cache, slope calculation
- **dem** — DEM tile download and disk cache (TINItaly / GLO-30), slope tile rendering

---

## HTTP API

### `GET /spots`

**Required params:** `south`, `west`, `north`, `east` (WGS-84 floats, bounding box)

**Optional params:**

| Param | Type | Default | Range | Meaning |
|-------|------|---------|-------|---------|
| `min_m` | float | 15 | 1–4000 | minimum anchor-pair distance (m) |
| `max_m` | float | 50 | 1–4000 | maximum anchor-pair distance (m) |
| `water` | string | `any` | `any` / `only` / `exclude` | water-crossing filter |
| `max_slope` | float | 10 | 0–90 | maximum slope in degrees |

Reject bounding boxes larger than 0.1° on either axis with HTTP 400.

**Response:** `application/json`, GeoJSON `FeatureCollection`. Each feature:

```json
{
  "type": "Feature",
  "geometry": { "type": "LineString", "coordinates": [[lon_a, lat_a], [lon_b, lat_b]] },
  "properties": {
    "distance_m":    42.3,
    "over_water":    false,
    "anchor_a_id":   123456789,
    "anchor_b_id":   987654321,
    "anchor_a_tags": {},
    "anchor_b_tags": {},
    "anchor_a_kind": "tree",
    "anchor_b_kind": "tree",
    "elev_a":        145.2,
    "elev_b":        143.8,
    "slope_pct":     3.3,
    "slope_deg":     1.9,
    "terrain_elevs": [145.2, 145.0, "…10 values…"],
    "terrain_types": ["forest", "grassland", "…10 values…"]
  }
}
```

`anchor_*_kind` values: `tree`, `guard_rail`, `tree_row`, `forest_edge`, `geological`. Currently only `tree` is active; others are architecturally supported (see `backend/overpass.py` FUTURE comments).

Anchor A is always the westernmost anchor (tie-break: northernmost). Anchor B is always easternmost.

---

### `GET /slope/{z}/{x}/{y}.png`

Returns a 256×256 RGBA PNG slope heatmap tile (plasma colourmap: blue = flat, yellow ≥ 45°). Flat areas (< 1°)
are transparent. Tiles are computed from the DEM cache and cached permanently to disk.

---

## Pipeline — implement in this order

### 1. OSM data fetch (Overpass API)

POST to `https://overpass-api.de/api/interpreter`. Use Overpass QL. A single query fetches both anchors and obstacles.

**Overpass QL query to send** (substitute the actual bbox coordinates):

```
[out:json][timeout:60];
(
  node[natural~"^(tree|arch|arete|bare_rock|cave_entrance|cliff|rock|stone)$"](south,west,north,east);
  nwr[natural~"^(wood|tree_row|water)$"](south,west,north,east);
  nwr[landuse=forest](south,west,north,east);
  way[barrier=guard_rail](south,west,north,east);

  nwr[aeroway](south,west,north,east);
  nwr[amenity](south,west,north,east);
  nwr[barrier](south,west,north,east);
  nwr[building](south,west,north,east);
  nwr[craft](south,west,north,east);
  nwr[emergency](south,west,north,east);
  nwr[healthcare](south,west,north,east);
  way[highway](south,west,north,east);
  nwr[historic](south,west,north,east);
  nwr[landuse](south,west,north,east);
  nwr[leisure](south,west,north,east);
  nwr[man_made](south,west,north,east);
  nwr[military](south,west,north,east);
  nwr[office](south,west,north,east);
  way[power~"^(line|minor_line|cable)$"](south,west,north,east);
  nwr[public_transport](south,west,north,east);
  way[railway](south,west,north,east);
  nwr[shop](south,west,north,east);
  way[telecom=line](south,west,north,east);
  nwr[tourism](south,west,north,east);
  nwr[wastewater](south,west,north,east);
  nwr[waterway](south,west,north,east);
);
out body; >; out skel qt;
```

**Cache results** keyed by bbox (round coordinates to 5 decimal places) with a 5-minute TTL.

#### Parsing anchors

| OSM element | kind |
|-------------|------|
| `node` with `natural=tree` | `tree` |
| `node` with `natural` ∈ {arch, arete, bare_rock, cave_entrance, cliff, rock, stone} | `geological` |
| Every node in a `way` with `natural=tree_row` | `tree_row` |
| Every node in a `way` with `barrier=guard_rail` | `guard_rail` |
| Forest-edge nodes (see below) | `forest_edge` |

**Forest-edge anchor logic:** For each `landuse=forest` way or relation, collect every boundary node and register it as a `forest_edge` anchor using its real OSM node ID.

#### Parsing obstacles

**Blocking — lines and polygons** (reject any pair whose line intersects these):

| Tag key | Exception / filter | Geometry |
|---------|-------------------|----------|
| `aeroway` | none | lines + polygons |
| `amenity` | none | nodes → point buffer (~2 m); ways/relations → polygon |
| `barrier` | `barrier=kerb` is OK | non-kerb ways → lines + polygons; non-kerb nodes → point buffer (~2 m) |
| `building` | none | polygons |
| `craft` | none | polygons |
| `emergency` | none | nodes → point buffer; ways → polygons |
| `healthcare` | none | polygons |
| `highway` | none | lines |
| `historic` | none | polygons; nodes → point buffer |
| `landuse` | education, fairground, allotments, farmland, farmyard, logging, meadow, orchard, basin, grass, greenfield, recreation_ground, winter_sports, **forest** are NOT obstacles | all other values → polygons |
| `leisure` | nature_reserve, park, garden, summer_camp, pitch, dog_park are NOT obstacles | all other values → polygons |
| `man_made` | cutline, clearcut, dyke, embankment are NOT obstacles | all other values → lines + polygons |
| `military` | none | polygons |
| `office` | none | polygons |
| `power` | only `power` ∈ {line, minor_line, cable} | lines |
| `public_transport` | none | nodes → point buffer; ways → polygons |
| `railway` | `railway` ∈ {abandoned, disused} is OK | all other values → lines |
| `shop` | none | nodes → point buffer; ways → polygons |
| `telecom` | only `telecom=line` | lines |
| `tourism` | camp_pitch, camp_site, caravan_site, picnic_site are NOT obstacles | all other values → polygons |
| `wastewater` | none | polygons |
| `waterway` | only `waterway` ∈ {dock, boatyard, water_point, fuel} block | lines + polygons |

**Water — annotation only** (crossable; set `over_water=True`):

| Tag filter | Geometry |
|------------|----------|
| `waterway` where value ∉ {dock, boatyard, water_point, fuel} | LineString |
| `natural=water` | Polygon |

---

### 2. Spatial index

Three separate `STRtree` indices:

- **Blocking index** — all blocking lines, polygons, and point buffers.
- **Water index** — water lines and water polygons.
- **Anchor index** — circular buffer per anchor. Radius = `circumference_tag / (2π)` if available, else 0.5 m minimum.

---

### 3. Pair enumeration

Iterate all N(N−1)/2 anchor combinations.

**Distance check** — haversine great-circle distance (Earth radius 6 371 000 m). Skip if outside `[min_m, max_m]`.

**Line-of-sight check:**

1. Form a line segment between the two anchors.
2. Shrink ~0.3 m from each endpoint to avoid false positives from trunk buffers.
3. Reject if intersects blocking index.
4. Reject if intersects anchor index excluding the two endpoints.
5. Intersects water index → set `over_water = True` (do not reject).

**Anchor ordering** — after passing LOS: A = westernmost (tie-break: northernmost), B = easternmost.

---

### 4. Elevation and slope

Sample 10 evenly-spaced points per pair and look up elevations from `backend/dem.get_elevation_at_points()`.

**DEM cache** (`/media/pi/WD/.cache/spotfinder/`):

```
dem/tinitaly/{N46_E011}.tif    10 m, WGS-84, Italy only  (preferred)
dem/glo30/{N46_E011}.tif       30 m, WGS-84, global      (fallback)
slope/{z}/{x}/{y}.png          slope tiles, derived
```

Tile selection: if the 1°×1° cell overlaps Italy (approx. 35.5–47.1 N, 6.6–18.8 E), try TINItaly first (WCS
download, ~50–100 MB compressed). Otherwise use Copernicus GLO-30 (AWS S3 COG HTTP range read, ~10–20 MB).
All tiles are stored as deflate-compressed WGS-84 GeoTIFFs and reused indefinitely.

Deduplicate sample points to 5 decimal places (~1 m) before querying.

Compute per pair:
- `elev_a`, `elev_b` (m, 1 decimal)
- `terrain_elevs` — 10-element array
- `slope_pct` = `|elev_b − elev_a| / distance_m × 100`
- `slope_deg` = `degrees(atan(|elev_b − elev_a| / distance_m))`

Null if elevation unavailable; slope filter does not apply to those pairs.

---

### 5. Post-fetch filters

- `water=only` → keep only `over_water` pairs
- `water=exclude` → keep only `over_water=false` pairs
- `max_slope` → discard pairs where `slope_deg > max_slope` (null passes)

Serialise surviving pairs to GeoJSON and return.

---

### 6. Slope tile endpoint

`GET /slope/{z}/{x}/{y}.png`:

1. Check disk cache (`slope/{z}/{x}/{y}.png`) → return if hit.
2. Convert tile coords to WGS-84 bbox.
3. Open the relevant 1°×1° DEM tile(s) from cache (downloading if needed).
4. Read a 256×256 window via rasterio with bilinear resampling.
5. Compute slope with `numpy.gradient` (divided by pixel size in metres).
6. Apply plasma colourmap (0°→transparent blue, 45°+→yellow, alpha ramps from 0 at <1° to 200 at max).
7. Save PNG to disk cache and return with `Cache-Control: immutable`.

---

## Frontend

A single HTML page with an embedded JS map. No framework, no build step.

**Map:** Leaflet with baselayers:

| Layer | Tile source | Default |
|-------|-------------|---------|
| OSM | openstreetmap.org | ✓ |
| Satellite names | Esri World Imagery + CARTO labels | |
| Satellite raw | Esri World Imagery | |
| Topo | opentopomap.org | |

**Overlays:**

| Overlay | Source | Notes |
|---------|--------|-------|
| Slope heatmap | `/slope/{z}/{x}/{y}.png` | plasma, `maxNativeZoom: 14` |

**Controls (toolbar above map):**
- Min distance input (metres, default 15)
- Max distance input (metres, default 50)
- Water filter dropdown: Any / Water crossings only / Exclude water
- Max slope input (degrees, default 10)
- Search button

**Interaction:**
1. User draws a rectangle on the map (Leaflet.Draw, rectangle only).
2. Clicking Search calls `GET /spots` with the rectangle bbox and current control values.
3. Show a loading indicator while waiting.

**Result rendering:**
- Draw each pair as a `LineString`.
- Colour by distance using the plasma colourmap over `[min_m, max_m]`.
- Dashed if `over_water`.
- Hover tooltip: distance, elevation, slope direction arrow (↗ / → / ↘).
- Click popup: distance, water warning, inline SVG elevation profile, one card per anchor (kind, species/name, circumference, height, OSM link).
- Distance-range legend.

**Serve the frontend from the FastAPI app** as static files at `/`.

---

## Key numeric values

| What | Value | Why |
|------|-------|-----|
| Default min distance | 15 m | typical short slackline |
| Default max distance | 50 m | typical urban slackline |
| Max bbox side | 0.1° (~11 km) | keeps Overpass queries fast |
| Overpass cache TTL | 300 s | balance freshness vs. load |
| Endpoint shrink | 0.3 m | avoid trunk-buffer false positives |
| Min anchor buffer radius | 0.5 m | fallback when no circumference tag |
| Point obstacle buffer radius | ~2 m | bench / barrier node footprint |
| Elevation sample points per pair | 10 | sufficient slope resolution |
| Elevation deduplication precision | 5 decimal places | ~1 m grid (matches TINItaly 10 m) |
| Slope tile max native zoom | 14 | ~2.4 km × 2.4 km per tile at 46 °N |
| DEM cache root | `/media/pi/WD/.cache/spotfinder/` | permanent, large disk |
| Earth radius (haversine) | 6 371 000 m | standard mean radius |

---

## Gotchas

- All coordinates are WGS-84. Shapely uses `(lon, lat)` order; haversine uses `(lat, lon)`. Be consistent and explicit everywhere.
- The O(N²) pair loop is the performance bottleneck for large areas — keep it tight.
- Build the spatial indices once per request, reuse across all pair checks.
- Endpoint shrink must be converted from metres to degrees using the actual latitude of the line's midpoint.
- The Overpass cache key should use rounded coordinates so slightly different bbox requests hit the same cached result.
- All cached DEM tiles are guaranteed WGS-84 (reprojected at download time by `_save_as_wgs84`). Never assume the raw WCS/COG CRS.
- TINItaly has a self-signed TLS cert — use `verify=False` and suppress urllib warnings.
- The first search in a new 1°×1° cell blocks while the DEM tile downloads (up to ~300 s for a large TINItaly tile). Subsequent requests are instant.
- Slope PNG tiles are derived from DEM tiles and can be safely wiped; they will be recomputed on next request.
