# Spotlines

Spotlines finds ideal anchor-pair locations for slacklining in any geographic area. The user draws a bounding box on a
map; the app returns every pair of suitable anchor points (trees, guard rails, geological features, forest edges) that
are connected by a clear line of sight within a chosen distance range, annotated with elevation profiles and water
crossings.

---

## What it does

1. The user draws a rectangle on the map and sets optional filters (distance range, water crossing, max slope).
2. The backend fetches all relevant OpenStreetMap features inside that rectangle.
3. It tests every possible pair of anchor points for distance, line-of-sight, and slope.
4. Valid pairs are returned as a GeoJSON layer, coloured by distance, with per-pair popups showing anchor details and a
   terrain elevation profile.

Geodata comes live from OpenStreetMap (via the Overpass API). Elevation data comes from a local DEM cache
(TINItaly 10 m for Italy, Copernicus GLO-30 30 m elsewhere) — no external elevation API is used.
OSM results are cached in memory for 5 minutes; DEM tiles are cached permanently on disk.

---

## Architecture

```
Browser (map UI)
    │  GET /spots?south=…&north=…&west=…&east=…&min_m=…&max_m=…&water=…&max_slope=…
    │  GET /slope/{z}/{x}/{y}.png
    ▼
HTTP API (Python / FastAPI)
    ├─ Fetch OSM data for bbox        → anchor points + obstacle geometries
    ├─ Build spatial index            → fast intersection lookups
    ├─ Enumerate anchor pairs         → distance filter + line-of-sight check
    ├─ Sample elevations from DEM     → slope calculation
    ├─ Apply user filters             → GeoJSON FeatureCollection response
    └─ Serve slope tiles              → plasma-coloured slope heatmap PNGs

DEM tile cache  (disk)
    ├─ dem/tinitaly/N46_E011.tif      10 m, WGS-84, Italy only
    ├─ dem/glo30/N46_E011.tif         30 m, WGS-84, global fallback
    └─ slope/{z}/{x}/{y}.png          derived, safe to wipe
```

---

## API

### `GET /spots`

| Parameter   | Type   | Default | Range            | Description                           |
|-------------|--------|---------|------------------|---------------------------------------|
| `south`     | float  | —       | —                | Bounding-box south edge (WGS-84 lat)  |
| `west`      | float  | —       | —                | Bounding-box west edge (WGS-84 lon)   |
| `north`     | float  | —       | —                | Bounding-box north edge (WGS-84 lat)  |
| `east`      | float  | —       | —                | Bounding-box east edge (WGS-84 lon)   |
| `min_m`     | float  | 15      | 1–4000           | Minimum anchor-pair distance (metres) |
| `max_m`     | float  | 50      | 1–4000           | Maximum anchor-pair distance (metres) |
| `water`     | string | `any`   | any/only/exclude | Water-crossing filter                 |
| `max_slope` | float  | 10      | 0–90             | Maximum allowed slope (degrees)       |

Bounding boxes larger than 0.1° on either side are rejected with HTTP 400.

**Response:** GeoJSON `FeatureCollection`. Each feature is a `LineString` between the two anchors with properties:

```json
{
  "distance_m": 42.3,
  "over_water": false,
  "anchor_a_id": 123456789,
  "anchor_b_id": 987654321,
  "anchor_a_tags": { "natural": "tree", "species": "Quercus robur" },
  "anchor_b_tags": { "natural": "tree" },
  "anchor_a_kind": "tree",
  "anchor_b_kind": "tree",
  "elev_a": 145.2,
  "elev_b": 143.8,
  "slope_pct": 3.3,
  "slope_deg": 1.9,
  "terrain_elevs": [145.2, 145.0, 144.7, "…10 values total…"],
  "terrain_types": ["forest", "grassland", "forest", "…10 values total…"]
}
```

`anchor_*_kind` is one of: `tree`, `guard_rail`, `tree_row`, `forest_edge`, `geological`.

`terrain_types` is a 10-element array of terrain labels at evenly-spaced sample points along the line.
Possible values: `forest`, `grassland`, `farmland`, `water`, `wetland`, `rock`, `sand`, `shrub`, `urban`, `snow`, `unknown`.

Anchor A is always the westernmost anchor (tie-break: northernmost). Anchor B is always easternmost.

---

### `GET /slope/{z}/{x}/{y}.png`

Returns a 256×256 RGBA PNG tile showing terrain slope as a plasma heatmap (blue = flat, yellow = steep ≥ 45°).
Flat areas (< 1°) are transparent so the base map shows through.

Tiles are computed from the DEM cache on first request and cached to disk permanently.
The Leaflet overlay uses `maxNativeZoom: 14` — higher zoom levels scale up the cached tile.

---

## Query pipeline

### Step 1 — Fetch OSM data (Overpass API)

A single Overpass QL query fetches all relevant features inside the bounding box.

**Anchor sources** (what can be slacklined between):

| Kind          | OSM source                                                                      | Status  |
|---------------|---------------------------------------------------------------------------------|---------|
| `tree`        | `natural=tree` nodes                                                            | active  |
| `geological`  | `natural` = rock, stone, cliff, arch, arete, bare_rock, cave_entrance           | planned |
| `tree_row`    | every node of `natural=tree_row` ways                                           | planned |
| `guard_rail`  | every node of `barrier=guard_rail` ways                                         | planned |
| `forest_edge` | every boundary node of `landuse=forest` ways/relations                          | planned |

> Currently only `tree` anchors are active. The others are architecturally supported and can be enabled in `backend/overpass.py`.

**Obstacle sources** (what blocks a slackline):

| OSM tag            | Exceptions (OK to cross)                                                                                                    | Effect            |
|--------------------|-----------------------------------------------------------------------------------------------------------------------------|-------------------|
| `aeroway`          | —                                                                                                                           | blocks            |
| `amenity`          | —                                                                                                                           | blocks            |
| `barrier`          | `kerb`                                                                                                                      | blocks            |
| `building`         | —                                                                                                                           | blocks            |
| `craft`            | —                                                                                                                           | blocks            |
| `emergency`        | —                                                                                                                           | blocks            |
| `healthcare`       | —                                                                                                                           | blocks            |
| `highway`          | —                                                                                                                           | blocks            |
| `historic`         | —                                                                                                                           | blocks            |
| `landuse`          | education, fairground, allotments, farmland, farmyard, logging, meadow, orchard, basin, grass, greenfield, recreation_ground, winter_sports, **forest** | blocks all others |
| `leisure`          | nature_reserve, park, garden, summer_camp, pitch, dog_park                                                                  | blocks all others |
| `man_made`         | cutline, clearcut, dyke, embankment                                                                                         | blocks all others |
| `military`         | —                                                                                                                           | blocks            |
| `office`           | —                                                                                                                           | blocks            |
| `power`            | line, minor_line, cable                                                                                                     | blocks            |
| `public_transport` | —                                                                                                                           | blocks            |
| `railway`          | abandoned, disused                                                                                                          | blocks all others |
| `shop`             | —                                                                                                                           | blocks            |
| `telecom`          | line                                                                                                                        | blocks            |
| `tourism`          | camp_pitch, camp_site, caravan_site, picnic_site                                                                            | blocks all others |
| `wastewater`       | —                                                                                                                           | blocks            |
| `waterway`         | dock, boatyard, water_point, fuel                                                                                           | blocks those only |

Water bodies (`waterway` values other than the four above, plus `natural=water` areas) are **always crossable** — they do not reject a pair but set `over_water = true` on it.

Overpass results are cached by bbox for ~5 minutes to avoid redundant requests.

---

### Step 2 — Spatial index

All obstacle geometries are loaded into a spatial index (Shapely `STRtree`) so that intersection tests during
pair enumeration are O(log n) rather than O(n).

Three separate indices:

- **Blocking obstacles** — roads, buildings, barriers, etc.
- **Water** — for `over_water` annotation only.
- **Anchor buffers** — a circle around each anchor (radius derived from trunk circumference if known, minimum 0.5 m).
  Used to detect other anchors that a line passes through.

---

### Step 3 — Pair enumeration and line-of-sight

Every combination of two anchors is tested:

1. **Distance** — compute great-circle distance (haversine). Skip if outside `[min_m, max_m]`.

2. **Line-of-sight** — draw a straight line between the two anchors, then:
    - Shrink the line ~0.3 m from each endpoint so the endpoints' own trunk buffers don't cause false positives.
    - Reject if the line intersects any blocking obstacle.
    - Reject if the line passes through the buffer of any *other* anchor (i.e. there is a third tree in the way).
    - If the line intersects water, mark `over_water = true` (don't reject).

3. **Anchor ordering** — after passing LOS, assign A = westernmost anchor (tie-break: northernmost), B = easternmost.

---

### Step 4 — Elevation and slope

For each surviving pair, interpolate 10 evenly-spaced sample points along the line and look up their elevations
from the local DEM cache (`backend/dem.py`).

**DEM sources** (selected automatically per 1°×1° tile):

| Source | Resolution | Coverage | Cache location |
|--------|-----------|----------|----------------|
| TINItaly (INGV) | 10 m | Italy | `dem/tinitaly/` |
| Copernicus GLO-30 (AWS COG) | 30 m | Global | `dem/glo30/` |

Tiles are downloaded on first access and stored permanently as deflate-compressed WGS-84 GeoTIFFs at
`/media/pi/WD/.cache/spotfinder/`. TINItaly is preferred for any 1°×1° cell that overlaps Italy; GLO-30 is the
fallback everywhere else.

Deduplicate sample points across all pairs (round to 5 decimal places, ~1 m grid) before querying the DEM.

Compute per pair:
- `elev_a`, `elev_b` — endpoint elevations (m, 1 decimal)
- `terrain_elevs` — full 10-element sample array
- `slope_pct` = `|elev_b − elev_a| / distance_m × 100`
- `slope_deg` = `atan(|elev_b − elev_a| / distance_m)` in degrees

If elevation data is unavailable, these fields are null and the slope filter does not apply to that pair.

---

### Step 5 — Apply user filters

- `water=only` → keep only `over_water` pairs
- `water=exclude` → remove `over_water` pairs
- `max_slope` → remove pairs where `slope_deg > max_slope` (null slope passes)

Return surviving pairs as a GeoJSON `FeatureCollection`.

---

## Frontend

A single-page map application with:

- A Leaflet map with four baselayers: **OSM** (default), Satellite names, Satellite raw, Topo
- A **Slope heatmap** optional overlay (plasma-coloured, served by `/slope/{z}/{x}/{y}.png`)
- A rectangle-draw tool to define the search area
- A toolbar: min/max distance, water filter, max slope, search button
- Result layer: lines coloured by distance (plasma colourmap), dashed if over water, with hover tooltips (distance) and
  click popups (anchor details, elevation profile SVG)
- A distance-range legend

---

## External APIs used

| API      | URL                                       | Auth | Purpose               |
|----------|-------------------------------------------|------|-----------------------|
| Overpass | `https://overpass-api.de/api/interpreter` | none | OpenStreetMap geodata |

Elevation data is served from locally cached DEM files; no external elevation API is used at runtime.

DEM tiles are downloaded once on first access:

| Source | URL | Auth |
|--------|-----|------|
| Copernicus GLO-30 | `https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/` | none |
| TINItaly | `https://tinitaly.pi.ingv.it/TINItaly_1_1/wcs` | none (CC BY 4.0) |
