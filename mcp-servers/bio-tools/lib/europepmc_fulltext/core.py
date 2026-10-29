"""Core pipeline: ID list -> OA/full-text availability check -> JATS retrieval ->
structured section extraction, with explicit not-OA / no-full-text reporting.

Output order is deterministic: records are returned in the input ID order;
sections and captions inside each record are in document order.
"""
from __future__ import annotations

import re
import time

from .client import EuropePMCClient
from .extract import extract_sections

PMCID_RE = re.compile(r"^PMC\d+$", re.IGNORECASE)
PMID_RE = re.compile(r"^\d+$")


SEARCH_BATCH_SIZE = 8


_SEARCH_FIELDS = {
    "id": "ext_id",
    "source": "source",
    "pmid": "pmid",
    "pmcid": "pmcid",
    "doi": "doi",
    "title": "title",
    "authorString": "author_string",
    "journalTitle": "journal",
    "pubYear": "pub_year",
    "isOpenAccess": "is_open_access_flag",
    "inEPMC": "in_epmc_flag",
    "inPMC": "in_pmc_flag",
    "license": "license",
    "citedByCount": "cited_by_count",
}


def classify_id(raw_id: str) -> str:
    """'pmcid' | 'pmid' | 'unknown'."""
    rid = raw_id.strip()
    if PMCID_RE.match(rid):
        return "pmcid"
    if PMID_RE.match(rid):
        return "pmid"
    return "unknown"


def _flag(value) -> bool:
    return str(value).strip().upper() == "Y"


def _availability_from_hit(input_id: str, id_type: str, hit: dict) -> dict:
    rec = {"input_id": input_id, "input_id_type": id_type, "found": True}
    for src_key, dst_key in _SEARCH_FIELDS.items():
        rec[dst_key] = hit.get(src_key)
    journal = hit.get("journalTitle")
    if journal is None:
        journal = (
            hit.get("journalInfo", {}).get("journal", {}).get("title")
            if isinstance(hit.get("journalInfo"), dict)
            else None
        )
    rec["journal"] = journal
    rec["is_open_access"] = _flag(hit.get("isOpenAccess"))
    rec["in_epmc"] = _flag(hit.get("inEPMC"))
    rec["search_abstract"] = hit.get("abstractText") or None
    return rec


def _not_found_record(input_id: str, id_type: str) -> dict:
    return {
        "input_id": input_id,
        "input_id_type": id_type,
        "found": False,
        "is_open_access": False,
        "in_epmc": False,
        "pmid": None,
        "pmcid": None,
        "title": None,
        "search_abstract": None,
    }


def check_availability(ids: list[str], client: EuropePMCClient | None = None) -> list[dict]:
    """Resolve each PMID/PMCID via the /search endpoint (resultType=core) and
    report open-access / in-Europe-PMC flags.

    IDs are batched (OR-ed, SEARCH_BATCH_SIZE per query) to keep the request
    count low; results are mapped back to the input IDs and returned in input
    order. IDs that resolve to nothing are reported with found=False, never
    dropped.
    """
    client = client or EuropePMCClient()






    typed = [(raw, raw.strip(), classify_id(raw)) for raw in ids]

    pmids = [rid for _, rid, t in typed if t == "pmid"]
    pmcids = [rid for _, rid, t in typed if t == "pmcid"]

    by_pmid: dict[str, dict] = {}
    by_pmcid: dict[str, dict] = {}

    def _ingest(hits: list[dict]) -> None:
        for hit in hits:
            pmid = str(hit.get("pmid") or hit.get("id") or "")
            pmcid = str(hit.get("pmcid") or "")
            if pmid:
                by_pmid.setdefault(pmid, hit)
            if pmcid:
                by_pmcid.setdefault(pmcid.upper(), hit)

    for i in range(0, len(pmids), SEARCH_BATCH_SIZE):
        chunk = pmids[i : i + SEARCH_BATCH_SIZE]
        query = "(" + " OR ".join(f"EXT_ID:{p}" for p in chunk) + ") AND SRC:MED"
        data = client.search(query, result_type="core", page_size=max(25, len(chunk) * 2))
        _ingest(data.get("resultList", {}).get("result", []))

    for i in range(0, len(pmcids), SEARCH_BATCH_SIZE):
        chunk = pmcids[i : i + SEARCH_BATCH_SIZE]
        query = "(" + " OR ".join(f"PMCID:{p.upper()}" for p in chunk) + ")"
        data = client.search(query, result_type="core", page_size=max(25, len(chunk) * 2))
        _ingest(data.get("resultList", {}).get("result", []))

    records = []
    for raw, rid, id_type in typed:
        if id_type == "pmid":
            hit = by_pmid.get(rid)
        elif id_type == "pmcid":
            hit = by_pmcid.get(rid.upper())
        else:
            rec = _not_found_record(raw, id_type)
            rec["error"] = "unrecognized id format (expected PMID digits or PMC#######)"
            records.append(rec)
            continue
        records.append(
            _availability_from_hit(raw, id_type, hit) if hit else _not_found_record(raw, id_type)
        )
    return records


def fetch_fulltext_xml(pmcid: str, client: EuropePMCClient | None = None) -> tuple[int, bytes | None]:
    """GET the JATS full-text XML for a PMCID. (status_code, xml_bytes|None)."""
    client = client or EuropePMCClient()
    return client.full_text_xml(pmcid)


def fetch_articles(
    ids: list[str],
    client: EuropePMCClient | None = None,
    include_section_text: bool = True,
    fetch_non_oa: bool = False,
    deadline_s: float = 40.0,
) -> list[dict]:
    """Full pipeline. For each input ID:

      1. availability check via /search (batched);
      2. if the article is flagged open access and has a PMCID, retrieve
         /{PMCID}/fullTextXML and extract structured sections;
      3. otherwise report explicitly why no full text was returned
         (fulltext_status is never silently empty).

    fulltext_status values:
      retrieved          -- JATS XML fetched and parsed
      not_open_access    -- article exists but is not in the OA subset
      no_pmcid           -- article is flagged OA but has no PMCID
      xml_not_available  -- flagged OA with PMCID, but fullTextXML returned 404
      not_found          -- the input ID did not resolve in Europe PMC
      invalid_id         -- the input string is not a PMID/PMCID
      not_processed      -- the deadline elapsed before this article's
                            fullTextXML was fetched (partial batch returned)

    ``deadline_s`` bounds the per-article fullTextXML wall-clock: once it is
    exceeded, remaining OA articles are reported ``not_processed`` rather than
    risking the ~60s MCP transport budget and discarding already-fetched
    records (mirrors the sibling ClinVar/dbsnp batch tools).
    """
    client = client or EuropePMCClient()
    availability = check_availability(ids, client=client)
    _deadline = time.monotonic() + deadline_s

    out = []
    for av in availability:
        record = {
            "input_id": av["input_id"],
            "input_id_type": av["input_id_type"],
            "found": av.get("found", False),
            "pmid": av.get("pmid"),
            "pmcid": av.get("pmcid"),
            "doi": av.get("doi"),
            "title": av.get("title"),
            "journal": av.get("journal"),
            "pub_year": av.get("pub_year"),
            "is_open_access": av.get("is_open_access", False),
            "in_epmc": av.get("in_epmc", False),
            "license": av.get("license"),
            "fulltext_available": False,
            "fulltext_status": None,
            "abstract": None,
            "sections": [],
            "section_inventory": [],
            "figure_captions": [],
            "table_captions": [],
            "n_figures": None,
            "n_tables": None,
            "n_references": None,
        }

        if av.get("error"):
            record["fulltext_status"] = "invalid_id"
            record["detail"] = av["error"]
            out.append(record)
            continue
        if not av.get("found"):
            record["fulltext_status"] = "not_found"
            record["detail"] = "ID did not resolve via the Europe PMC search endpoint"
            out.append(record)
            continue

        pmcid = av.get("pmcid")
        is_oa = av.get("is_open_access", False)

        if not is_oa and not fetch_non_oa:
            record["fulltext_status"] = "not_open_access"
            record["detail"] = (
                "isOpenAccess=N: not in the Europe PMC open-access full-text subset"
                + (" (a PMCID exists but fullTextXML is not served for it)" if pmcid else "")
            )
            record["abstract"] = av.get("search_abstract")
            out.append(record)
            continue

        if not pmcid:
            record["fulltext_status"] = "no_pmcid"
            record["detail"] = "no PMCID assigned; fullTextXML requires a PMCID"
            record["abstract"] = av.get("search_abstract")
            out.append(record)
            continue





        if time.monotonic() >= _deadline:
            record["fulltext_status"] = "not_processed"
            record["detail"] = (
                f"deadline ({deadline_s:g}s) elapsed before fullTextXML fetch; "
                "retry with fewer ids"
            )
            record["abstract"] = av.get("search_abstract")
            out.append(record)
            continue

        status, xml_bytes = client.full_text_xml(pmcid)
        if status != 200 or xml_bytes is None:
            record["fulltext_status"] = "xml_not_available"
            record["detail"] = f"fullTextXML returned HTTP {status} for {pmcid}"
            record["abstract"] = av.get("search_abstract")
            out.append(record)
            continue

        extracted = extract_sections(xml_bytes, include_section_text=include_section_text)
        record["fulltext_available"] = True
        record["fulltext_status"] = "retrieved"
        record["raw_xml_bytes"] = len(xml_bytes)
        record["title"] = extracted["title"] or record["title"]
        record["abstract"] = extracted["abstract"]
        record["sections"] = extracted["sections"]
        record["section_inventory"] = extracted["section_inventory"]
        record["figure_captions"] = extracted["figure_captions"]
        record["table_captions"] = extracted["table_captions"]
        record["n_figures"] = extracted["n_figures"]
        record["n_tables"] = extracted["n_tables"]
        record["n_references"] = extracted["n_references"]
        out.append(record)

    return out


def summarize_record(record: dict) -> dict:
    """Token-lean summary view: availability + section inventory + counts,
    no section text, no captions text."""
    return {
        "input_id": record["input_id"],
        "pmid": record.get("pmid"),
        "pmcid": record.get("pmcid"),
        "title": record.get("title"),
        "is_open_access": record.get("is_open_access"),
        "fulltext_available": record.get("fulltext_available"),
        "fulltext_status": record.get("fulltext_status"),
        "section_inventory": record.get("section_inventory", []),
        "n_figures": record.get("n_figures"),
        "n_tables": record.get("n_tables"),
        "n_references": record.get("n_references"),
    }
