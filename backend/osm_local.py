"""Local OSM source: a Geofabrik extract cut into tiles with osmium-tool, instead of Overpass.

Setup / refresh (downloads ~630 MB, then filters it once):
    python -m backend.osm_local

Requests outside the extract's polygon (or before setup) fall back to Overpass.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from threading import Lock

import requests
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from . import feature_map as _fm
from .dem import CACHE_ROOT

REGION = os.environ.get("SPOTLINES_OSM_REGION", "europe/italy/nord-est")  # Geofabrik path
_NAME = REGION.rsplit("/", 1)[-1]
_URL = f"https://download.geofabrik.de/{REGION}/{_NAME}"
_DIR = CACHE_ROOT / "osm" / "geofabrik"
_KEYS = sorted(_fm.ALL_KEYS)
_KEYHASH = hashlib.sha1(",".join(_KEYS).encode()).hexdigest()[:8]
_PBF = _DIR / f"{_NAME}-latest.osm.pbf"
_POLY = _DIR / f"{_NAME}.poly"
_FILTERED = _DIR / f"{_NAME}-{_KEYHASH}.osm.pbf"   # only objects with feature_map keys (+ refs, untagged)
_SPANS = _DIR / f"{_NAME}-{_KEYHASH}-spans.json"
_CELLS = _DIR / f"{_NAME}-{_KEYHASH}-cells"           # cut lazily from _FILTERED, 0.5° each
_CELL_DEG = 0.5
# ponytail: one global lock; every osmium extract needs ~3.5 GB RAM (ID sets sized by max node ID)
_CELL_LOCK = Lock()

# osmium picks ways by their nodes; Overpass by geometry. A cable car or power line can cross a
# tile with both ends outside it, so ways with long segments are added by geometric intersection.
# ponytail: only aerialway/power are checked; other keys rarely have segments longer than this.
# Same gap for areas: a multipolygon enclosing the bbox with no node inside is missed. Seen only
# for forests (non-blocking); urban blockers are caught by the landuse raster anyway.
_SPAN_KEYS = ("aerialway", "power")
_SPAN_MIN_M = 150


def available() -> bool:
    return _FILTERED.exists() and _POLY.exists() and _SPANS.exists()


def covers(south: float, west: float, north: float, east: float) -> bool:
    return available() and _region().contains(box(west, south, east, north))


def extract(south: float, west: float, north: float, east: float) -> list[dict]:
    """Overpass-shaped elements for a bbox: like `nwr[keys](bbox); out body; >; out skel qt;`."""
    def idx(deg: float) -> int:
        return math.floor(deg / _CELL_DEG + 1e-9)
    cells = [_cell(i, j) for i in range(idx(south), idx(north) + 1) for j in range(idx(west), idx(east) + 1)]
    cmd = ["osmium", "extract", "-s", "smart", "-S", "types=any",
           "-b", f"{west},{south},{east},{north}",
           "-f", "opl,add_metadata=false", "-o", "-", "--no-progress"]
    if len(cells) == 1:
        opl = subprocess.run([*cmd, str(cells[0])], check=True, capture_output=True, text=True).stdout
    else:  # bbox crosses cell edges: merge dedupes shared objects (smart can't read stdin)
        with tempfile.TemporaryDirectory(dir=_CELLS) as tmp:
            merged = Path(tmp) / "merged.osm.pbf"
            _osmium("merge", *map(str, cells), "-o", str(merged))
            opl = subprocess.run([*cmd, str(merged)], check=True, capture_output=True, text=True).stdout
    elements = parse_opl(opl)
    seen = {(e["type"], e["id"]) for e in elements}
    tree, ways, nodes = _spans()
    for i in tree.query(box(west, south, east, north), predicate="intersects"):
        way = ways[int(i)]
        if ("way", way["id"]) in seen:
            continue
        elements.append(way)
        for nid in way["nodes"]:
            if ("node", nid) not in seen:
                seen.add(("node", nid))
                elements.append(nodes[nid])
    return elements


def _cell(i: int, j: int) -> Path:
    """Cut (once) a cell of the filtered region. smart + types=any keeps every way and relation
    touching the cell complete, so any bbox inside it extracts the same as from the full region."""
    path = _CELLS / f"{i}_{j}.osm.pbf"
    with _CELL_LOCK:
        if not path.exists():
            s, w = i * _CELL_DEG, j * _CELL_DEG
            _CELLS.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            _osmium("extract", "-s", "smart", "-S", "types=any",
                    "-b", f"{w},{s},{w + _CELL_DEG},{s + _CELL_DEG}", str(_FILTERED),
                    "-f", "pbf", "-o", str(tmp))
            tmp.replace(path)
    return path


# ── OPL parsing ───────────────────────────────────────────────────────────────

_ESC = re.compile(r"%([0-9a-fA-F]+)%")


def _unesc(s: str) -> str:
    return _ESC.sub(lambda m: chr(int(m.group(1), 16)), s)


def _tags(field: str) -> dict:
    return dict(_unesc(kv).split("=", 1) for kv in field.split(",")) if field else {}


def parse_opl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if not line:
            continue
        fields = {f[0]: f[1:] for f in line.split(" ")}
        kind = line[0]
        oid = int(fields[kind])
        tags = _tags(fields.get("T", ""))
        if kind == "n":
            el = {"type": "node", "id": oid, "lat": float(fields["y"]), "lon": float(fields["x"])}
        elif kind == "w":
            nds = fields.get("N", "")
            el = {"type": "way", "id": oid, "nodes": [int(n[1:]) for n in nds.split(",")] if nds else []}
        elif kind == "r":
            ms = fields.get("M", "")
            el = {"type": "relation", "id": oid, "members": [
                {"type": {"n": "node", "w": "way", "r": "relation"}[m[0]],
                 "ref": int(m[1:m.index("@")]), "role": _unesc(m[m.index("@") + 1:])}
                for m in ms.split(",")] if ms else []}
        else:
            continue
        if tags:
            el["tags"] = tags
        out.append(el)
    return out


# ── Region polygon & span index ───────────────────────────────────────────────

@lru_cache(maxsize=1)
def _region():
    """Parse a Geofabrik .poly file: rings of 'lon lat' lines; '!' rings are holes."""
    outers, holes, ring, name = [], [], None, None
    for line in _POLY.read_text().splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        if line == "END":
            if ring is None:
                break
            (holes if name.startswith("!") else outers).append(Polygon(ring))
            ring = None
        elif ring is None:
            name, ring = line, []
        else:
            lon, lat = map(float, line.split()[:2])
            ring.append((lon, lat))
    return unary_union(outers).difference(unary_union(holes)) if holes else unary_union(outers)


@lru_cache(maxsize=1)
def _spans():
    data = json.loads(_SPANS.read_text())
    nodes = {n["id"]: n for n in data["nodes"]}
    lines = [LineString([(nodes[n]["lon"], nodes[n]["lat"]) for n in w["nodes"]]) for w in data["ways"]]
    return STRtree(lines), data["ways"], nodes


def _long_span_ways(elements: list[dict]) -> dict:
    nodes = {e["id"]: e for e in elements if e["type"] == "node"}
    keep = []
    for w in elements:
        if w["type"] != "way" or len(w["nodes"]) < 2 or any(n not in nodes for n in w["nodes"]):
            continue
        pts = [nodes[n] for n in w["nodes"]]
        mid_cos = math.cos(math.radians(pts[0]["lat"]))
        if any(math.hypot(b["lat"] - a["lat"], (b["lon"] - a["lon"]) * mid_cos) * 111_111 > _SPAN_MIN_M
               for a, b in zip(pts, pts[1:])):
            keep.append(w)
    used = {n for w in keep for n in w["nodes"]}
    return {"ways": keep, "nodes": [nodes[n] for n in used]}


# ── Setup ─────────────────────────────────────────────────────────────────────

def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)


def _osmium(*args: str) -> None:
    subprocess.run(["osmium", *args, "--no-progress", "-O"], check=True)


def setup(refresh: bool = False) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    if refresh or not _PBF.exists():
        print(f"Downloading {_URL}-latest.osm.pbf …", flush=True)
        _download(f"{_URL}-latest.osm.pbf", _PBF)
    if refresh or not _POLY.exists():
        _download(f"{_URL}.poly", _POLY)
    if refresh or not _FILTERED.exists():
        print(f"Filtering to {len(_KEYS)} feature_map keys …", flush=True)
        _osmium("tags-filter", "-t", str(_PBF), *[f"nwr/{k}" for k in _KEYS], "-o", str(_FILTERED))
        shutil.rmtree(_CELLS, ignore_errors=True)  # cut from the old filtered file
    if refresh or not _SPANS.exists():
        print("Indexing long-span ways …", flush=True)
        opl = subprocess.run(
            ["osmium", "tags-filter", "-t", str(_FILTERED), *[f"w/{k}" for k in _SPAN_KEYS],
             "-f", "opl,add_metadata=false", "-o", "-", "--no-progress"],
            check=True, capture_output=True, text=True,
        ).stdout
        _SPANS.write_text(json.dumps(_long_span_ways(parse_opl(opl))))
    print(f"Local OSM ready: {_FILTERED}", flush=True)


if __name__ == "__main__":
    import sys
    setup(refresh="--refresh" in sys.argv)
