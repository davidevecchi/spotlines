# CLAUDE.md — Spotlines

You are building **Spotlines** from scratch. This file is your full specification.

---

## What to build

A web app that finds valid slackline spots in any geographic area. The user draws a bounding box on a map; the backend returns every pair of anchor points (trees, guard rails, geological features, forest edges) that are within a chosen distance range and have a clear line of sight between them, annotated with elevation data.

**Stack:** Python backend (FastAPI), plain HTML/JS frontend (Leaflet). No database — all data comes from free external APIs.

---

## Project structure

```
backend/          Python package
frontend/         Static files served by FastAPI
requirements.txt
```

Suggested backend module split:
- **main** — FastAPI app, `/spots` endpoint, GeoJSON serialisation, post-fetch filters
- **overpass** — Overpass QL query, OSM response parsing, anchor and obstacle models
- **geometry** — haversine distance, spatial index construction, line-of-sight check
- **analysis** — pair enumeration loop, Pair model
- **elevation** — Open-Meteo batched fetch, slope calculation

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

## Pipeline — implement in this order

### 1. OSM data fetch (Overpass API)

POST to `https://overpass-api.de/api/interpreter`. Use Overpass QL. A single query should fetch both anchors and obstacles.

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

Notes on query scope:
- `route` relations reference ways already fetched by `highway`/`railway`; no separate query needed.
- `highway` nodes (traffic lights, crossings) are excluded by using `way[highway]` — they add no useful geometry.
- `power` and `telecom` are filtered to only the linear forms that actually cross airspace; area/point power features (substations, towers) are irrelevant.
- Timeout is 60 s because the broader tag set fetches more data than the old query.

**Cache results** keyed by bbox (round coordinates to 5 decimal places) with a 5-minute TTL. A plain dict with a stored timestamp is enough.

#### Parsing anchors

Parse the JSON response into `Anchor` objects (id, lat, lon, tags dict, kind string).

| OSM element | kind |
|-------------|------|
| `node` with `natural=tree` | `tree` |
| `node` with `natural` ∈ {arch, arete, bare_rock, cave_entrance, cliff, rock, stone} | `geological` |
| Every node in a `way` with `natural=tree_row` | `tree_row` |
| Every node in a `way` with `barrier=guard_rail` | `guard_rail` |
| Forest-edge nodes (see below) | `forest_edge` |

**Forest-edge anchor logic:** For each `landuse=forest` way or relation, collect every boundary node and register it directly as a `forest_edge` anchor using its real OSM node ID. No adjacency filtering — the line-of-sight and obstacle checks will exclude unusable spots.

#### Parsing obstacles

Separate incoming OSM elements into obstacle categories. Use Shapely geometry objects. For each element, derive geometry from its node coordinates: open ways → LineString, closed ways and relations → Polygon, nodes → Point (with buffer).

**Blocking — lines and polygons** (reject any pair whose line intersects these):

| Tag key | Exception / filter | Geometry |
|---------|-------------------|----------|
| `aeroway` | none | lines + polygons |
| `amenity` | none | nodes → point buffer (~2 m); ways/relations → polygon |
| `barrier` | `barrier=kerb` is OK (not an obstacle) | non-kerb ways → lines + polygons; non-kerb nodes → point buffer (~2 m) |
| `building` | none | polygons |
| `craft` | none | polygons |
| `emergency` | none | nodes → point buffer; ways → polygons |
| `healthcare` | none | polygons |
| `highway` | none | lines |
| `historic` | none | polygons; nodes → point buffer |
| `landuse` | education, fairground, allotments, farmland, farmyard, logging, meadow, orchard, basin, grass, greenfield, recreation_ground, winter_sports are NOT obstacles | all other values (including `forest`) → polygons |
| `leisure` | nature_reserve, park, garden, summer_camp, pitch, dog_park are NOT obstacles | all other values → polygons |
| `man_made` | allowed values (see below) are NOT obstacles | all other values → lines + polygons |
| `military` | none | polygons |
| `office` | none | polygons |
| `power` | only `power` ∈ {line, minor_line, cable} — towers, poles, substations are ignored | lines |
| `public_transport` | none | nodes → point buffer; ways → polygons |
| `railway` | `railway` ∈ {abandoned, disused} is OK | all other values → lines |
| `shop` | none | nodes → point buffer; ways → polygons |
| `telecom` | only `telecom=line` — other telecom tags are ignored | lines |
| `tourism` | allowed values (see below) are NOT obstacles | all other values → polygons |
| `wastewater` | none | polygons |
| `waterway` | only `waterway` ∈ {dock, boatyard, water_point, fuel} — built structures that physically block | lines + polygons |

**Water — annotation only** (always crossable; do NOT reject the pair; set `over_water=True`):

All waterways are crossable **except** the four built structures above that physically block. Specifically:

| Tag filter | Geometry |
|------------|----------|
| `waterway` where value ∉ {dock, boatyard, water_point, fuel} | LineString |
| `natural=water` | Polygon |

Water geometries do **not** block lines — they only mark pairs as `over_water=True`.

---

### 2. Spatial index

Build three separate spatial indices (use Shapely's `STRtree`):

- **Blocking index** — all blocking lines, polygons, and point buffers.
- **Water index** — water lines and water polygons.
- **Anchor index** — a circular buffer around each anchor. Radius = `circumference_tag / (2π)` if available, else 0.5 m minimum. Store anchor IDs alongside so you can exclude the two endpoints when testing.

---

### 3. Pair enumeration

Iterate all N(N−1)/2 anchor combinations.

**Distance check** — compute haversine great-circle distance. Earth radius: 6 371 000 m. Skip pair if outside `[min_m, max_m]`.

**Line-of-sight check:**

1. Form a line segment between the two anchors.
2. Shrink it ~0.3 m from each endpoint (convert metres to degrees using the mean latitude) to avoid the endpoints' own trunk buffers triggering false positives.
3. Test against the blocking index. Any intersection → reject pair.
4. Test against the anchor index, excluding the two endpoint anchors. Any intersection → reject pair (a third anchor is in the way).
5. Test against the water index. Any intersection → set `over_water = True` (do not reject).

Surviving pairs carry: both anchors, distance, `over_water` flag.

---

### 4. Elevation and slope

Fetch terrain elevations from the Open-Meteo API:

```
GET https://api.open-meteo.com/v1/elevation?latitude=…&longitude=…
```

For each pair, interpolate 10 evenly-spaced sample points along the line. Collect all sample points across all pairs, deduplicate by rounding to 4 decimal places (~11 m grid), then send in batches of ≤ 100. Sleep ~150 ms between batches to stay within the free tier (~600 req/min).

For each pair, look up the returned elevations by rounded coordinate and compute:
- `elev_a`, `elev_b` (m, round to 1 decimal)
- `terrain_elevs` — 10-element array
- `slope_pct` = `|elev_b − elev_a| / distance_m × 100`
- `slope_deg` = `degrees(atan(|elev_b − elev_a| / distance_m))`

If elevation fetching fails for any reason, leave all elevation fields as `null` — the slope filter will not apply to those pairs.

---

### 5. Post-fetch filters

After pair enumeration and elevation:

- `water=only` → keep only pairs where `over_water` is true
- `water=exclude` → keep only pairs where `over_water` is false
- `max_slope` → discard pairs where `slope_deg > max_slope` (null passes through)

Serialise surviving pairs to GeoJSON and return.

---

## Frontend

A single HTML page with an embedded JS map. No framework, no build step.

**Map:** Leaflet with baselayers: CartoDB Voyager (default), OSM, satellite, topo.

**Controls (toolbar above map):**
- Min distance input (metres, default 15)
- Max distance input (metres, default 50)
- Water filter dropdown: Any / Water crossings only / Exclude water
- Max slope input (degrees, default 10)
- Search button

**Interaction:**
1. User draws a rectangle on the map (use Leaflet.Draw, rectangle only).
2. Clicking Search calls `GET /spots` with the rectangle bbox and current control values.
3. Show a loading indicator while waiting.

**Result rendering:**
- Draw each pair as a `LineString` on the map.
- Colour lines by distance using a plasma colourmap (map the `[min_m, max_m]` range to the gradient).
- Draw water-crossing pairs dashed.
- Show a sticky tooltip on hover with the distance.
- Show a popup on click with:
  - Distance in metres
  - Water warning if applicable
  - Elevation profile as an inline SVG graph
  - One card per anchor: kind, species/name if known, circumference, height, link to OSM (`https://www.openstreetmap.org/node/{id}`)
- Show a legend mapping the distance colour range.

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
| Forest-edge neighbour distance | ~5 m (0.00005°) | detect open-area adjacency |
| Elevation sample points per pair | 10 | sufficient slope resolution |
| Elevation deduplication precision | 4 decimal places | ~11 m grid |
| Elevation batch size | 100 points | Open-Meteo limit |
| Elevation inter-batch sleep | 150 ms | stay under 600 req/min |
| Earth radius (haversine) | 6 371 000 m | standard mean radius |

---

## Gotchas

- All coordinates are WGS-84. Shapely uses `(lon, lat)` order; haversine uses `(lat, lon)`. Be consistent and explicit everywhere.
- The O(N²) pair loop is the performance bottleneck for large areas — keep it tight.
- Build the spatial indices once per request, reuse across all pair checks.
- Endpoint shrink must be converted from metres to degrees using the actual latitude of the line's midpoint (degrees per metre varies with latitude).
- The Overpass cache key should use rounded coordinates so slightly different bbox requests hit the same cached result.
- Elevation errors must be silently swallowed — the API can time out on large batches.
