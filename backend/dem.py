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
import warnings
from pathlib import Path
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

CACHE_ROOT = Path("/media/pi/WD/.cache/spotfinder")
_DEM_DIR   = CACHE_ROOT / "dem"
_SLOPE_DIR = CACHE_ROOT / "slope"

_CRS_WGS84 = CRS.from_epsg(4326)

# Italy bounding box: (south, west, north, east)
_ITALY = (35.5, 6.6, 47.1, 18.8)

_PLASMA = [
    (0.00, (13,   8, 135)),
    (0.25, (126,  3, 168)),
    (0.50, (204, 71, 120)),
    (0.75, (248, 148,  65)),
    (1.00, (240, 249,  33)),
]
_MAX_SLOPE_DEG = 45.0
_TILE_SIZE = 256


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


def _pixel_size_m(west: float, south: float, east: float, north: float) -> float:
    clat = math.radians((south + north) / 2)
    lat_m = (north - south) * math.pi / 180 * 6_371_000
    lon_m = (east - west) * math.pi / 180 * 6_371_000 * math.cos(clat)
    return ((lat_m + lon_m) / 2) / _TILE_SIZE


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
        "&COVERAGEID=TINItaly_1_1_10m"
        "&SUBSETTINGCRS=EPSG:4326"
        f"&SUBSET=Lon({float(lon_f)},{float(lon_f + 1)})"
        f"&SUBSET=Lat({float(lat_f)},{float(lat_f + 1)})"
        "&FORMAT=image/tiff"
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            resp = requests.get(
                f"https://tinitaly.pi.ingv.it/TINItaly_1_1/wcs?{qs}",
                timeout=300,
                verify=False,
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
    Returns None only if both sources fail.
    """
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

def _compute_slope(elev: np.ndarray, pixel_size_m: float) -> np.ndarray:
    dy, dx = np.gradient(elev)
    dy = np.where(np.isfinite(dy), dy / pixel_size_m, 0.0)
    dx = np.where(np.isfinite(dx), dx / pixel_size_m, 0.0)
    return np.degrees(np.arctan(np.sqrt(dx ** 2 + dy ** 2))).astype(np.float32)


def _slope_to_png(slope: np.ndarray) -> bytes:
    t = np.clip(slope / _MAX_SLOPE_DEG, 0.0, 1.0)
    rgb = np.zeros((*slope.shape, 3), dtype=np.float32)
    for seg in range(len(_PLASMA) - 1):
        t0, c0 = _PLASMA[seg]
        t1, c1 = _PLASMA[seg + 1]
        mask = (t >= t0) & (t <= t1 if seg == len(_PLASMA) - 2 else t < t1)
        f = np.where(mask, (t - t0) / (t1 - t0), 0.0)
        for k in range(3):
            rgb[:, :, k] += np.where(mask, c0[k] + f * (c1[k] - c0[k]), 0.0)
    rgba = np.zeros((*slope.shape, 4), dtype=np.uint8)
    rgba[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[:, :, 3] = np.where(slope < 1.0, 0, np.clip(t * 200, 0, 200)).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def _empty_tile() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (_TILE_SIZE, _TILE_SIZE), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def get_slope_tile(z: int, x: int, y: int) -> bytes:
    """
    Return slope-heatmap PNG for Leaflet tile z/x/y.
    Reads from cached DEM tiles; result cached at CACHE_ROOT/slope/{z}/{x}/{y}.png.
    """
    cache_path = _SLOPE_DIR / str(z) / str(x) / f"{y}.png"
    if cache_path.exists():
        return cache_path.read_bytes()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    west, south, east, north = tile_bbox(z, x, y)
    pixel_size_m = _pixel_size_m(west, south, east, north)

    S = _TILE_SIZE
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
            png = _empty_tile()
            cache_path.write_bytes(png)
            return png

        arrays: list[np.ndarray] = []
        for ds in opened:
            win = win_from_bounds(west, south, east, north, ds.transform)
            arr = ds.read(1, window=win, out_shape=(S, S),
                          resampling=Resampling.bilinear).astype(np.float32)
            if ds.nodata is not None:
                arr[arr == ds.nodata] = np.nan
            arrays.append(arr)

        elev = np.nanmean(np.stack(arrays, axis=0), axis=0) if len(arrays) > 1 else arrays[0]
        slope = _compute_slope(elev, pixel_size_m)
        png = _slope_to_png(slope)
        cache_path.write_bytes(png)
        return png
    finally:
        for ds in opened:
            try:
                ds.close()
            except Exception:
                pass
