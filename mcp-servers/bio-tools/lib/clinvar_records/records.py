"""Canonicalize ClinVar esummary documents into lean JSON-able records."""
from __future__ import annotations





GOLD_STARS = {
    "practice guideline": 4,
    "reviewed by expert panel": 3,
    "criteria provided, multiple submitters, no conflicts": 2,
    "criteria provided, multiple submitters": 2,
    "criteria provided, conflicting classifications": 1,
    "criteria provided, conflicting interpretations": 1,
    "criteria provided, single submitter": 1,
    "no assertion criteria provided": 0,
    "no classification provided": 0,
    "no classification for the individual variant": 0,
    "no classifications from unflagged records": 0,
    "no assertion provided": 0,
}


_NO_DATE = "1/01/01 00:00"


def gold_stars(review_status: str) -> int | None:
    """Map a ClinVar review-status string to its gold-star count
    (0-4; None for unknown/empty strings)."""
    return GOLD_STARS.get((review_status or "").strip().lower())


def _date(raw: str | None) -> str | None:
    """``2022/10/12 00:00`` -> ``2022-10-12``; the 1/01/01 sentinel -> None."""
    raw = (raw or "").strip()
    if not raw or raw == _NO_DATE:
        return None
    return raw.split(" ")[0].replace("/", "-")


def _classification(block: dict | None) -> dict | None:
    """Normalize one of the three esummary classification blocks.

    Returns None when the block is absent or empty (no description AND no
    review status), else {description, review_status, gold_stars,
    last_evaluated, fda_recognized_database, conditions}.
    """
    if not block:
        return None
    description = (block.get("description") or "").strip()
    review_status = (block.get("review_status") or "").strip()
    if not description and not review_status:
        return None
    conditions = []
    for trait in block.get("trait_set") or []:
        name = (trait.get("trait_name") or "").strip()
        xrefs = [{"db": x.get("db_source"), "id": x.get("db_id")}
                 for x in trait.get("trait_xrefs") or []]
        if name or xrefs:
            conditions.append({"name": name, "xrefs": xrefs})
    return {
        "description": description,
        "review_status": review_status,
        "gold_stars": gold_stars(review_status),
        "last_evaluated": _date(block.get("last_evaluated")),
        "fda_recognized_database":
            (block.get("fda_recognized_database") or "").strip() or None,
        "conditions": conditions,
    }


def _locations(variation_set: list[dict]) -> list[dict]:
    locs = []
    for vs in variation_set:
        for loc in vs.get("variation_loc") or []:
            locs.append({
                "status": loc.get("status"),
                "assembly": loc.get("assembly_name"),
                "chrom": loc.get("chr"),
                "band": loc.get("band") or None,
                "start": int(loc["start"]) if loc.get("start") else None,
                "stop": int(loc["stop"]) if loc.get("stop") else None,
                "ref": loc.get("ref") or None,
                "alt": loc.get("alt") or None,
            })
    return locs


def parse_summary_doc(doc: dict) -> dict:
    """One esummary db=clinvar document -> canonical record.

    Keeps everything gnomAD's ClinVar mirror lacks: the three
    classification axes with review status / gold stars / last-evaluated
    dates / condition xrefs, SCV submission counts, canonical SPDI, and
    per-assembly locations.
    """
    variation_set = doc.get("variation_set") or []
    vs0 = variation_set[0] if variation_set else {}
    xrefs = vs0.get("variation_xrefs") or []
    rsids = [f"rs{x['db_id']}" for x in xrefs
             if x.get("db_source") == "dbSNP" and x.get("db_id")]
    other_xrefs = [{"db": x.get("db_source"), "id": x.get("db_id")}
                   for x in xrefs if x.get("db_source") != "dbSNP"]
    submissions = doc.get("supporting_submissions") or {}
    scv = submissions.get("scv") or []
    rcv = submissions.get("rcv") or []
    freqs = [{"source": f.get("source"), "minor_allele": f.get("minor_allele"),
              "value": f.get("value")}
             for f in vs0.get("allele_freq_set") or []]
    return {
        "variation_id": int(doc["uid"]),
        "accession": doc.get("accession"),
        "accession_version": doc.get("accession_version"),
        "title": doc.get("title"),
        "obj_type": doc.get("obj_type"),
        "variant_type": vs0.get("variant_type"),
        "canonical_spdi": vs0.get("canonical_spdi") or None,
        "cdna_change": vs0.get("cdna_change") or None,
        "protein_change": doc.get("protein_change") or None,
        "rsids": rsids,
        "other_xrefs": other_xrefs,
        "genes": [{"symbol": g.get("symbol"), "gene_id": g.get("geneid"),
                   "strand": g.get("strand")}
                  for g in doc.get("genes") or []],
        "molecular_consequences": doc.get("molecular_consequence_list") or [],
        "locations": _locations(variation_set),
        "allele_frequencies": freqs,
        "germline_classification":
            _classification(doc.get("germline_classification")),
        "clinical_impact_classification":
            _classification(doc.get("clinical_impact_classification")),
        "oncogenicity_classification":
            _classification(doc.get("oncogenicity_classification")),
        "n_submissions": len(scv),
        "supporting_submissions": {"scv": scv, "rcv": rcv},
    }
