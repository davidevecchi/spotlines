"""DEM caching (GLO-30 / TINItaly), elevation sampling, slope tile serving.

Cache layout:
  CACHE_ROOT/dem/tinitaly/N46_E011.tif   1°×1° at 10 m, WGS-84
  CACHE_ROOT/dem/glo30/N46_E011.tif      1°×1° at 30 m, WGS-84
  CACHE_ROOT/slope/{z}/{x}/{y}.png       derived, safe to wipe
"""
from __future__ import annotations

import io
import math
import os
import time
import warnings
from pathlib import Path
from threading import Lock
from typing import Optional

import numpy as np
import requests
from PIL import Image
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.windows import from_bounds as win_from_bounds
from rasterio.warp import calculate_default_transform, reproject
from rasterio.warp import Resampling as WarpResampling

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")

CACHE_ROOT = Path(os.environ.get("SPOTLINES_CACHE_DIR", "/media/twister/WD/.cache/spotfinder"))
_DEM_DIR   = CACHE_ROOT / "dem"

_CRS_WGS84 = CRS.from_epsg(4326)

# Italy bounding box: (south, west, north, east)
_ITALY = (35.5, 6.6, 47.1, 18.8)

_JET = [   # YlOrRd: yellow (flat) → red (steep)
    (0.000, (255, 255, 178)),
    (0.250, (254, 204,  92)),
    (0.500, (253, 141,  60)),
    (0.750, (240,  59,  32)),
    (1.000, (189,   0,  38)),
]
_MAX_SLOPE_DEG = 45.0


# ── Coordinate helpers ────────────────────────────────────────────────────────

def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) WGS-84 for Leaflet tile z/x/y."""
    n = 2 ** z
    west  = x / n * 360 - 180
    east  = (x + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _tile_name(lat_f: int, lon_f: int) -> str:
    ls = f"N{lat_f:02d}" if lat_f >= 0 else f"S{abs(lat_f):02d}"
    lo = f"E{lon_f:03d}" if lon_f >= 0 else f"W{abs(lon_f):03d}"
    return f"{ls}_{lo}"


def _tile_intersects_italy(lat_f: int, lon_f: int) -> bool:
    return (
        lat_f + 1 > _ITALY[0] and lat_f < _ITALY[2] and
        lon_f + 1 > _ITALY[1] and lon_f < _ITALY[3]
    )



# ── DEM download & disk cache ─────────────────────────────────────────────────

def _save_as_wgs84(src: rasterio.DatasetReader, dest: Path) -> None:
    """Write src to dest as a deflate-compressed WGS-84 GeoTIFF, reprojecting if needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.crs == _CRS_WGS84:
        profile = src.profile.copy()
        profile.update(driver="GTiff", compress="deflate", predictor=2)
        with rasterio.open(dest, "w", **profile) as dst:
            dst.write(src.read())
    else:
        transform, width, height = calculate_default_transform(
            src.crs, _CRS_WGS84, src.width, src.height, *src.bounds,
        )
        profile = src.profile.copy()
        profile.update(
            crs=_CRS_WGS84, transform=transform, width=width, height=height,
            driver="GTiff", compress="deflate", predictor=2,
        )
        with rasterio.open(dest, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_crs=src.crs,
                dst_crs=_CRS_WGS84,
                resampling=WarpResampling.bilinear,
            )


def _download_glo30(lat_f: int, lon_f: int, dest: Path) -> None:
    """Stream one 1°×1° GLO-30 tile from the Copernicus AWS COG and cache it."""
    ls = f"N{lat_f:02d}" if lat_f >= 0 else f"S{abs(lat_f):02d}"
    lo = f"E{lon_f:03d}" if lon_f >= 0 else f"W{abs(lon_f):03d}"
    name = f"Copernicus_DSM_COG_10_{ls}_00_{lo}_00_DEM"
    url = f"/vsicurl/https://copernicus-dem-30m.s3.eu-central-1.amazonaws.com/{name}/{name}.tif"
    with rasterio.open(url) as src:
        _save_as_wgs84(src, dest)
    print(f"DEM cached: glo30/{dest.name}", flush=True)


def _download_tinitaly(lat_f: int, lon_f: int, dest: Path) -> bool:
    """Download one 1°×1° TINItaly tile via WCS and cache it. Returns True on success."""
    qs = (
        "SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
        "&COVERAGEID=TINItaly_1_1__tinitaly_dem"
        "&SUBSETTINGCRS=EPSG:4326"
        f"&SUBSET=Long({float(lon_f)},{float(lon_f + 1)})"
        f"&SUBSET=Lat({float(lat_f)},{float(lat_f + 1)})"
        "&FORMAT=image/tiff"
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            resp = requests.get(
                f"https://tinitaly.pi.ingv.it/TINItaly_1_1/wcs?{qs}",
                timeout=300,
                verify=False,  # TINItaly server uses a self-signed cert; GLO-30 fallback is used if this fails
            )
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "tiff" not in ct and "octet-stream" not in ct:
            return False
        with rasterio.open(io.BytesIO(resp.content)) as src:
            _save_as_wgs84(src, dest)
        print(f"DEM cached: tinitaly/{dest.name}", flush=True)
        return True
    except Exception as exc:
        print(f"TINItaly fetch failed ({lat_f},{lon_f}): {exc}", flush=True)
        if dest.exists():
            dest.unlink()
        return False


def _get_dem_tile(lat_f: int, lon_f: int) -> Optional[Path]:
    """
    Return path to a cached 1°×1° DEM tile, downloading it on first access.
    Prefers TINItaly (10 m) for cells that overlap Italy, falls back to GLO-30 (30 m).
    Returns None only if both sources fail or the cache root is unavailable.
    """
    if not _DEM_DIR.exists():
        return None
    name = _tile_name(lat_f, lon_f)

    if _tile_intersects_italy(lat_f, lon_f):
        tini = _DEM_DIR / "tinitaly" / f"{name}.tif"
        if tini.exists():
            return tini
        if _download_tinitaly(lat_f, lon_f, tini):
            return tini

    glo = _DEM_DIR / "glo30" / f"{name}.tif"
    if glo.exists():
        return glo
    try:
        _download_glo30(lat_f, lon_f, glo)
        return glo
    except Exception as exc:
        print(f"GLO-30 fetch failed ({lat_f},{lon_f}): {exc}", flush=True)
        return None


# ── Public elevation API (used by elevation.py) ───────────────────────────────

def dem_cache_available() -> bool:
    """Return True if the local DEM cache directory exists and is accessible."""
    return _DEM_DIR.exists()


def get_elevation_at_points(
    lats: list[float], lons: list[float],
) -> list[Optional[float]]:
    """
    Sample terrain elevation (m) at arbitrary WGS-84 points from the DEM cache.
    Returns a list parallel to the inputs; None where data is unavailable.
    All tiles are guaranteed to be in WGS-84 (ensured at download time).
    """
    result: list[Optional[float]] = [None] * len(lats)

    # Group point indices by the 1°×1° tile they fall in
    groups: dict[tuple[int, int], list[int]] = {}
    for i, (lat, lon) in enumerate(zip(lats, lons)):
        key = (int(math.floor(lat)), int(math.floor(lon)))
        groups.setdefault(key, []).append(i)

    for (lat_f, lon_f), indices in groups.items():
        tile_path = _get_dem_tile(lat_f, lon_f)
        if tile_path is None:
            continue
        try:
            with rasterio.open(tile_path) as ds:
                nodata = ds.nodata
                coords = [(lons[i], lats[i]) for i in indices]
                for i, val_arr in zip(indices, ds.sample(coords, indexes=1)):
                    val = float(val_arr[0])
                    if nodata is None or val != nodata:
                        result[i] = val
        except Exception as exc:
            print(f"Elevation sampling failed ({lat_f},{lon_f}): {exc}", flush=True)

    return result


# ── Slope tile serving ────────────────────────────────────────────────────────

def _gaussian_blur(arr: np.ndarray, sigma: float) -> np.ndarray:
    """NaN-safe Gaussian blur using scipy.ndimage (2-5× faster than apply_along_axis)."""
    from scipy.ndimage import gaussian_filter
    filled  = np.where(np.isnan(arr), 0.0, arr)
    weights = np.where(np.isnan(arr), 0.0, 1.0)
    blurred_data    = gaussian_filter(filled,  sigma=sigma)
    blurred_weights = gaussian_filter(weights, sigma=sigma)
    return np.where(blurred_weights > 0, blurred_data / blurred_weights, np.nan).astype(np.float32)


def _compute_slope(elev: np.ndarray, dy_m: float, dx_m: float) -> np.ndarray:
    """Return slope in degrees; dy_m/dx_m are metres per pixel in row/col directions."""
    dy, dx = np.gradient(elev)
    dy = np.where(np.isfinite(dy), dy / dy_m, 0.0)
    dx = np.where(np.isfinite(dx), dx / dx_m, 0.0)
    return np.degrees(np.arctan(np.sqrt(dx ** 2 + dy ** 2))).astype(np.float32)


def _slope_to_rgba(slope: np.ndarray) -> np.ndarray:
    """Convert slope (degrees) to an H×W×4 uint8 RGBA array using the YlOrRd colourmap.

    Color and alpha both use the absolute 0–45° scale (clamped above 45°).
    Alpha: 0.1 at 0°, 0.9 at 45°+, linear.
    """
    t_color = np.clip(slope / _MAX_SLOPE_DEG, 0.0, 1.0)
    t_alpha = np.clip(slope / _MAX_SLOPE_DEG, 0.0, 1.0)
    rgb = np.zeros((*slope.shape, 3), dtype=np.float32)
    for seg in range(len(_JET) - 1):
        t0, c0 = _JET[seg]
        t1, c1 = _JET[seg + 1]
        mask = (t_color >= t0) & (t_color <= t1 if seg == len(_JET) - 2 else t_color < t1)
        f = np.where(mask, (t_color - t0) / (t1 - t0), 0.0)
        for k in range(3):
            rgb[:, :, k] += np.where(mask, c0[k] + f * (c1[k] - c0[k]), 0.0)
    rgba = np.zeros((*slope.shape, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[:, :, 3] = np.round((0.1 + t_alpha * 0.8) * 255).astype(np.uint8)
    return rgba


def _rgba_to_png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _empty_image(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()



_dem_array_cache: dict[tuple, tuple] = {}  # (s,w,n,e,max_px,sigma) → (elev, slope, W, H, expire)
_dem_array_lock = Lock()
_DEM_ARRAY_TTL = 60  # seconds — DEM tiles don't change; short TTL to avoid stale memory


def _load_slope_data(
    south: float, west: float, north: float, east: float, max_px: int,
    blur_sigma: float = 0.5,
) -> Optional[tuple[np.ndarray, np.ndarray, int, int]]:
    """Load DEM tiles and compute (elev, slope, W, H), or None if no data.

    Results are cached for _DEM_ARRAY_TTL seconds so that simultaneous calls for
    slope image, elevation image, stats, and contours on the same bbox only open
    rasterio files once.
    """
    cache_key = (round(south, 5), round(west, 5), round(north, 5), round(east, 5), max_px, blur_sigma)
    now = time.time()
    with _dem_array_lock:
        entry = _dem_array_cache.get(cache_key)
        if entry and entry[4] > now:
            return entry[0], entry[1], entry[2], entry[3]
    lat_m = (north - south) * 111_111.0
    lon_m = (east - west) * 111_111.0 * math.cos(math.radians((south + north) / 2.0))
    if lat_m >= lon_m:
        H = max_px
        W = max(1, round(max_px * lon_m / lat_m))
    else:
        W = max_px
        H = max(1, round(max_px * lat_m / lon_m))
    dy_m = lat_m / H
    dx_m = lon_m / W

    opened: list[rasterio.DatasetReader] = []
    try:
        for lat in range(int(math.floor(south)), int(math.floor(north)) + 1):
            for lon in range(int(math.floor(west)), int(math.floor(east)) + 1):
                p = _get_dem_tile(lat, lon)
                if p:
                    try:
                        opened.append(rasterio.open(p))
                    except Exception:
                        pass
        if not opened:
            return None
        arrays: list[np.ndarray] = []
        for ds in opened:
            win = win_from_bounds(west, south, east, north, ds.transform)
            arr = ds.read(1, window=win, out_shape=(H, W),
                          resampling=Resampling.cubic_spline).astype(np.float32)
            if ds.nodata is not None:
                arr[arr == ds.nodata] = np.nan
            arrays.append(arr)
        elev = np.nanmean(np.stack(arrays, axis=0), axis=0) if len(arrays) > 1 else arrays[0]
        if blur_sigma > 0:
            elev = _gaussian_blur(elev, sigma=blur_sigma)
        slope = _compute_slope(elev, dy_m, dx_m)
        now2 = time.time()
        expire = now2 + _DEM_ARRAY_TTL
        with _dem_array_lock:
            _dem_array_cache[cache_key] = (elev, slope, W, H, expire)
            stale = [k for k, v in _dem_array_cache.items() if v[4] <= now2]
            for k in stale:
                del _dem_array_cache[k]
        return elev, slope, W, H
    finally:
        for ds in opened:
            try:
                ds.close()
            except Exception:
                pass


def get_slope_image(
    south: float, west: float, north: float, east: float,
    max_px: int = 1024,
) -> bytes:
    """Render a slope heatmap PNG covering the exact bbox."""
    result = _load_slope_data(south, west, north, east, max_px)
    if result is None:
        return _empty_image(256, 256)
    _, slope, _, _ = result
    return _rgba_to_png(_slope_to_rgba(slope))


def get_slope_stats(
    south: float, west: float, north: float, east: float,
) -> tuple[float, float]:
    """Return (min_deg, max_deg) of slope in the bbox (at reduced resolution for speed)."""
    result = _load_slope_data(south, west, north, east, max_px=256)
    if result is None:
        return 0.0, 0.0
    _, slope, _, _ = result
    valid = slope[np.isfinite(slope)]
    if not len(valid):
        return 0.0, 0.0
    return round(float(np.min(valid)), 1), round(float(np.max(valid)), 1)


_ELEV_CMAP_LO, _ELEV_CMAP_HI = 0.22, 1.0  # skip ocean blues
_GIST_EARTH = None  # lazy: loaded on first elevation image request


def _get_gist_earth():
    global _GIST_EARTH
    if _GIST_EARTH is None:
        from matplotlib import colormaps
        _GIST_EARTH = colormaps["gist_earth"]
    return _GIST_EARTH


def _elev_to_rgba(elev: np.ndarray, vmin: float, vmax: float) -> np.ndarray:
    """Map elevation array to RGBA using gist_earth (land portion) scaled to [vmin, vmax]."""
    rng = vmax - vmin if vmax > vmin else 1.0
    t_norm = np.clip((elev - vmin) / rng, 0.0, 1.0)
    t_mapped = _ELEV_CMAP_LO + t_norm * (_ELEV_CMAP_HI - _ELEV_CMAP_LO)
    rgba_f = _get_gist_earth()(t_mapped)
    rgba = (rgba_f * 255).astype(np.uint8)
    rgba[:, :, 3] = np.where(np.isfinite(elev), 255, 0).astype(np.uint8)
    return rgba


def get_elevation_image(
    south: float, west: float, north: float, east: float,
    max_px: int = 1024,
) -> bytes:
    """Render an elevation heatmap PNG using the terrain colourmap scaled to actual range."""
    result = _load_slope_data(south, west, north, east, max_px, blur_sigma=0.0)
    if result is None:
        return _empty_image(256, 256)
    elev, slope, W, H = result
    valid = elev[np.isfinite(elev)]
    if not len(valid):
        return _empty_image(W, H)
    vmin, vmax = float(np.min(valid)), float(np.max(valid))
    elev_f = _elev_to_rgba(elev, vmin, vmax).astype(np.float32)
    slope_f = _slope_to_rgba(slope).astype(np.float32)
    # Alpha-composite slope over elevation; slope alpha already encodes steepness
    a = slope_f[:, :, 3:4] / 255.0
    blended = elev_f.copy()
    blended[:, :, :3] = elev_f[:, :, :3] * (1.0 - a) + slope_f[:, :, :3] * a
    return _rgba_to_png(blended.astype(np.uint8))


def get_elevation_stats(
    south: float, west: float, north: float, east: float,
) -> tuple[float, float]:
    """Return (min_m, max_m) of elevation in the bbox."""
    result = _load_slope_data(south, west, north, east, max_px=256, blur_sigma=0.0)
    if result is None:
        return 0.0, 0.0
    elev, _, _, _ = result
    valid = elev[np.isfinite(elev)]
    if not len(valid):
        return 0.0, 0.0
    return round(float(np.min(valid)), 0), round(float(np.max(valid)), 0)


_CONTOUR_MINOR_M = 5.0
_CONTOUR_MAJOR_M = 50.0


def get_contour_svg(south: float, west: float, north: float, east: float) -> str:
    """Return an SVG string with elevation contour polylines for the bbox."""
    result = _load_slope_data(south, west, north, east, max_px=2048, blur_sigma=0.0)
    _EMPTY = '<svg xmlns="http://www.w3.org/2000/svg"/>'
    if result is None:
        return _EMPTY
    elev, _, W, H = result

    valid = elev[np.isfinite(elev)]
    if not len(valid):
        return _EMPTY

    lo, hi = float(np.min(valid)), float(np.max(valid))
    level_lo = math.ceil(lo / _CONTOUR_MINOR_M) * _CONTOUR_MINOR_M
    levels = []
    v = level_lo
    while v <= hi + 1e-6:
        levels.append(round(v, 6))
        v += _CONTOUR_MINOR_M
    if not levels:
        return _EMPTY

    from matplotlib.figure import Figure
    fig = Figure()
    ax = fig.add_subplot(111)
    cs = ax.contour(elev, levels=levels)

    minor_segs, major_segs = [], []
    for level, segs in zip(levels, cs.allsegs):
        target = major_segs if abs(level % _CONTOUR_MAJOR_M) < 0.01 else minor_segs
        for seg in segs:
            if len(seg) < 2:
                continue
            target.append('<polyline points="' + ' '.join(f'{x:.1f},{y:.1f}' for x, y in seg) + '"/>')

    _G = 'stroke="#A0522D" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
        f'<g id="c-minor" {_G} stroke-width="2" opacity="1">{"".join(minor_segs)}</g>'
        f'<g id="c-major" {_G} stroke-width="4" opacity="1">{"".join(major_segs)}</g>'
        '</svg>'
    )
