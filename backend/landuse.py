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

_MAX_DIST = 30   # pixels further than this from all legend colours are unclassified


def _nearest_type(r: int, g: int, b: int) -> str:
    best_d, best_t = float("inf"), ""
    for (pr, pg, pb), t in _PALETTE:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d, best_t = d, t
    return best_t if best_d <= _MAX_DIST ** 2 else ""


# ── Mercator helpers ──────────────────────────────────────────────────────────

def _to_merc(lat: float, lon: float) -> tuple[float, float]:
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / math.pi * 20037508.34
    return x, y


def _bbox_key(s, w, n, e):
    return (round(s, 3), round(w, 3), round(n, 3), round(e, 3))


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
    n: int = 50,
    clearance_m: float = 0.0,
) -> list[dict]:
    """Sample the landuse raster across the full corridor rectangle.

    Samples a 2-D grid: n points along the line × 7 perpendicular offsets
    spanning ±clearance_m.  For each unique landuse category, records which
    t-indices have that category at any offset, then run-length encodes into
    contiguous segments.  Multiple categories can overlap at the same t range.

    `img` may be a pre-fetched PIL RGBA image; if None the raster is fetched.
    Returns corridor features in the same {category, label, is_blocker, is_water,
    tags, segments} format as get_corridor_features.
    """
    if img is None:
        img, _ = get_or_fetch_raster(south, west, north, east)
    if img is None:
        return []

    pixels = img.load()
    img_w, img_h = img.size
    bbox_w = east - west
    bbox_h = north - south

    mid_lat = (lat_a + lat_b) / 2
    cos_ml = max(math.cos(math.radians(mid_lat)), 0.001)

    # Perpendicular unit vector in degrees
    dlat_m = (lat_b - lat_a) * 111_111
    dlon_m = (lon_b - lon_a) * 111_111 * cos_ml
    length_m = max(math.sqrt(dlat_m ** 2 + dlon_m ** 2), 5.0)
    perp_lat_deg = -dlon_m / length_m / 111_111
    perp_lon_deg =  dlat_m / length_m / (111_111 * cos_ml)

    if clearance_m > 0:
        w_offsets = [clearance_m * k / 3 for k in range(-3, 4)]  # 7 offsets
    else:
        w_offsets = [0.0]

    ts = [i / (n - 1) for i in range(n)]

    # cat_present[lu] is a boolean list indexed by t; True if lu seen at any offset
    cat_present: dict[str, list[bool]] = {}

    for i, t in enumerate(ts):
        clat = lat_a + t * (lat_b - lat_a)
        clon = lon_a + t * (lon_b - lon_a)
        for w in w_offsets:
            lat = clat + w * perp_lat_deg
            lon = clon + w * perp_lon_deg
            px = max(0, min(img_w - 1, int((lon - west) / bbox_w * img_w)))
            py = max(0, min(img_h - 1, int((north - lat) / bbox_h * img_h)))
            r, g, b, a = pixels[px, py]
            lu = _nearest_type(r, g, b) if a >= 128 else ""
            if lu:
                if lu not in cat_present:
                    cat_present[lu] = [False] * n
                cat_present[lu][i] = True

    features: list[dict] = []
    for lu, present in cat_present.items():
        segs: list[dict] = []
        in_seg = False
        start = 0
        for i, hit in enumerate(present):
            if hit and not in_seg:
                in_seg, start = True, i
            elif not hit and in_seg:
                in_seg = False
                segs.append({"t_start": round(ts[start], 3), "t_end": round(ts[i - 1], 3)})
        if in_seg:
            segs.append({"t_start": round(ts[start], 3), "t_end": round(ts[n - 1], 3)})
        if segs:
            features.append({
                "category": "osmlanduse",
                "label": lu.replace("_", " ").capitalize(),
                "name": None,
                "is_blocker": lu not in _ALLOWED,
                "is_water": lu in _WATER,
                "tags": {},
                "segments": segs,
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
