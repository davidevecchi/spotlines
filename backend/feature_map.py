"""Feature classification map loaded from osm-features-checklist-export.json.

Provides DATA (the full map), KEYS (top-level OSM keys present), and props()
for per-tag lookups. Consumed by geometry.py (classification) and overpass.py
(query building).
"""
from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "feature_map.json")

try:
    with open(_PATH) as _f:
        DATA: dict[str, dict[str, dict]] = json.load(_f)
except Exception:
    DATA = {}

KEYS: frozenset[str] = frozenset(DATA)


def props(key: str, value: str) -> dict:
    """Return classification properties for an OSM key=value pair, or {}."""
    return DATA.get(key, {}).get(value, {})
