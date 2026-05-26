"""Feature classification map loaded from feature_map.csv.

Provides DATA (per-value flags), OVERPASS_DATA (per-key color, derived from CSV),
KEYS, ALL_KEYS, props() and covered() for per-tag lookups.
Consumed by geometry.py (classification) and overpass.py (query building).

CSV wildcard: a row with value="*" means "all unlisted values for this key".
props() falls back to "*" when an exact value is not present.
covered() returns True when a key/value pair has an explicit entry or a "*" wildcard.

Anchor flag: derived solely from anchors.json (presence of a key/value entry
there means anchor=True). The CSV no longer carries an anchor column.
"""
from __future__ import annotations

import csv
import json
import os

_DIR = os.path.dirname(__file__)

# ── Load anchor pairs from anchors.json ──────────────────────────────────────
# Any (key, value) present in the JSON (excluding the "" fallback entry) is
# an anchor point. Entries without an icon ("icon": null or missing) are
# excluded at query time via variations, but their parent key/value still
# counts as an anchor.
try:
    with open(os.path.join(_DIR, "anchors.json"), encoding="utf-8") as _af:
        _anchors_raw: dict = json.load(_af)
    _ANCHOR_PAIRS: frozenset[tuple[str, str]] = frozenset(
        (k, v)
        for k, vals in _anchors_raw.items()
        for v in vals
        if k and v  # skip the "" fallback entry
    )
except Exception:
    _ANCHOR_PAIRS = frozenset()

# ── Load feature classification from CSV ─────────────────────────────────────
try:
    _data: dict[str, dict[str, dict]] = {}
    with open(os.path.join(_DIR, "feature_map.csv"), newline="", encoding="utf-8-sig") as _f:
        for _row in csv.DictReader(_f):
            _key = _row["key"].strip()
            _val = _row["value"].strip()
            _data.setdefault(_key, {})[_val] = {
                "los": _row["los"].strip() == "True",
                "buffer": _row["buffer"].strip() == "True",
                "anchor": (_key, _val) in _ANCHOR_PAIRS,
                "water": _row["water"].strip() == "True",
                "color": _row["color"].strip(),
                "description": _row["description"].strip(),
            }
    # Ensure every anchor entry exists in DATA even if absent from the CSV
    for _ak, _av in _ANCHOR_PAIRS:
        _data.setdefault(_ak, {}).setdefault(_av, {
            "los": False, "buffer": False, "anchor": True,
            "water": False, "color": "", "description": "",
        })
        _data[_ak][_av]["anchor"] = True
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
