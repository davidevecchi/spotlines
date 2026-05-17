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

There is no database. All geodata comes live from OpenStreetMap (via the Overpass API) and elevations from Open-Meteo.
Results are cached in memory for a few minutes to avoid hammering the external APIs on repeat queries.

---

## Architecture

```
Browser (map UI)
    │  GET /spots?south=…&north=…&west=…&east=…&min_m=…&max_m=…&water=…&max_slope=…
    ▼
HTTP API (Python / FastAPI)
    ├─ Fetch OSM data for bbox        → anchor points + obstacle geometries
    ├─ Build spatial index            → fast intersection lookups
    ├─ Enumerate anchor pairs         → distance filter + line-of-sight check
    ├─ Fetch terrain elevations       → slope calculation
    └─ Apply user filters             → GeoJSON FeatureCollection response
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

Bounding boxes larger than roughly 10 km on either side are rejected.

**Response:** GeoJSON `FeatureCollection`. Each feature is a `LineString` between the two anchors with properties:

```json
{
  "distance_m": 42.3,
  "over_water": false,
  "anchor_a_id": 123456789,
  "anchor_b_id": 987654321,
  "anchor_a_tags": {
    "natural": "tree",
    "species": "Quercus robur"
  },
  "anchor_b_tags": {
    "natural": "tree"
  },
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

`terrain_types` is a 10-element array of terrain/feature labels at evenly-spaced sample points along the line. Possible values: `forest`, `grassland`, `farmland`, `water`, `wetland`, `rock`, `sand`, `shrub`, `urban`, `snow`, `unknown`.

Anchor A is always the westernmost anchor (tie-break: northernmost). Anchor B is always easternmost.

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

Forest-edge anchors represent slacklining at the margin of a forest. They use the real OSM node IDs from the forest polygon boundary. The line-of-sight and obstacle checks naturally exclude unusable spots.

**Obstacle sources** (what blocks a slackline):

| OSM tag            | Exceptions (OK to cross)                                                | Effect                                      |
|--------------------|-------------------------------------------------------------------------|---------------------------------------------|
| `aeroway`          | —                                                                       | blocks                                      |
| `amenity`          | —                                                                       | blocks                                      |
| `barrier`          | `kerb`                                                                  | blocks                                      |
| `building`         | —                                                                       | blocks                                      |
| `craft`            | —                                                                       | blocks                                      |
| `emergency`        | —                                                                       | blocks                                      |
| `healthcare`       | —                                                                       | blocks                                      |
| `highway`          | —                                                                       | blocks                                      |
| `historic`         | —                                                                       | blocks                                      |
| `landuse`          | education, fairground, allotments, farmland, farmyard, logging, meadow, orchard, basin, grass, greenfield, recreation_ground, winter_sports | blocks all others (including `forest`) |
| `leisure`          | nature_reserve, park, garden, summer_camp, pitch, dog_park              | blocks all others                           |
| `man_made`         | cutline, clearcut, dyke, embankment                                     | blocks all others                           |
| `military`         | —                                                                       | blocks                                      |
| `office`           | —                                                                       | blocks                                      |
| `power`            | line, minor_line, cable                                                 | blocks                                      |
| `public_transport` | —                                                                       | blocks                                      |
| `railway`          | abandoned, disused                                                      | blocks all others                           |
| `shop`             | —                                                                       | blocks                                      |
| `telecom`          | line                                                                    | blocks                                      |
| `tourism`          | camp_pitch, camp_site, caravan_site, picnic_site                        | blocks all others                           |
| `wastewater`       | —                                                                       | blocks                                      |
| `waterway`         | dock, boatyard, water_point, fuel                                       | blocks those values only                    |

Water bodies (`waterway` values other than the four above, plus `natural=water` areas) are **always crossable** — they do not reject a pair but set `over_water = true` on it.

Overpass results are cached by bbox for ~5 minutes to avoid redundant requests.

---

### Step 2 — Spatial index

All obstacle geometries are loaded into a spatial index (e.g. an R-tree / STRtree) so that intersection tests during
pair enumeration are O(log n) rather than O(n).

Three separate indices are useful:

- **Blocking obstacles** — roads, buildings, barriers, etc.
- **Water** — for `over_water` annotation only.
- **Anchor buffers** — a circle around each anchor (radius derived from trunk circumference if known, with a minimum
  of ~0.5 m). Used to detect other anchors that a line passes through.

---

### Step 3 — Pair enumeration and line-of-sight

Every combination of two anchors is tested:

1. **Distance** — compute great-circle distance (haversine). Skip if outside `[min_m, max_m]`.

2. **Line-of-sight** — draw a straight line between the two anchors, then:
    - Shrink the line ~0.3 m from each endpoint so the endpoints' own trunk buffers don't cause false positives.
    - Reject if the line intersects any blocking obstacle.
    - Reject if the line passes through the buffer of any *other* anchor (i.e. there is a third tree in the way).
    - If the line intersects water, mark `over_water = true` (don't reject).

Pairs that survive both tests are kept.

---

### Step 4 — Elevation and slope

For each surviving pair, sample ~10 evenly-spaced points along the line and fetch their elevations from the Open-Meteo
elevation API (`https://api.open-meteo.com/v1/elevation`, free, no key required).

Deduplicate sample points across all pairs (round to ~4 decimal places, ~11 m grid) before batching requests (max 100
points per call). Respect the API's rate limit (~600 req/min) with a short sleep between batches.

Compute per pair:

- `elev_a`, `elev_b` — endpoint elevations
- `terrain_elevs` — full sample array
- `slope_pct` = `|elev_b − elev_a| / distance_m × 100`
- `slope_deg` = `atan(|elev_b − elev_a| / distance_m)` in degrees

If elevation fetching fails for any reason, leave these fields null and let the pair through (the slope filter simply
won't apply).

---

### Step 5 — Apply user filters

- `water=only` → keep only `over_water` pairs
- `water=exclude` → remove `over_water` pairs
- `max_slope` → remove pairs where `slope_deg > max_slope` (null slope passes)

Return surviving pairs as a GeoJSON `FeatureCollection`.

---

## Frontend

A single-page map application with:

- A Leaflet map with multiple baselayers (CartoDB Voyager default, OSM, topo, satellite)
- A rectangle-draw tool to define the search area
- A toolbar: min/max distance, water filter, max slope, search button
- Result layer: lines coloured by distance (plasma colourmap), dashed if over water, with hover tooltips (distance) and
  click popups (anchor details, elevation profile SVG)
- A distance-range legend

---

## External APIs used

| API                  | URL                                       | Auth | Purpose                    |
|----------------------|-------------------------------------------|------|----------------------------|
| Overpass             | `https://overpass-api.de/api/interpreter` | none | OpenStreetMap geodata      |
| Open-Meteo elevation | `https://api.open-meteo.com/v1/elevation` | none | Terrain elevation profiles |

Both are free and require no registration.
