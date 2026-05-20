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

import requests
from PIL import Image

log = logging.getLogger(__name__)

_WMS     = "https://maps.heigit.org/osmlanduse/wms"
_LAYER   = "osmlanduse:osm_lulc_combined_osm4eo"
_TIMEOUT = 15
_MAP_W   = 512
_MAP_H   = 512
_TTL     = 300   # 5 min

_raster_cache: dict[tuple, tuple] = {}   # bbox_key → (Image RGBA, bytes, expire)
_lock = Lock()


# ── Legend palette ────────────────────────────────────────────────────────────
# Exact colours extracted from the WMS GetLegendGraphic PNG, mapped to short
# lowercase type labels consistent with existing OSM-based obs entries.

_PALETTE: list[tuple[tuple[int, int, int], str]] = [
    ((230,   0,  77), "urban"),          # Urban fabric
    ((255, 255, 168), "farmland"),        # Arable land
    (( 77, 255,   0), "forest"),          # Forests
    ((204,  77, 242), "industrial"),      # Industrial, commercial and transport units
    ((255, 166, 255), "park"),            # Artificial, non-agricultural vegetated areas
    ((166,   0, 204), "quarry"),          # Mine, dump and construction sites
    ((230, 230,  77), "meadow"),          # Pastures
    ((230, 128,   0), "farmland"),        # Permanent crops
    ((  0, 204, 242), "water"),           # Water bodies
    ((230, 230, 230), "bare"),            # Open spaces with little or no vegetation
    ((204, 242,  77), "scrub"),           # Shrub and/or herbaceous vegetation associations
    ((166, 166, 255), "wetland"),         # Wetlands
    ((230, 230, 255), "wetland"),         # Coastal wetlands
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
    params = {
        "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap",
        "LAYERS": _LAYER, "STYLES": "",
        "FORMAT": "image/png", "TRANSPARENT": "TRUE",
        "WIDTH": _MAP_W, "HEIGHT": _MAP_H,
        "BBOX": f"{x0},{y0},{x1},{y1}", "SRS": "EPSG:3857",
    }
    try:
        resp = requests.get(_WMS, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        raw = resp.content
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        log.warning("GetMap failed: %s", exc)
        return None, None

    expire = time.time() + _TTL
    with _lock:
        _raster_cache[key] = (img, raw, expire)
        stale = [k for k, v in _raster_cache.items() if v[2] <= now]
        for k in stale:
            del _raster_cache[k]
    return img, raw


# ── Classification ────────────────────────────────────────────────────────────

def fill_corridor_grid(pairs: list, south: float, west: float,
                       north: float, east: float) -> int:
    """Fill empty corridor-grid points using a single WMS GetMap fetch.

    Classifies by nearest-colour lookup against the legend palette — no
    additional HTTP requests.  Returns the number of points classified.
    """
    img, _ = get_or_fetch_raster(south, west, north, east)
    if img is None:
        return 0

    w, h   = img.size
    lat_span = north - south
    lon_span = east  - west
    filled   = 0

    for pair in pairs:
        for row in (pair.corridor_grid or []):
            for pt in row["points"]:
                if pt["obs"]:
                    continue
                px = min(w - 1, max(0, int((pt["lon"] - west)  / lon_span * w)))
                py = min(h - 1, max(0, int((north - pt["lat"]) / lat_span * h)))
                r, g, b, a = img.getpixel((px, py))
                if a < 10:
                    continue
                t = _nearest_type(r, g, b)
                if t:
                    pt["obs"].append({"source": "osmlanduse", "tags": {"type": t}})
                    filled += 1

    return filled
