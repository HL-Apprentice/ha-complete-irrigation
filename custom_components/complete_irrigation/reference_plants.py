"""Outdoor Dream Builder plant pages, stored as printed.

Pages fetched 2026-11-07 from https://www.outdoordreambuilder.com/plants.
usda_zone is the Quick Stats USDA range. When the narrative prints a different
range, that text is hardiness_alt and hardiness_note. lookup_reference only
returns these page records. The operational merge lives in odb_care.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

REFERENCE_PATH = Path(__file__).resolve().parent / "reference" / "outdoordreambuilder_plants.json"

_RECORDS: list[dict[str, Any]] | None = None
_INDEX: dict[str, dict[str, Any]] | None = None


def _load() -> list[dict[str, Any]]:
    """Load and index the dataset once. Records stay private to this module."""
    global _RECORDS, _INDEX
    if _RECORDS is not None and _INDEX is not None:
        return _RECORDS
    raw = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{REFERENCE_PATH.name} must be a list of plant records")
    index: dict[str, dict[str, Any]] = {}
    for rec in raw:
        if not isinstance(rec, dict):
            raise ValueError(f"{REFERENCE_PATH.name} contains a non-object record")
        for field in ("common_name", "botanical_name"):
            name = rec.get(field)
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"{REFERENCE_PATH.name} record is missing {field}")
            key = name.strip().casefold()
            previous = index.get(key)
            if previous is not None and previous is not rec:
                raise ValueError(f"duplicate reference name: {name}")
            index[key] = rec
    _RECORDS = raw
    _INDEX = index
    return _RECORDS


def load_reference() -> list[dict[str, Any]]:
    """Return a deep copy of every reference record, in source order."""
    return copy.deepcopy(_load())


def lookup_reference(name: str | None) -> dict[str, Any] | None:
    """Return a copy of the record for a common or botanical name.

    The match is exact and case-insensitive after stripping surrounding
    whitespace. An unknown name, a blank, or a non-string returns None.
    The copy is independent of the stored record.
    """
    if not isinstance(name, str):
        return None
    key = name.strip().casefold()
    if not key:
        return None
    _load()
    assert _INDEX is not None
    rec = _INDEX.get(key)
    if rec is None:
        return None
    return copy.deepcopy(rec)
