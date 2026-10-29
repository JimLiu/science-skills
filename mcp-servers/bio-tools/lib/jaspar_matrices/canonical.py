"""Shared canonicalization for gate comparisons.

Rules (documented in README; per CONTRACT.md only non-scientific
normalization is allowed):
- recursive key sorting of all JSON objects
- unordered collections sorted by a stable key:
    * lists of matrix rows  -> by matrix_id
    * lists of species rows -> by tax_id
    * other lists of dicts  -> by their full canonical JSON encoding
    * lists of scalars that are row collections are NOT reordered when order
      is scientifically meaningful (PFM base vectors keep positional order)
- floats serialized via repr through json (no rounding)
No fields are dropped: versioned JASPAR matrices are immutable, so every
field is treated as stable.
"""
from __future__ import annotations

import json


def _sort_key(item) -> str:
    if isinstance(item, dict):
        for k in ("matrix_id", "tax_id", "name", "release_number", "url"):
            if k in item:
                return f"{k}={item[k]}"
        return json.dumps(item, sort_keys=True)
    return str(item)


def _canon(obj):
    if isinstance(obj, dict):
        return {k: _canon(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return sorted((_canon(x) for x in obj), key=_sort_key)

        return [_canon(x) for x in obj]
    return obj


def canonicalize(record) -> bytes:
    """Canonical, byte-stable JSON encoding of a tool output record."""
    return json.dumps(_canon(record), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode()
