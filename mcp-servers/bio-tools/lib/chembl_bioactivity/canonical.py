"""Canonicalization shared by the equivalence gate (legacy vs modern records).

Allowed canonicalizations only (per contract): key ordering, stable sorting of
unordered collections, dropping documented client-only / volatile keys.
Scientific content (values, identifiers, metadata) is never altered.
"""
from __future__ import annotations

import json





CLIENT_ONLY_KEYS: frozenset[str] = frozenset()

SORT_KEYS = {
    "molecule": "molecule_chembl_id",
    "activity": "activity_id",
    "mechanism": "mec_id",
}


def drop_keys(record: dict, keys: frozenset[str] = CLIENT_ONLY_KEYS) -> dict:
    """Return a copy of *record* without the given top-level keys."""
    return {k: v for k, v in record.items() if k not in keys}


def canonicalize_record(record: dict, drop: frozenset[str] = CLIENT_ONLY_KEYS) -> bytes:
    """Canonical byte representation of one record.

    JSON with sorted keys, compact separators, no NaN, UTF-8. Nested structures
    are preserved as-is except that lists of dicts with a recognizable id key
    are NOT re-ordered here (ChEMBL returns them deterministically).
    """
    return json.dumps(
        drop_keys(record, drop),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def canonicalize_records(records: list[dict], resource: str, drop: frozenset[str] = CLIENT_ONLY_KEYS) -> list[bytes]:
    """Canonicalize a record collection: sort by the resource's id key, then per-record bytes."""
    sort_key = SORT_KEYS[resource]
    ordered = sorted(records, key=lambda r: (r.get(sort_key) is None, r.get(sort_key)))
    return [canonicalize_record(r, drop) for r in ordered]


def index_by(records: list[dict], key: str) -> dict:
    """Index records by an id key, asserting uniqueness."""
    out: dict = {}
    for r in records:
        k = r.get(key)
        if k in out:
            raise ValueError(f"duplicate key {k!r} for index field {key!r}")
        out[k] = r
    return out
