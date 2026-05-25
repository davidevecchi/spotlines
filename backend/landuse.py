"""Terrain classification via osmlanduse.org WMS.

One GetMap PNG per bbox covers both the /landuse/image overlay and point
classification.  Classification is a local nearest-colour lookup against the
hardcoded legend palette — zero extra HTTP requests after the initial fetch.

Legend extracted from:
  GetLegendGraphic?layer=osmlanduse:osm_lulc_combined_osm4eo&bgColor=0xFF0000
"""
from __future__ import annotations

import io
import json
import logging
import math
import pathlib
import time
from threading import Lock

from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

log = logging.getLogger(__name__)

_WMS          = "https://maps.heigit.org/osmlanduse/wms"
_LAYER_CLASSIC = "osmlanduse:osm_lulc"
_LAYER_FILLED  = "osmlanduse:osm_lulc_combined_osm4eo"
_TIMEOUT = 15
_MAP_W   = 1024
_MAP_H   = 1024
_TTL     = 300   # 5 min

_raster_cache: dict[tuple, tuple] = {}   # bbox_key → (Image RGBA, bytes, expire)
_lock = Lock()


# ── Type metadata from osmlanduse.json ───────────────────────────────────────

_TYPES: dict = json.loads(
    (pathlib.Path(__file__).parent / "osmlanduse.json").read_text()
)
_ALLOWED: frozenset[str] = frozenset(k for k, v in _TYPES.items() if v["allowed"])
_WATER:   frozenset[str] = frozenset(k for k, v in _TYPES.items() if v["water"])

# ── Legend palette ────────────────────────────────────────────────────────────
# Exact colours extracted from the WMS GetLegendGraphic PNG, mapped to short
# lowercase type labels consistent with existing OSM-based obs entries.

_PALETTE: list[tuple[tuple[int, int, int], str]] = [
    ((230,   0,  77), "urban"),           # Urban fabric
    ((255, 255, 168), "arable"),          # Arable land
    (( 77, 255,   0), "forest"),          # Forests
    ((204,  77, 242), "industrial"),      # Industrial, commercial and transport units
    ((255, 166, 255), "park"),            # Artificial, non-agricultural vegetated areas
    ((166,   0, 204), "quarry"),          # Mine, dump and construction sites
    ((230, 230,  77), "meadow"),          # Pastures
    ((230, 128,   0), "permanent_crops"), # Permanent crops
    ((  0, 204, 242), "water"),           # Water bodies
    ((230, 230, 230), "bare"),            # Open spaces with little or no vegetation
    ((204, 242,  77), "scrub"),           # Shrub and/or herbaceous vegetation associations
    ((166, 166, 255), "wetland"),         # Wetlands
    ((230, 230, 255), "coastal_wetland"), # Coastal wetlands
]

def _nearest_type(r: int, g: int, b: int) -> str:
    """Return the closest palette label. The filled WMS layer is exhaustive for all
    land so every opaque pixel belongs to some category — no distance cutoff needed."""
    best_d, best_t = float("inf"), ""
    for (pr, pg, pb), t in _PALETTE:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d, best_t = d, t
    return best_t


# ── Mercator helpers ──────────────────────────────────────────────────────────

def _to_merc(lat: float, lon: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / math.pi * 20037508.34
    return x, y


def _bbox_key(s, w, n, e):
    return round(s, 3), round(w, 3), round(n, 3), round(e, 3)


# ── Raster fetch / cache ──────────────────────────────────────────────────────

def _fetch_layer(layer: str, bbox_str: str) -> Image.Image | None:
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": layer, "STYLES": "",
        "FORMAT": "image/png", "TRANSPARENT": "TRUE",
        "WIDTH": _MAP_W, "HEIGHT": _MAP_H,
        "BBOX": bbox_str, "SRS": "EPSG:3857",
    }
    try:
        resp = requests.get(_WMS, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception as exc:
        log.warning("GetMap %s failed: %s", layer, exc)
        return None


def get_or_fetch_raster(south, west, north, east) -> tuple[Image.Image | None, bytes | None]:
    """Return (PIL Image RGBA, raw PNG bytes), fetching and caching as needed."""
    key = _bbox_key(south, west, north, east)
    now = time.time()
    with _lock:
        # Prune expired entries on every call (hit or miss) to prevent unbounded growth
        stale = [k for k, v in _raster_cache.items() if v[2] <= now]
        for k in stale:
            del _raster_cache[k]
        cached = _raster_cache.get(key)
        if cached and cached[2] > now:
            return cached[0], cached[1]

    x0, y0 = _to_merc(south, west)
    x1, y1 = _to_merc(north, east)
    bbox_str = f"{x0},{y0},{x1},{y1}"

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_classic = ex.submit(_fetch_layer, _LAYER_CLASSIC, bbox_str)
        f_filled  = ex.submit(_fetch_layer, _LAYER_FILLED,  bbox_str)
        classic, filled = f_classic.result(), f_filled.result()

    if classic is None and filled is None:
        return None, None

    if classic is None:
        img = filled
    elif filled is None:
        img = classic
    else:
        # Hard-threshold the classic alpha before compositing so partial-alpha
        # pixels (anti-aliased tile edges) don't produce blended RGB values that
        # fall outside every palette entry and are silently treated as unblocked.
        mask = classic.getchannel("A").point(lambda p: 255 if p >= 128 else 0)
        img = Image.composite(classic, filled, mask)

    raw = io.BytesIO()
    img.save(raw, format="PNG")
    raw = raw.getvalue()

    expire = time.time() + _TTL
    with _lock:
        _raster_cache[key] = (img, raw, expire)
    return img, raw



def sample_landuse_along_line(
    lat_a: float, lon_a: float,
    lat_b: float, lon_b: float,
    south: float, west: float, north: float, east: float,
    img: "Image | None" = None,
) -> list[dict]:
    """Sample landuse along the centreline and return tiling, non-overlapping segments.

    Classifies 51 equally-spaced points along the centreline (no perpendicular
    buffer).  Each point gets exactly one landuse type.  Consecutive same-type
    points are merged into one run; isolated interior singletons are absorbed by
    the shorter neighbour.  The resulting t-values tile [0, 1] exactly so the
    SVG strip and the bar strip always match with no gaps or overlaps.
    """
    if img is None:
        img, _ = get_or_fetch_raster(south, west, north, east)
    if img is None:
        return []

    pixels = img.load()
    img_w, img_h = img.size
    bbox_w = east - west
    bbox_h = north - south

    N = 51   # number of sample points; step = 1/50

    # ── 1. Classify each of the N centreline points to one type ──────────────
    types: list[str] = []
    for i in range(N):
        t   = i / (N - 1)
        lat = lat_a + t * (lat_b - lat_a)
        lon = lon_a + t * (lon_b - lon_a)
        px  = max(0, min(img_w - 1, int((lon - west) / bbox_w * img_w)))
        py  = max(0, min(img_h - 1, int((north - lat) / bbox_h * img_h)))
        r, g, b, a = pixels[px, py]
        types.append(_nearest_type(r, g, b) if a >= 128 else "")

    # ── 2. Run-length encode into (start_idx, end_idx, lu) runs ──────────────
    runs: list[list] = []     # mutable lists so we can update in-place
    i = 0
    while i < N:
        j = i
        while j + 1 < N and types[j + 1] == types[i]:
            j += 1
        runs.append([i, j, types[i]])
        i = j + 1

    # ── 3. Merge interior singletons into shorter neighbour ───────────────────
    idx = 0
    while idx > 0:
        s, e, lu = runs[idx]
        if s == e and s != 0 and e != N - 1:   # interior singleton
            runs[idx - 1][1] = e    # extend left run's end
            runs.pop(idx)
            if runs[idx - 1][2] == runs[idx][2]:
                runs[idx - 1][1] = runs[idx][1]   # extend right run's start
                runs.pop(idx)
        idx += 1
        if idx >= len(runs):
            idx = -1
        
    # ── 4. Convert runs to tiling features (t_start / t_end share boundaries) ─
    features: list[dict] = []
    for s, e, lu in runs:
        if not lu:
            continue
        t_start = round(s / (N - 1), 4)
        t_end   = round(min((e + 1) / (N - 1), 1.0), 4)
        features.append({
            "category": "osmlanduse",
            "label":     lu.replace("_", " ").capitalize(),
            "name":      None,
            "is_blocker": lu not in _ALLOWED,
            "is_water":   lu in _WATER,
            "tags":      {},
            "segments":  [{"t_start": t_start, "t_end": t_end}],
        })

    return features


def check_landuse_blocker(
    lat_a: float, lon_a: float,
    lat_b: float, lon_b: float,
    south: float, west: float, north: float, east: float,
    raster: "Image | None",
    clearance_m: float = 0.0,
) -> bool:
    """Return True if any pixel of the corridor rectangle is in a blocking landuse zone.

    Iterates over all pixels whose centres project onto the line segment within
    the corridor width (clearance_m each side).  Uses a minimum effective width of
    0.5 m so the centerline pixels are always checked even when clearance_m == 0.
    """
    if raster is None:
        return False

    pixels = raster.load()
    img_w, img_h = raster.size
    bbox_w = east - west
    bbox_h = north - south

    # Pixel coordinates of the two endpoints
    pax = (lon_a - west) / bbox_w * img_w
    pay = (north - lat_a) / bbox_h * img_h
    pbx = (lon_b - west) / bbox_w * img_w
    pby = (north - lat_b) / bbox_h * img_h

    mid_lat = (lat_a + lat_b) / 2.0
    eff_clr = max(clearance_m, 0.5)  # metres — ensures at least one pixel column is checked

    # Clearance expressed in pixel units (lat and lon scales differ)
    clr_py = eff_clr / (bbox_h * 111_111) * img_h
    cos_ml = max(math.cos(math.radians(mid_lat)), 0.001)
    clr_px = eff_clr / (bbox_w * 111_111 * cos_ml) * img_w

    # Bounding box of the corridor in pixel space
    min_px = int(max(0, min(pax, pbx) - clr_px - 1))
    max_px = int(min(img_w - 1, max(pax, pbx) + clr_px + 1))
    min_py = int(max(0, min(pay, pby) - clr_py - 1))
    max_py = int(min(img_h - 1, max(pay, pby) + clr_py + 1))

    dlon = lon_b - lon_a
    dlat = lat_b - lat_a
    # Use metric coordinates for the t-projection: raw degree values have different
    # physical lengths per axis at non-equatorial latitudes, so degree dot-products
    # give wrong t values for diagonal corridors (up to ~0.35 m endpoint error).
    dlon_m = dlon * 111_111 * cos_ml
    dlat_m = dlat * 111_111
    len2_m = dlon_m * dlon_m + dlat_m * dlat_m
    if len2_m == 0:
        return False

    for py in range(min_py, max_py + 1):
        for px in range(min_px, max_px + 1):
            lon = (px + 0.5) / img_w * bbox_w + west
            lat = north - (py + 0.5) / img_h * bbox_h

            px_m = (lon - lon_a) * 111_111 * cos_ml
            py_m = (lat - lat_a) * 111_111
            t = (px_m * dlon_m + py_m * dlat_m) / len2_m
            if t < 0.0 or t > 1.0:
                continue

            perp_lon = (lon - lon_a) - t * dlon
            perp_lat = (lat - lat_a) - t * dlat
            perp_m = math.sqrt(
                (perp_lon * 111_111 * math.cos(math.radians(lat))) ** 2
                + (perp_lat * 111_111) ** 2
            )
            if perp_m > eff_clr:
                continue

            r, g, b, alpha = pixels[px, py]
            if alpha < 128:
                continue
            lu = _nearest_type(r, g, b)
            if lu and lu not in _ALLOWED:
                return True

    return False
