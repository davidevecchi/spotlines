"""Feature classification map loaded from feature_map.csv.

Provides DATA (per-value flags), OVERPASS_DATA (per-key color, derived from CSV),
KEYS, ALL_KEYS, props() and covered() for per-tag lookups.
Consumed by geometry.py (classification) and overpass.py (query building).

CSV wildcard: a row with value="*" means "all unlisted values for this key".
props() falls back to "*" when an exact value is not present.
covered() returns True when a key/value pair has an explicit entry or a "*" wildcard.
"""
from __future__ import annotations

import csv
import os

_DIR = os.path.dirname(__file__)

try:
    _data: dict[str, dict[str, dict]] = {}
    with open(os.path.join(_DIR, "feature_map.csv"), newline="", encoding="utf-8-sig") as _f:
        for _row in csv.DictReader(_f):
            _key = _row["key"].strip()
            _val = _row["value"].strip()
            _data.setdefault(_key, {})[_val] = {
                "los": _row["los"].strip() == "True",
                "buffer": _row["buffer"].strip() == "True",
                "anchor": _row["anchor"].strip() == "True",
                "water": _row["water"].strip() == "True",
                "color": _row["color"].strip(),
                "description": _row["description"].strip(),
            }
    DATA: dict[str, dict[str, dict]] = _data
except Exception:
    DATA = {}


OVERPASS_DATA: dict[str, dict] = {}
for k, d in DATA.items():
    for v, vv in d.items():
        OVERPASS_DATA[f'{k}:{v}'] = {"color": vv.get("color") or "#64748b"}

KEYS: frozenset[str] = frozenset(DATA)
OVERPASS_KEYS: frozenset[str] = frozenset()
ALL_KEYS: frozenset[str] = KEYS


def props(key: str, value: str) -> dict:
    """Return classification properties for an OSM key=value pair, or {}.

    Falls back to the "*" wildcard row when the exact value is not listed.
    """
    kd = DATA.get(key, {})
    return kd.get(value) or kd.get("*") or {}


def covered(key: str, value: str) -> bool:
    """Return True if key/value has an explicit entry or a "*" wildcard row."""
    kd = DATA.get(key, {})
    return value in kd or "*" in kd
