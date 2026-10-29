"""Deterministic structured summaries for entry-centric InterPro results.

Canonicalization rules (documented in README §4, implemented ONLY here):

* JSON serialized with sorted keys, compact separators, ensure_ascii.
* Unordered collections stably sorted: search/listing rows by accession;
  GO terms by identifier; member-DB signatures by (database, accession);
  clan member nodes by accession.
* Volatile/presentation fields dropped from summaries: ``wikipedia``,
  ``description`` (rich HTML blobs), ``counters`` (cross-release volatile),
  ``next``/``previous`` URLs (pagination plumbing).
* NO scientific content is dropped or rewritten: accessions, names, types,
  integration status, GO identifiers, clan membership, organism/taxonomy,
  protein lengths and coordinates pass through verbatim.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj) -> str:
    """The single canonical serialization used everywhere (gate digests, output)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _sorted_go(go_terms) -> list | None:
    if not go_terms:
        return None
    return sorted(go_terms, key=lambda g: g.get("identifier", ""))


def _sorted_member_dbs(member_databases) -> list | None:
    """Flatten {db: {acc: name}} into a sorted list of signature dicts."""
    if not member_databases:
        return None
    flat = [
        {"database": db, "accession": acc, "name": name}
        for db, sigs in member_databases.items()
        for acc, name in (sigs or {}).items()
    ]
    return sorted(flat, key=lambda s: (s["database"], s["accession"]))


def summarize_entry_row(row: dict) -> dict:
    """One row of an /entry/ listing -> stable summary record."""
    md = row.get("metadata", row)
    out = {
        "accession": md.get("accession"),
        "name": md.get("name"),
        "type": md.get("type"),
        "source_database": md.get("source_database"),
        "integrated": md.get("integrated"),
    }
    sigs = _sorted_member_dbs(md.get("member_databases"))
    if sigs is not None:
        out["member_db_signatures"] = sigs
    go = _sorted_go(md.get("go_terms"))
    if go is not None:
        out["go_terms"] = go
    return out


def summarize_entry_detail(payload: dict) -> dict:
    """An /entry/{db}/{acc}/ detail payload -> stable summary record."""
    md = payload.get("metadata", {})
    name = md.get("name")
    if isinstance(name, dict):
        out_name = {"name": name.get("name"), "short": name.get("short")}
    else:
        out_name = {"name": name, "short": None}
    out = {
        "accession": md.get("accession"),
        "name": out_name,
        "type": md.get("type"),
        "source_database": md.get("source_database"),
        "integrated": md.get("integrated"),
        "hierarchy": md.get("hierarchy"),
        "set_info": md.get("set_info"),
    }
    sigs = _sorted_member_dbs(md.get("member_databases"))
    if sigs is not None:
        out["member_db_signatures"] = sigs
    go = _sorted_go(md.get("go_terms"))
    if go is not None:
        out["go_terms"] = go
    if md.get("literature"):
        out["n_literature_refs"] = len(md["literature"])
    return out


def summarize_search(fetched: dict) -> dict:
    """A complete search/listing walk -> stable summary (rows sorted by accession)."""
    rows = [summarize_entry_row(r) for r in fetched.get("results") or []]
    rows.sort(key=lambda r: r["accession"] or "")
    return {"count": fetched.get("count", 0), "results": rows}


def summarize_clan_detail(payload: dict) -> dict:
    """A /set/pfam/{acc}/ detail payload -> stable summary; members sorted."""
    md = payload.get("metadata", {})
    name = md.get("name")
    if isinstance(name, dict):
        name = name.get("name")
    nodes = (md.get("relationships") or {}).get("nodes") or []
    members = sorted(
        (
            {
                "accession": n.get("accession"),
                "name": n.get("name"),
                "short_name": n.get("short_name"),
                "type": n.get("type"),
            }
            for n in nodes
        ),
        key=lambda n: n["accession"] or "",
    )
    return {
        "accession": md.get("accession"),
        "name": name,
        "source_database": md.get("source_database"),
        "member_count": len(members),
        "members": members,
    }


def summarize_clan_row(row: dict) -> dict:
    md = row.get("metadata", row)
    name = md.get("name")
    if isinstance(name, dict):
        name = name.get("name")
    return {
        "accession": md.get("accession"),
        "name": name,
        "source_database": md.get("source_database"),
    }


def summarize_clan_search(fetched: dict) -> dict:
    rows = [summarize_clan_row(r) for r in fetched.get("results") or []]
    rows.sort(key=lambda r: r["accession"] or "")
    return {"count": fetched.get("count", 0), "results": rows}


def summarize_protein_row(row: dict) -> dict:
    md = row.get("metadata", row)
    org = md.get("source_organism") or {}
    return {
        "accession": md.get("accession"),
        "name": md.get("name"),
        "source_database": md.get("source_database"),
        "length": md.get("length"),
        "tax_id": org.get("taxId"),
        "organism": org.get("scientificName"),
    }


def summarize_proteins(fetched: dict) -> dict:
    if fetched.get("results") is None:
        return {"count": fetched.get("count", 0), "results": None}
    rows = [summarize_protein_row(r) for r in fetched["results"]]
    rows.sort(key=lambda r: r["accession"] or "")
    return {"count": fetched.get("count", 0), "results": rows}


def summarize_proteome_row(row: dict) -> dict:
    md = row.get("metadata", row)
    return {
        "accession": md.get("accession"),
        "name": md.get("name"),
        "is_reference": md.get("is_reference"),
        "taxonomy": md.get("taxonomy"),
    }


def summarize_proteomes(fetched: dict) -> dict:
    if fetched.get("results") is None:
        return {"count": fetched.get("count", 0), "results": None}
    rows = [summarize_proteome_row(r) for r in fetched["results"]]
    rows.sort(key=lambda r: r["accession"] or "")
    return {"count": fetched.get("count", 0), "results": rows}
