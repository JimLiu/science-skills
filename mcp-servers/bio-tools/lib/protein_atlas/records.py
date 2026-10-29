"""Stable field subset, summary shaping and canonicalization.

The per-gene JSON is a flat dict of ~119 human-readable keys. ~40 of those
are per-cancer prognostics keys ("Cancer prognostics - <cancer>"); the rest
cover identity, expression specificity per compartment, subcellular
location, secretome, antibodies and reliability scores. summarize() groups
the stable subset into sections mirroring the knowledgebase MCP's
tissue/subcellular/pathology/blood/brain + antibody scope.

Canonicalization (documented per contract, implemented once here):
  * JSON with sorted keys, compact separators, UTF-8, ensure_ascii=False
  * lists whose elements are all strings are sorted (unordered collections:
    Uniprot accessions, antibody IDs, location/class/process terms)
  * no scientific content is dropped or rewritten
"""
from __future__ import annotations

import json

PROGNOSTICS_PREFIX = "Cancer prognostics - "



SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "identity": (
        "Gene", "Gene synonym", "Ensembl", "Gene description", "Uniprot",
        "Chromosome", "Position", "Protein class", "Biological process",
        "Molecular function", "Disease involvement", "Evidence",
        "HPA evidence", "UniProt evidence", "NeXtProt evidence",
    ),
    "tissue_expression": (
        "RNA tissue specificity", "RNA tissue distribution",
        "RNA tissue specificity score", "RNA tissue specific nTPM",
        "RNA tissue cell type enrichment", "Tissue expression cluster",
        "Protein tissue specificity", "Protein tissue distribution",
        "Protein tissue specificity score", "Protein tissue specific Intensity",
    ),
    "single_cell_expression": (
        "RNA single cell type specificity", "RNA single cell type distribution",
        "RNA single cell type specificity score",
        "RNA single cell type specific nCPM", "Single cell expression cluster",
    ),
    "blood_expression": (
        "RNA blood cell specificity", "RNA blood cell distribution",
        "RNA blood cell specificity score", "RNA blood cell specific nTPM",
        "RNA blood lineage specificity", "RNA blood lineage distribution",
        "RNA blood lineage specific nTPM", "Blood expression cluster",
        "Blood concentration - Conc. blood IM [pg/L]",
        "Blood concentration - Conc. blood MS [pg/L]",
    ),
    "brain_expression": (
        "RNA brain regional specificity", "RNA brain regional distribution",
        "RNA brain regional specificity score",
        "RNA brain regional specific nTPM", "Brain expression cluster",
    ),
    "cancer_expression": (
        "RNA cancer specificity", "RNA cancer distribution",
        "RNA cancer specificity score", "RNA cancer specific pTPM",
    ),
    "subcellular": (
        "Subcellular location", "Subcellular main location",
        "Subcellular additional location", "Reliability (IF)",
        "Secretome location", "Secretome function",
        "CCD Protein", "CCD Transcript",
    ),
    "antibody": (
        "Antibody", "Antibody RRID", "Reliability (IH)",
        "Reliability (Mouse Brain)", "Reliability (IF)",
    ),
}


def summarize(record: dict) -> dict:
    """Group the stable subset of a per-gene record into sections.

    Keys absent from the record are omitted (HPA omits rather than nulls
    some fields for some genes); present-but-null values are kept, so the
    summary distinguishes "not reported" from "reported as null".
    The ~40 per-cancer prognostics keys are folded into
    summary["pathology"]["prognostics"][<cancer>].
    """
    summary: dict = {}
    for section, names in SUMMARY_FIELDS.items():
        block = {k: record[k] for k in names if k in record}
        summary[section] = block
    prognostics = {
        k[len(PROGNOSTICS_PREFIX):]: v
        for k, v in record.items() if k.startswith(PROGNOSTICS_PREFIX)
    }
    summary["pathology"] = {"prognostics": prognostics}
    return summary


def _sort_string_lists(obj):
    if isinstance(obj, dict):
        return {k: _sort_string_lists(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [_sort_string_lists(v) for v in obj]
        if items and all(isinstance(v, str) for v in items):
            return sorted(items)
        return items
    return obj


def canonicalize(obj) -> bytes:
    """Canonical UTF-8 JSON bytes for equality checks (rules in module doc)."""
    return json.dumps(
        _sort_string_lists(obj), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
