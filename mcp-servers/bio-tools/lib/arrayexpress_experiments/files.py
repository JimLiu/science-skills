"""Per-file records and per-sample SDRF rows for ArrayExpress experiments.

Closes the two PARTIAL coverage items vs tooluniverse/arrayexpress:
  arrayexpress_get_experiment_files   -> get_experiment_files(accession)
  arrayexpress_get_experiment_samples -> get_experiment_samples(accession)

Sources:
  * file records: the SAME ``GET /api/v1/studies/{accession}`` submission JSON
    the flattener consumes (file entries with path/size/attributes live in the
    section tree) plus ``GET /api/v1/studies/{accession}/info`` for the
    download endpoints (HTTP/FTP/Globus) and the server's own file count.
  * sample rows: the experiment's SDRF file (tab-delimited), downloaded via
    ``https://www.ebi.ac.uk/biostudies/files/{accession}/{sdrf}`` and parsed
    into one dict per row. MAGE-TAB SDRF headers repeat (e.g. several
    ``Characteristics [x]`` columns) — duplicate header names are
    disambiguated with ``#2``, ``#3``, ... suffixes in document order.
"""
from __future__ import annotations

import csv
import io
import urllib.parse
from typing import Any

from .client import BioStudiesClient, BioStudiesError
from .flatten import _iter_files

FILES_BASE_URL = "https://www.ebi.ac.uk/biostudies/files"


DEFAULT_MAX_SDRF_BYTES = 50_000_000


def _file_record(f: dict[str, Any]) -> dict[str, Any]:
    attrs = {a.get("name"): a.get("value")
             for a in f.get("attributes") or [] if isinstance(a, dict)}
    return {
        "path": f.get("path"),
        "size_bytes": f.get("size"),
        "type": attrs.get("Type"),
        "format": attrs.get("Format"),
        "description": attrs.get("Description"),
    }


def get_experiment_files(
    accession: str,
    client: BioStudiesClient | None = None,
) -> dict[str, Any]:
    """Per-file records (name, size, type, format, description) plus download
    endpoints for one experiment.

    The submission JSON is the authoritative file inventory; /info contributes
    the HTTP/FTP base links and its own independent file count (carried in the
    record so callers — and the accuracy gate — can compare).
    """
    client = client or BioStudiesClient()
    acc = urllib.parse.quote(str(accession), safe="")
    raw = client.get_json(f"studies/{acc}")
    info = client.get_json(f"studies/{acc}/info")

    files = sorted(
        (_file_record(f) for f in _iter_files(raw.get("section", {}) or {})),
        key=lambda r: (r["path"] or ""),
    )
    http_base = info.get("httpLink")
    for rec in files:
        rec["download_url"] = (
            f"{FILES_BASE_URL}/{acc}/{urllib.parse.quote(rec['path'], safe='/')}"
            if rec["path"] else None
        )

    return {
        "accession": raw.get("accno"),
        "n_files": len(files),
        "files": files,
        "info_reported_file_count": info.get("files"),
        "http_link": http_base,
        "ftp_link": info.get("ftpLink"),
        "rel_path": info.get("relPath"),
    }


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        h = h.strip()
        n = seen.get(h, 0) + 1
        seen[h] = n
        out.append(h if n == 1 else f"{h}#{n}")
    return out


def parse_sdrf(text: str) -> dict[str, Any]:
    """Parse SDRF tab-delimited text into header list + row dicts.

    Pure function (offline-testable). Repeated MAGE-TAB headers get
    ``#2``/``#3`` suffixes; rows shorter than the header are padded with
    ``""``; longer rows raise (malformed SDRF must not be silently truncated).
    """
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return {"headers": [], "samples": [], "n_samples": 0}
    headers = _dedupe_headers(rows[0])
    samples = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) > len(headers):
            raise BioStudiesError(
                f"SDRF line {i} has {len(row)} fields but header has "
                f"{len(headers)} — refusing to truncate")
        row = row + [""] * (len(headers) - len(row))
        samples.append(dict(zip(headers, row)))
    return {"headers": headers, "samples": samples, "n_samples": len(samples)}


def get_experiment_samples(
    accession: str,
    client: BioStudiesClient | None = None,
    max_sdrf_bytes: int = DEFAULT_MAX_SDRF_BYTES,
) -> dict[str, Any]:
    """Per-sample SDRF rows for one experiment.

    Locates the SDRF file in the submission JSON (attribute Type == "SDRF
    File"), downloads it from the public files endpoint, and parses every row.
    Experiments without an SDRF (some sequencing submissions keep sample
    metadata in ENA only) return ``{"error": "no_sdrf"}`` rather than raising.

    ``max_sdrf_bytes`` (default 50 MB) guards against pathological downloads;
    the declared size from the submission JSON is checked BEFORE downloading.
    """
    client = client or BioStudiesClient()
    acc = urllib.parse.quote(str(accession), safe="")
    raw = client.get_json(f"studies/{acc}")
    sdrf_files = [
        _file_record(f) for f in _iter_files(raw.get("section", {}) or {})
        if any(a.get("name") == "Type" and a.get("value") == "SDRF File"
               for a in f.get("attributes") or [])
    ]
    if not sdrf_files:
        return {"accession": raw.get("accno"), "error": "no_sdrf",
                "n_samples": 0, "samples": []}
    sdrf = sorted(sdrf_files, key=lambda r: r["path"] or "")[0]
    declared = sdrf.get("size_bytes")
    if isinstance(declared, (int, float)) and declared > max_sdrf_bytes:
        raise BioStudiesError(
            f"SDRF {sdrf['path']} is {declared} bytes (> max_sdrf_bytes="
            f"{max_sdrf_bytes}); raise the cap explicitly to proceed")

    url = f"{FILES_BASE_URL}/{acc}/{urllib.parse.quote(sdrf['path'], safe='/')}"
    text = client.get_text(url)
    parsed = parse_sdrf(text)
    return {
        "accession": raw.get("accno"),
        "sdrf_file": sdrf["path"],
        "sdrf_size_bytes": declared,
        "headers": parsed["headers"],
        "n_samples": parsed["n_samples"],
        "samples": parsed["samples"],
    }
