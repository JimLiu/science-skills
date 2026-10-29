"""Structured cross-database link retrieval via NCBI E-utilities elink.

Key design points (TARGETS_WAVE3B.md B47):
- one id= parameter per input UID (NOT a comma-joined list), so elink returns one
  linkset per UID and per-UID attribution is preserved;
- explicit per-UID empty-link reporting (a UID with no links to the target db is a
  record with has_links=False, never silently dropped);
- linkname enumeration via einfo (the authoritative registry of link names);
- deterministic output: target UID lists deduplicated and sorted numerically,
  linknames sorted, records in input-UID order.
"""
from __future__ import annotations

import json
from typing import Sequence

from .client import EUtilsClient


def canonical_json(obj) -> str:
    """Compact, key-sorted JSON used for run-to-run identity checks and token counts."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _uid_sort_key(uid: str) -> tuple[int, str]:

    return (len(uid), uid) if uid.isdigit() else (10**9, uid)


def _as_str_ids(ids: Sequence[int | str]) -> list[str]:
    out = []
    for i in ids:
        s = str(i).strip()
        if not s:
            raise ValueError("empty UID in id list")
        if not s.isdigit():
            raise ValueError(
                f"elink UIDs must be numeric Entrez UIDs, got {s!r} "
                "(resolve accessions to UIDs first; for dbSNP strip the 'rs' prefix)")
        out.append(s)
    return out


def parse_linksets(payload: dict, requested_ids: Sequence[str], dbfrom: str, db: str) -> list[dict]:
    """Convert an elink retmode=json payload (per-UID request form) into per-UID records.

    Returns one record per requested UID, in input order. UIDs with no links to the
    target db are reported explicitly (has_links=False, links={}).
    Raises ValueError if the payload is in the merged (comma-joined id) form, because
    per-UID attribution is impossible there.
    """
    linksets = payload.get("linksets", []) or []
    by_uid: dict[str, list[dict]] = {}
    for ls in linksets:
        ids = [str(x) for x in (ls.get("ids", []) or [])]
        if len(ids) > 1:
            raise ValueError(
                f"linkset covers {len(ids)} input UIDs ({ids[:5]}...); per-UID attribution requires "
                "the repeated id= request form (one UID per id parameter), not a comma-joined id list")
        if ids:
            by_uid.setdefault(ids[0], []).append(ls)

    records = []
    for uid in requested_ids:
        links: dict[str, list[str]] = {}
        notes: list[str] = []
        for ls in by_uid.get(uid, []):
            for lsdb in ls.get("linksetdbs", []) or []:
                name = str(lsdb.get("linkname", ""))
                target_ids = [str(x) for x in (lsdb.get("links", []) or [])]
                if name in links:
                    target_ids = list(set(links[name]) | set(target_ids))
                else:
                    target_ids = list(set(target_ids))
                links[name] = sorted(target_ids, key=_uid_sort_key)
            err = ls.get("ERROR") or ls.get("error")
            if err:
                notes.append(f"elink ERROR: {err}")
        if uid not in by_uid:
            notes.append("no linkset returned for this UID")
        link_counts = {k: len(links[k]) for k in sorted(links)}
        record = {
            "source_db": dbfrom,
            "source_uid": uid,
            "target_db": db,
            "links": {k: links[k] for k in sorted(links)},
            "link_counts": link_counts,
            "total_links": sum(link_counts.values()),
            "has_links": bool(link_counts),
        }
        if notes:
            record["notes"] = notes
        records.append(record)
    return records


def elink_links(dbfrom: str, db: str, ids: Sequence[int | str], linkname: str | None = None,
                client: EUtilsClient | None = None) -> dict:
    """Cross-database links for a list of UIDs, with per-UID attribution.

    One HTTP request per call: the UID list is sent as repeated id= parameters so
    elink returns one linkset per UID.
    """
    client = client or EUtilsClient()
    str_ids = _as_str_ids(ids)
    params: dict = {"dbfrom": dbfrom, "db": db, "id": str_ids, "retmode": "json"}
    if linkname:
        params["linkname"] = linkname
    resp = client.get("elink.fcgi", params)
    payload = resp.json()
    records = parse_linksets(payload, str_ids, dbfrom, db)
    linknames = sorted({ln for r in records for ln in r["links"]})
    return {
        "dbfrom": dbfrom,
        "db": db,
        "linkname_filter": linkname,
        "n_input_uids": len(str_ids),
        "records": records,
        "linknames_seen": linknames,
        "uids_with_no_links": [r["source_uid"] for r in records if not r["has_links"]],
    }


def parse_einfo_linklist(payload: dict, db: str | None = None) -> list[dict]:
    """Extract the link-name registry from an einfo retmode=json payload."""
    dbinfo = (payload.get("einforesult", {}) or {}).get("dbinfo", [])
    if isinstance(dbinfo, dict):
        dbinfo = [dbinfo]
    out = []
    for info in dbinfo:
        for link in info.get("linklist", []) or []:
            dbto = str(link.get("dbto", ""))
            if db is not None and dbto != db:
                continue
            out.append({
                "linkname": str(link.get("name", "")),
                "dbto": dbto,
                "menu": str(link.get("menu", "")),
                "description": str(link.get("description", "")),
            })
    out.sort(key=lambda r: r["linkname"])
    return out


def enumerate_linknames(dbfrom: str, db: str | None = None,
                        client: EUtilsClient | None = None) -> list[dict]:
    """Enumerate link names available FROM ``dbfrom`` (optionally restricted to target ``db``).

    Uses einfo version 2.0 (the authoritative registry); returns
    [{"linkname", "dbto", "menu", "description"}, ...] sorted by linkname.
    """
    client = client or EUtilsClient()
    resp = client.get("einfo.fcgi", {"db": dbfrom, "retmode": "json", "version": "2.0"})
    return parse_einfo_linklist(resp.json(), db=db)


def resolve_accessions(db: str, accessions: Sequence[str],
                       client: EUtilsClient | None = None) -> dict[str, str | None]:
    """Resolve sequence accessions (accession.version) to numeric Entrez UIDs via esearch [ACCN]."""
    client = client or EUtilsClient()
    out: dict[str, str | None] = {}
    for acc in accessions:
        resp = client.get("esearch.fcgi", {"db": db, "term": f"{acc}[ACCN]", "retmode": "json"})
        idlist = (resp.json().get("esearchresult", {}) or {}).get("idlist", [])
        out[acc] = str(idlist[0]) if idlist else None
    return out
