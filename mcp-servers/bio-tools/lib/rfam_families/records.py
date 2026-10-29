"""Parsing and canonicalization helpers.

Canonicalization rules (documented in README, enforced here, shared by the
gate and the tool):

* JSON payloads: serialized with ``json.dumps(..., sort_keys=True)`` —
  rfam.org is a Perl/Catalyst app and emits hash keys in nondeterministic
  order run-to-run (observed live), so raw-byte comparison of JSON is invalid.
* regions TSV: comment lines starting with ``#`` are dropped — the header
  embeds the request timestamp ("file built HH:MM:SS DD-Mon-YYYY"), which is
  volatile by construction. Data rows are kept verbatim, in server order
  (observed stable).
* structure mapping: rows sorted by (pdb_id, chain, pdb_start, pdb_end,
  cm_start) — row order in the JSON array is nondeterministic (observed live).
* alignment / CM / tree plain-text payloads: byte-identical, no
  canonicalization (observed stable run-to-run).

None of these rules drop or rewrite scientific content.
"""
from __future__ import annotations

import hashlib
import json


def canonicalize_json(obj) -> str:
    """Deterministic serialization of a JSON-decoded payload."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def family_record(payload: dict) -> dict:
    """Flatten the /family JSON ('rfam' envelope) into a stable record."""
    rfam = payload.get("rfam", payload)
    cur = rfam.get("curation", {}) or {}
    cm = rfam.get("cm", {}) or {}
    rel = rfam.get("release", {}) or {}
    clan = rfam.get("clan", {}) or {}
    return {
        "rfam_acc": rfam.get("acc"),
        "rfam_id": rfam.get("id"),
        "description": rfam.get("description"),
        "comment": rfam.get("comment"),
        "clan_acc": clan.get("acc"),
        "clan_id": clan.get("id"),
        "rna_type": cur.get("type"),
        "structure_source": cur.get("structure_source"),
        "num_seed": _int(cur.get("num_seed")),
        "num_full": _int(cur.get("num_full")),
        "num_species": _int(cur.get("num_species")),
        "gathering_cutoff": _float(cm.get("threshold", {}).get("gathering")
                                   if isinstance(cm.get("threshold"), dict)
                                   else rfam.get("curation", {}).get("ga")),
        "release_number": rel.get("number"),
        "release_date": rel.get("date"),
    }


def _int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None





REGION_COLUMNS = [
    "sequence_accession", "bits_score", "region_start", "region_end",
    "sequence_description", "species", "ncbi_tax_id",
]


def parse_regions(tsv_text: str) -> dict:
    """Parse the /regions plain-text payload.

    Returns ``{"declared_count": int|None, "regions": [dict, ...]}`` where
    ``declared_count`` is the server's own '# found N regions' header value
    (the gate's internal ground truth) and rows keep server order.
    """
    declared = None
    rows = []
    for line in tsv_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            low = line.lower()
            if "found" in low and "region" in low:
                for tok in line.split():
                    if tok.isdigit():
                        declared = int(tok)
                        break
            continue
        parts = line.split("\t")
        row = dict(zip(REGION_COLUMNS, parts))
        rows.append(row)
    return {"declared_count": declared, "regions": rows}


def regions_data_lines(tsv_text: str) -> list[str]:
    """Just the non-comment lines, verbatim (for checksum canonicalization)."""
    return [l for l in tsv_text.splitlines() if l.strip() and not l.startswith("#")]





def parse_stockholm_seq_names(stockholm_text: str) -> list[str]:
    """Unique sequence names from a Stockholm alignment, in first-seen order."""
    names: list[str] = []
    seen = set()
    for line in stockholm_text.splitlines():
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        name = line.split(None, 1)[0]
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def parse_fasta_seq_names(fasta_text: str) -> list[str]:
    return [l[1:].split(None, 1)[0] for l in fasta_text.splitlines()
            if l.startswith(">")]





def parse_cm_header(cm_text: str) -> dict:
    """Key header fields from an Infernal CM file (1.1.x format).

    Reads the CM header block only (stops at the model section 'CM' line).
    The file also embeds the calibrated HMM filter; we only report the
    leading CM header fields, which include NAME/ACC/STATES/NODES/CLEN.
    """
    fields = {}
    wanted = {"NAME", "ACC", "DESC", "STATES", "NODES", "CLEN", "W", "ALPH", "GA", "TC", "NC"}
    for line in cm_text.splitlines():
        if line.strip() == "CM":
            break
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in wanted and parts[0] not in fields:
            fields[parts[0]] = parts[1].strip()
    for k in ("STATES", "NODES", "CLEN", "W"):
        if k in fields:
            try:
                fields[k] = int(fields[k])
            except ValueError:
                pass
    return fields





def sort_structure_mapping(mapping: list[dict]) -> list[dict]:
    """Stable order for the /structures rows (server order is volatile)."""
    return sorted(mapping, key=lambda m: (
        str(m.get("pdb_id")), str(m.get("chain")),
        int(m.get("pdb_start") or 0), int(m.get("pdb_end") or 0),
        int(m.get("cm_start") or 0),
    ))
