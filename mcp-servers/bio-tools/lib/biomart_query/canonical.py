"""Shared canonicalization for the equivalence gate.

Both sides of the gate (legacy pybiomart reference TSV and modern
biomart_query TSV) are passed through :func:`canonicalize` before byte
comparison. Allowed transformations only:

1. **Column-name mapping** — pybiomart's default ``Dataset.query()`` labels
   columns with BioMart *display* names ("Gene stable ID"); the modern tool
   labels them with *internal* attribute names (``ensembl_gene_id``). Display
   names are mapped to internal names using the dataset's own attribute
   metadata (captured alongside the reference) with a frozen fallback map.
2. **Column order** — columns are re-ordered to the battery's attribute-set
   order.
3. **Cell normalization** — pandas type-inference artifacts are undone:
   integral floats are rendered as integers (``1.0`` -> ``1``) and missing
   values (empty cells / pandas NaN written as empty) are the empty string.
   Surrounding whitespace is stripped.
4. **Row order** — rows are sorted lexicographically by the canonical row
   tuple (gene ID is the leading column in every battery item).
5. **Line endings / trailing whitespace** — ``\\n`` endings, no trailing blank
   lines, UTF-8 bytes.

No scientific content (identifiers, coordinates, biotypes, strand) is dropped
or rewritten.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence





DISPLAY_TO_INTERNAL = {
    "Gene stable ID": "ensembl_gene_id",
    "Gene name": "external_gene_name",
    "Chromosome/scaffold name": "chromosome_name",
    "Gene start (bp)": "start_position",
    "Gene end (bp)": "end_position",
    "Strand": "strand",
    "Gene type": "gene_biotype",
    "Transcript stable ID": "ensembl_transcript_id",
    "Ensembl Canonical": "transcript_is_canonical",
}

_INTEGRAL_FLOAT = re.compile(r"^-?\d+\.0+$")


def normalize_cell(value: str) -> str:
    """Normalize a single TSV cell (see module docstring, rule 3)."""
    v = value.strip()
    if v == "":
        return ""
    if _INTEGRAL_FLOAT.match(v):
        return v.split(".", 1)[0]
    return v


def canonicalize(
    tsv_text: str,
    column_order: Sequence[str],
    display_map: Mapping[str, str] | None = None,
) -> bytes:
    """Canonicalize a TSV table (header + rows) to comparable bytes.

    ``column_order`` is the battery attribute-set list (internal names).
    ``display_map`` maps display names -> internal names; the frozen
    :data:`DISPLAY_TO_INTERNAL` is always consulted as a fallback.
    """
    lines = [ln for ln in tsv_text.replace("\r\n", "\n").split("\n") if ln != ""]
    if not lines:
        raise ValueError("empty TSV: no header line")
    header = lines[0].split("\t")
    mapping = dict(DISPLAY_TO_INTERNAL)
    if display_map:
        mapping.update(display_map)
    mapped = [mapping.get(name.strip(), name.strip()) for name in header]
    missing = [c for c in column_order if c not in mapped]
    if missing:
        raise ValueError(f"TSV is missing required columns {missing}; has {mapped}")
    indices = [mapped.index(c) for c in column_order]

    rows = []
    for line in lines[1:]:
        fields = line.split("\t")
        if len(fields) != len(header):
            raise ValueError(
                f"malformed TSV row: expected {len(header)} fields, got {len(fields)}: {line[:200]!r}"
            )
        rows.append([normalize_cell(fields[i]) for i in indices])
    rows.sort()

    out_lines = ["\t".join(column_order)]
    out_lines.extend("\t".join(row) for row in rows)
    return ("\n".join(out_lines) + "\n").encode("utf-8")


def diff_summary(ref_bytes: bytes, mod_bytes: bytes, max_examples: int = 5) -> dict:
    """Human-readable summary of how two canonical tables differ."""
    ref_lines = ref_bytes.decode("utf-8").rstrip("\n").split("\n")
    mod_lines = mod_bytes.decode("utf-8").rstrip("\n").split("\n")
    ref_set = set(ref_lines[1:])
    mod_set = set(mod_lines[1:])
    only_ref = sorted(ref_set - mod_set)
    only_mod = sorted(mod_set - ref_set)
    return {
        "header_equal": ref_lines[0] == mod_lines[0],
        "reference_rows": len(ref_lines) - 1,
        "modern_rows": len(mod_lines) - 1,
        "rows_only_in_reference": len(only_ref),
        "rows_only_in_modern": len(only_mod),
        "examples_only_in_reference": only_ref[:max_examples],
        "examples_only_in_modern": only_mod[:max_examples],
    }
