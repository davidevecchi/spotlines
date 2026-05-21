"""Terrain classification via osmlanduse.org WMS.

One GetMap PNG per bbox covers both the /landuse/image overlay and point
classification.  Classification is a local nearest-colour lookup against the
hardcoded legend palette — zero extra HTTP requests after the initial fetch.

Legend extracted from:
  GetLegendGraphic?layer=osmlanduse:osm_lulc_combined_osm4eo&bgColor=0xFF0000
"""
from __future__ import annotations

import io
import logging
import math
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
_MAP_W   = 512
_MAP_H   = 512
_TTL     = 300   # 5 min

_raster_cache: dict[tuple, tuple] = {}   # bbox_key → (Image RGBA, bytes, expire)
_lock = Lock()


# ── Legend palette ────────────────────────────────────────────────────────────
# Exact colours extracted from the WMS GetLegendGraphic PNG, mapped to short
# lowercase type labels consistent with existing OSM-based obs entries.

_ALLOWED: frozenset[str] = frozenset({
    "arable", "forest", "park", "meadow", "scrub",
    "water", "bare", "wetland", "coastal_wetland",
})

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
        img = Image.composite(classic, filled, classic.getchannel("A"))

    raw = io.BytesIO()
    img.save(raw, format="PNG")
    raw = raw.getvalue()

    expire = time.time() + _TTL
    with _lock:
        _raster_cache[key] = (img, raw, expire)
        stale = [k for k, v in _raster_cache.items() if v[2] <= now]
        for k in stale:
            del _raster_cache[k]
    return img, raw



def sample_landuse_along_line(
    lat_a: float, lon_a: float,
    lat_b: float, lon_b: float,
    south: float, west: float, north: float, east: float,
    img: "Image | None" = None,
    n: int = 50,
) -> list[dict]:
    """Sample the landuse raster along the centerline.

    `img` may be a pre-fetched PIL RGBA image; if None the raster is fetched.
    Returns corridor features in the same {category, label, is_blocker, is_water,
    tags, segments} format as get_corridor_features.  Consecutive samples with
    the same classification are merged into a single segment.
    """
    if img is None:
        img, _ = get_or_fetch_raster(south, west, north, east)
    if img is None:
        return []

    pixels = img.load()
    img_w, img_h = img.size
    bbox_w = east - west
    bbox_h = north - south

    types: list[str] = []
    for i in range(n):
        t = i / (n - 1)
        lat = lat_a + t * (lat_b - lat_a)
        lon = lon_a + t * (lon_b - lon_a)
        px = int((lon - west) / bbox_w * img_w)
        py = int((north - lat) / bbox_h * img_h)
        px = max(0, min(img_w - 1, px))
        py = max(0, min(img_h - 1, py))
        r, g, b, a = pixels[px, py]
        types.append(_nearest_type(r, g, b) if a >= 128 else "")

    ts = [i / (n - 1) for i in range(n)]
    features: list[dict] = []
    seen: dict[str, int] = {}

    i = 0
    while i < n:
        lu = types[i]
        if not lu:
            i += 1
            continue
        j = i + 1
        while j < n and types[j] == lu:
            j += 1
        seg = {"t_start": round(ts[i], 3), "t_end": round(ts[j - 1], 3)}
        if lu in seen:
            features[seen[lu]]["segments"].append(seg)
        else:
            seen[lu] = len(features)
            features.append({
                "category": "landuse",
                "label": lu.replace("_", " ").capitalize(),
                "is_blocker": lu not in _ALLOWED,
                "is_water": lu in {"water", "wetland", "coastal_wetland"},
                "tags": {},
                "segments": [seg],
            })
        i = j

    return features


