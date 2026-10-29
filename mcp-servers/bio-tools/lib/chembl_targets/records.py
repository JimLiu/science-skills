"""Record construction, canonicalization, and tabular output for chembl-targets.

All ordering rules live here so that every retrieval path (single fetch, batched
fetch, reverse accession lookup, declarative search) produces byte-identical
records for the same upstream content.

Ordering rules (documented in the README):
  * components sorted by component_id (ascending, None last)
  * component synonyms de-duplicated and sorted by (syn_type, synonym)
  * protein classifications sorted by protein_class_id
  * cross references sorted by (xref_src, xref_id)
  * canonical bytes = JSON with sorted keys, compact separators, UTF-8
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


TARGET_SCALAR_FIELDS = (
    "target_chembl_id",
    "pref_name",
    "target_type",
    "organism",
    "tax_id",
    "species_group_flag",
)


COMPONENT_SCALAR_FIELDS = (
    "accession",
    "component_id",
    "component_type",
    "component_description",
    "relationship",
)

TABLE_COLUMNS = (
    "query",
    "target_chembl_id",
    "pref_name",
    "target_type",
    "organism",
    "tax_id",
    "component_accession",
    "component_id",
    "component_relationship",
    "component_description",
    "protein_class_path",
)


def build_component_record(
    raw_component: dict[str, Any],
    classifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a structured component record from a nested target_component dict.

    ``classifications`` is the (already resolved) list of protein classification
    records for this component, or None when classification was not requested.
    """
    rec: dict[str, Any] = {k: raw_component.get(k) for k in COMPONENT_SCALAR_FIELDS}
    synonyms = raw_component.get("target_component_synonyms") or []
    dedup = sorted(
        {
            (str(s.get("syn_type") or ""), str(s.get("component_synonym") or ""))
            for s in synonyms
        }
    )
    rec["synonyms"] = [{"syn_type": a, "synonym": b} for a, b in dedup]
    if classifications is not None:
        rec["protein_classifications"] = sorted(
            classifications,
            key=lambda c: (c.get("protein_class_id") is None, c.get("protein_class_id")),
        )
    return rec


def build_target_record(
    raw_target: dict[str, Any],
    component_classifications: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build a structured target record from a raw API target dict.

    ``component_classifications`` maps component_id -> list of classification
    records ({protein_class_id, pref_name, class_level, path}).  When None,
    classification fields are omitted entirely (not requested).
    """
    rec: dict[str, Any] = {k: raw_target.get(k) for k in TARGET_SCALAR_FIELDS}

    raw_components = raw_target.get("target_components") or []
    components = []
    for raw_comp in sorted(
        raw_components,
        key=lambda c: (c.get("component_id") is None, c.get("component_id")),
    ):
        cls = None
        if component_classifications is not None:
            cls = component_classifications.get(raw_comp.get("component_id"), [])
        components.append(build_component_record(raw_comp, cls))
    rec["component_count"] = len(components)
    rec["components"] = components

    xrefs = raw_target.get("cross_references") or []
    rec["cross_references"] = sorted(
        (
            {
                "xref_src": x.get("xref_src"),
                "xref_id": x.get("xref_id"),
                "xref_name": x.get("xref_name"),
            }
            for x in xrefs
        ),
        key=lambda x: (str(x["xref_src"]), str(x["xref_id"])),
    )
    return rec


def canonicalize(obj: Any) -> bytes:
    """Canonical bytes of any JSON-serializable structure (sorted keys, compact, UTF-8)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_of(obj: Any) -> str:
    """SHA-256 hex digest of the canonical bytes of ``obj``."""
    return hashlib.sha256(canonicalize(obj)).hexdigest()


def _class_path(component: dict[str, Any]) -> str | None:
    classifications = component.get("protein_classifications")
    if classifications is None:
        return None
    return " | ".join(str(c.get("path") or "") for c in classifications)


def records_to_table(records: Iterable[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    """Flatten structured target records into tidy rows (one per target-component).

    Targets with zero components (e.g. UNCHECKED targets) produce a single row
    with empty component fields so they are never silently dropped.
    """
    rows: list[dict[str, Any]] = []
    for rec in records:
        components = rec.get("components") or [None]
        for comp in components:
            rows.append(
                {
                    "query": query,
                    "target_chembl_id": rec.get("target_chembl_id"),
                    "pref_name": rec.get("pref_name"),
                    "target_type": rec.get("target_type"),
                    "organism": rec.get("organism"),
                    "tax_id": rec.get("tax_id"),
                    "component_accession": comp.get("accession") if comp else None,
                    "component_id": comp.get("component_id") if comp else None,
                    "component_relationship": comp.get("relationship") if comp else None,
                    "component_description": comp.get("component_description") if comp else None,
                    "protein_class_path": _class_path(comp) if comp else None,
                }
            )
    return rows


def table_to_tsv(rows: Iterable[dict[str, Any]]) -> str:
    """Serialize tidy rows to TSV (header + one line per row)."""

    def fmt(value: Any) -> str:
        if value is None:
            return ""
        return str(value).replace("\t", " ").replace("\n", " ")

    lines = ["\t".join(TABLE_COLUMNS)]
    for row in rows:
        lines.append("\t".join(fmt(row.get(col)) for col in TABLE_COLUMNS))
    return "\n".join(lines) + "\n"
