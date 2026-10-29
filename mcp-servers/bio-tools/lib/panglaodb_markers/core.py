"""Parse + query layer over the cached PanglaoDB marker table.

All queries are served from the locally cached, checksum-verified TSV; no
network traffic happens after the first download.
"""
from __future__ import annotations

import csv
import gzip
import os
import pathlib

from .client import fetch_markers_gz

COLUMNS = [
    "species",
    "official gene symbol",
    "cell type",
    "nicknames",
    "ubiquitousness index",
    "product description",
    "gene type",
    "canonical marker",
    "germ layer",
    "organ",
    "sensitivity_human",
    "sensitivity_mouse",
    "specificity_human",
    "specificity_mouse",
]

NUMERIC_COLUMNS = [
    "ubiquitousness index",
    "sensitivity_human",
    "sensitivity_mouse",
    "specificity_human",
    "specificity_mouse",
]

_SENS_COL = {"Hs": "sensitivity_human", "Mm": "sensitivity_mouse"}
_SPEC_COL = {"Hs": "specificity_human", "Mm": "specificity_mouse"}


def _to_float(value: str):
    """'NA' / '' -> None, else float."""
    if value is None:
        return None
    v = value.strip()
    if v in ("", "NA", "NaN", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse_markers(tsv_gz_path: str | os.PathLike) -> list[dict]:
    """Parse the gzipped TSV into a list of row dicts.

    Rows are preserved near-verbatim. Known upstream artifacts in the frozen
    2020 file, handled as follows:
      - 3 rows with species == '4' and 30 rows with species == 'None'
        (corrupt/unknown species): kept verbatim and surfaced in options().
      - organ == 'NA' (73 rows): normalized to None (missing organ), so organ
        enumeration returns only the 29 real organs.
    Numeric columns are converted to float with 'NA' -> None.
    """
    rows: list[dict] = []
    with gzip.open(tsv_gz_path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames != COLUMNS:
            raise ValueError(
                f"unexpected header: {reader.fieldnames!r} (expected {COLUMNS!r})"
            )
        for raw in reader:
            row = dict(raw)
            for col in NUMERIC_COLUMNS:
                row[col] = _to_float(row[col])
            if row["organ"] == "NA":
                row["organ"] = None
            rows.append(row)
    return rows


def _species_tokens(field: str) -> set[str]:
    return set((field or "").split())


def _row_sort_key(row: dict):
    return (row["cell type"], row["official gene symbol"], row["species"])


class PanglaoDB:
    """In-memory index over the PanglaoDB marker table.

    Construct once; every query is a pure in-memory filter (deterministic,
    stable sort order). Pass ``tsv_gz_path`` to run fully offline from a
    pre-downloaded file; otherwise the file is fetched (once) into the cache.
    """

    def __init__(
        self,
        tsv_gz_path: str | os.PathLike | None = None,
        *,
        cache_dir: str | os.PathLike | None = None,
        verify_checksum: bool = True,
    ):
        if tsv_gz_path is None:
            tsv_gz_path = fetch_markers_gz(cache_dir, verify_checksum=verify_checksum)
        self.path = pathlib.Path(tsv_gz_path)
        self.rows = parse_markers(self.path)


    def marker_genes(
        self,
        cell_type: str | None = None,
        organ: str | None = None,
        species: str | None = None,
        sensitivity_min: float | None = None,
        specificity_max: float | None = None,
        canonical_only: bool = False,
    ) -> list[dict]:
        """Filtered marker rows, sorted by (cell type, gene symbol, species).

        - ``cell_type`` / ``organ``: case-insensitive exact match.
        - ``species``: 'Hs' or 'Mm'; matches rows whose species field contains
          that token (so 'Mm Hs' rows match either).
        - ``sensitivity_min`` / ``specificity_max``: thresholds on the
          species-specific columns; when ``species`` is None they apply to the
          human columns (documented default). Rows with missing (NA) values in
          the thresholded column are excluded.
        - ``canonical_only``: keep rows flagged canonical marker == '1'.
        """
        if species is not None and species not in _SENS_COL:
            raise ValueError("species must be 'Hs' or 'Mm'")
        sens_col = _SENS_COL[species or "Hs"]
        spec_col = _SPEC_COL[species or "Hs"]
        ct = cell_type.lower() if cell_type is not None else None
        og = organ.lower() if organ is not None else None

        out = []
        for row in self.rows:
            if ct is not None and row["cell type"].lower() != ct:
                continue
            if og is not None and (row["organ"] or "").lower() != og:
                continue
            if species is not None and species not in _species_tokens(row["species"]):
                continue
            if sensitivity_min is not None:
                v = row[sens_col]
                if v is None or v < sensitivity_min:
                    continue
            if specificity_max is not None:
                v = row[spec_col]
                if v is None or v > specificity_max:
                    continue
            if canonical_only and row["canonical marker"] != "1":
                continue
            out.append(row)
        return sorted(out, key=_row_sort_key)


    def options(self) -> dict:
        """Distinct species / organs / cell types (+ cell types per organ).

        Values are reported verbatim from the upstream table, including the
        known upstream artifact species value '4' on three corrupt rows.
        """
        species = sorted({r["species"] for r in self.rows if r["species"]})
        organs = sorted({r["organ"] for r in self.rows if r["organ"]})
        cell_types = sorted({r["cell type"] for r in self.rows if r["cell type"]})
        by_organ: dict[str, list[str]] = {}
        for organ in organs:
            by_organ[organ] = sorted(
                {r["cell type"] for r in self.rows if r["organ"] == organ}
            )
        return {
            "species": species,
            "organs": organs,
            "cell_types": cell_types,
            "n_organs": len(organs),
            "n_cell_types": len(cell_types),
            "cell_types_by_organ": by_organ,
        }


    def cell_types_for_gene(
        self, symbol: str, *, include_synonyms: bool = False
    ) -> list[dict]:
        """Rows whose official gene symbol (optionally any nickname) matches.

        Match is case-insensitive. Returns the full marker rows plus a
        ``matched_via`` key ('official symbol' or 'synonym'), sorted.
        """
        want = symbol.strip().lower()
        out = []
        for row in self.rows:
            if row["official gene symbol"].lower() == want:
                hit = dict(row)
                hit["matched_via"] = "official symbol"
                out.append(hit)
            elif include_synonyms:
                nicks = [n.strip().lower() for n in (row["nicknames"] or "").split("|")]
                if want in nicks and want != "na":
                    hit = dict(row)
                    hit["matched_via"] = "synonym"
                    out.append(hit)
        return sorted(out, key=_row_sort_key)
