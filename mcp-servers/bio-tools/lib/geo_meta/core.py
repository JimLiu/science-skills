"""Core retrieval logic for geo_meta.

Data sources (and only these):

1. NCBI E-utilities, ``db=gds``:
   - ``esearch.fcgi``  — accession -> UID resolution and search-spec execution
     (the search count is also the accuracy-gate ground truth for search specs);
   - ``esummary.fcgi`` (version 2.0, JSON) — series-level summary including
     ``n_samples`` (the accuracy-gate ground truth for per-series sample counts),
     taxon, GDS type, publication date, FTP link, supplementary-file types.
2. GEO accession viewer ``acc.cgi`` (``form=text``, ``view=brief``):
   - ``targ=self`` — the series header block (title, summary, design, platform IDs,
     sample IDs, series-level supplementary file URLs);
   - ``targ=gsm``  — sample header blocks (title, organism, characteristics,
     library info, per-sample supplementary file URLs).

``view=brief`` responses contain *no* data tables. Platform records (``targ=gpl`` /
``targ=all``) are never requested: their probe-set listings reach several MB even
in brief view, which is exactly the GEOparse-style waste this tool avoids.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from .client import PoliteClient
from .soft import parse_sample_headers, parse_series_header

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ACC_CGI = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"

from mcp_servers_common.ua import contact_email

TOOL_NAME = "geo-meta"


TOOL_EMAIL = os.environ.get("NCBI_EMAIL") or contact_email()

_GSE_RE = re.compile(r"^GSE\d+$")




ESUMMARY_FIELDS = (
    "uid",
    "accession",
    "title",
    "summary",
    "gdstype",
    "taxon",
    "n_samples",
    "pdat",
    "suppfile",
    "ftplink",
    "bioproject",
    "gpl",
    "gse",
    "pubmedids",
    "platformtitle",
    "platformtaxa",
    "samplestaxa",
    "entrytype",
)



VOLATILE_FIELDS = ("provenance",)



def _eutils_params(extra: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"tool": TOOL_NAME}
    if TOOL_EMAIL:
        params["email"] = TOOL_EMAIL
    params.update(extra)
    return params


def esearch_gds(client: PoliteClient, term: str, retmax: int = 500) -> dict[str, Any]:
    """Run esearch against db=gds; returns {'count': int, 'ids': [uid, ...]}."""
    resp = client.get(
        f"{EUTILS_BASE}/esearch.fcgi",
        params=_eutils_params(
            {"db": "gds", "term": term, "retmode": "json", "retmax": str(retmax)}
        ),
    )
    result = resp.json()["esearchresult"]
    if "ERROR" in result:
        raise RuntimeError(f"esearch error for term {term!r}: {result['ERROR']}")
    return {"count": int(result["count"]), "ids": list(result.get("idlist", []))}


def esummary_gds(client: PoliteClient, uids: list[str]) -> dict[str, dict[str, Any]]:
    """Run esummary (version 2.0, JSON) for db=gds UIDs; returns accession -> docsum."""
    if not uids:
        return {}
    resp = client.post(
        f"{EUTILS_BASE}/esummary.fcgi",
        data=_eutils_params(
            {"db": "gds", "id": ",".join(uids), "retmode": "json", "version": "2.0"}
        ),
    )
    result = resp.json()["result"]
    out: dict[str, dict[str, Any]] = {}
    for uid in result.get("uids", []):
        doc = result[uid]
        out[doc["accession"]] = doc
    return out


def _trim_esummary(doc: dict[str, Any]) -> dict[str, Any]:
    trimmed = {k: doc.get(k) for k in ESUMMARY_FIELDS if k in doc}

    if "n_samples" in trimmed:
        try:
            trimmed["n_samples"] = int(trimmed["n_samples"])
        except (TypeError, ValueError):
            trimmed["n_samples"] = None

    samples = doc.get("samples") or []
    trimmed["samples"] = sorted(
        ({"accession": s.get("accession"), "title": s.get("title")} for s in samples),
        key=lambda s: s["accession"] or "",
    )
    return trimmed


def resolve_accessions(client: PoliteClient, accessions: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve GSE accessions -> trimmed esummary docs with one esearch + one esummary."""
    for acc in accessions:
        if not _GSE_RE.match(acc):
            raise ValueError(f"not a GSE accession: {acc!r}")
    term = "(" + " OR ".join(f"{acc}[ACCN]" for acc in accessions) + ") AND gse[ETYP]"
    found = esearch_gds(client, term, retmax=max(len(accessions) * 2, 20))
    docs = esummary_gds(client, found["ids"])
    missing = [acc for acc in accessions if acc not in docs]
    if missing:
        raise RuntimeError(f"accessions not found in GEO DataSets (db=gds): {missing}")
    return {acc: docs[acc] for acc in accessions}



def fetch_soft_brief(client: PoliteClient, accession: str, targ: str) -> str:
    """Fetch acc.cgi brief text for one accession (targ='self' or 'gsm')."""
    if targ not in {"self", "gsm"}:
        raise ValueError("targ must be 'self' or 'gsm' (geo_meta never fetches platform records)")
    resp = client.get(
        ACC_CGI,
        params={"acc": accession, "targ": targ, "form": "text", "view": "brief"},
    )
    text = resp.text
    if "^SERIES" not in text and "^SAMPLE" not in text:
        raise RuntimeError(f"acc.cgi returned no SOFT entities for {accession} (targ={targ})")
    return text



def _assemble_series_record(
    accession: str,
    esummary_doc: dict[str, Any],
    series_header: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    organisms = sorted({org for s in samples for org in s["organism"]})
    if not organisms and esummary_doc.get("taxon"):
        organisms = sorted(t.strip() for t in str(esummary_doc["taxon"]).split(";") if t.strip())
    supplementary = {
        "series": series_header["supplementary_files"],
        "samples": {s["accession"]: s["supplementary_files"] for s in samples if s["supplementary_files"]},
        "ftp_root": esummary_doc.get("ftplink"),
    }
    return {
        "accession": accession,
        "title": series_header["title"],
        "organism": organisms,
        "series_type": series_header["type"],
        "status": series_header["status"],
        "submission_date": series_header["submission_date"],
        "last_update_date": series_header["last_update_date"],
        "summary": series_header["summary"],
        "overall_design": series_header["overall_design"],
        "pubmed_ids": series_header["pubmed_ids"],
        "platforms": series_header["platform_ids"],
        "n_samples": len(samples),
        "samples": samples,
        "supplementary_files": supplementary,
        "esummary": _trim_esummary(esummary_doc),
    }


def fetch_series(
    accession: str,
    client: Optional[PoliteClient] = None,
    esummary_doc: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fetch one GSE series as a structured record.

    Issues at most 4 HTTP requests (esearch + esummary if ``esummary_doc`` is not
    supplied, then acc.cgi targ=self and targ=gsm).
    """
    own_client = client is None
    client = client or PoliteClient()
    try:
        if esummary_doc is None:
            esummary_doc = resolve_accessions(client, [accession])[accession]
        series_header = parse_series_header(fetch_soft_brief(client, accession, "self"))
        samples = parse_sample_headers(fetch_soft_brief(client, accession, "gsm"))
        return _assemble_series_record(accession, esummary_doc, series_header, samples)
    finally:
        if own_client:
            client.close()


def fetch_series_batch(
    accessions: list[str], client: Optional[PoliteClient] = None
) -> list[dict[str, Any]]:
    """Fetch many GSE series; output is ordered by accession (deterministic).

    Issues 2 + 2*N HTTP requests for N series.
    """
    own_client = client is None
    client = client or PoliteClient()
    try:
        docs = resolve_accessions(client, accessions)
        records = []
        for acc in sorted(set(accessions)):
            records.append(fetch_series(acc, client=client, esummary_doc=docs[acc]))
        return records
    finally:
        if own_client:
            client.close()


def search_series(
    term: str, client: Optional[PoliteClient] = None, retmax: int = 500
) -> dict[str, Any]:
    """Run a GEO DataSets (db=gds) search spec and return series-level records.

    Returns ``{"term", "count", "records"}`` where ``count`` is esearch's own count
    and ``records`` are trimmed esummary docs sorted by accession. Records include
    each sample's accession and title; call :func:`fetch_series` on individual
    accessions when full sample characteristics are needed.
    """
    own_client = client is None
    client = client or PoliteClient()
    try:
        found = esearch_gds(client, term, retmax=retmax)
        docs = esummary_gds(client, found["ids"])
        records = [_trim_esummary(docs[acc]) for acc in sorted(docs)]
        return {
            "term": term,
            "count": found["count"],
            "retrieved": len(records),
            "complete": found["count"] == len(records),
            "records": records,
        }
    finally:
        if own_client:
            client.close()



def canonicalize(record: dict[str, Any]) -> bytes:
    """Canonical byte representation of a record for run-to-run comparison.

    Rules (documented in README): drop volatile provenance fields, serialize as
    JSON with sorted keys, compact separators, ASCII-escaped, ``\\n`` line endings.
    No scientific content is dropped or rewritten.
    """
    cleaned = {k: v for k, v in record.items() if k not in VOLATILE_FIELDS}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()



def run_battery(battery: dict[str, Any], client: Optional[PoliteClient] = None) -> dict[str, Any]:
    """Run the full pinned battery (series accessions + search specs).

    Returns ``{"series": {acc: record}, "searches": {name: result}, "stats": {...}}``.
    """
    own_client = client is None
    client = client or PoliteClient()
    try:
        accessions = [item["accession"] for item in battery["series"]]
        series_records = {r["accession"]: r for r in fetch_series_batch(accessions, client=client)}
        searches = {}
        for spec in battery["searches"]:
            searches[spec["name"]] = search_series(spec["term"], client=client, retmax=spec.get("retmax", 500))
        return {
            "series": series_records,
            "searches": searches,
            "stats": client.stats.as_dict(),
        }
    finally:
        if own_client:
            client.close()
