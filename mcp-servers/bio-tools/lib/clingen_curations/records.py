"""Record extraction and canonicalization.

One shared ``canonicalize`` function is used by the gate, the bench, and the
tests. Canonicalization rules (documented in README, contract-compliant):

  * JSON with sorted keys, UTF-8, compact separators;
  * unordered collections sorted (HGVS expression lists, evidence links,
    evidence codes, record lists by a stable key);
  * volatile fields dropped (see VOLATILE notes per record type below) —
    these are bookkeeping/dates and external computed scores, never
    scientific curation content (classifications, MOI, MONDO IDs,
    coordinates and assertion labels are all retained).
"""
from __future__ import annotations

import json






DOSAGE_ASSERTION_LABELS = {
    "0": "No Evidence",
    "1": "Little Evidence",
    "2": "Emerging Evidence",
    "3": "Sufficient Evidence",
    "30": "Gene Associated with Autosomal Recessive Phenotype",
    "40": "Dosage Sensitivity Unlikely",
    "-5": "Not yet evaluated",
}


CSV_LABEL_TO_CODE = {
    "No Evidence": "0",
    "Little Evidence": "1",
    "Emerging Evidence": "2",
    "Sufficient Evidence": "3",
    "Gene Associated with Autosomal Recessive Phenotype": "30",
    "Dosage Sensitivity Unlikely": "40",
    "Not yet evaluated": "-5",
}


def normalize_dosage_assertion(value) -> dict | None:
    """Normalize a raw haplo/triplo assertion value to {code, label}.

    Raw values observed: int (3), str digit ("0"), str "40: Dosage
    sensitivity unlikely", str "Not yet evaluated", None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s == "Not yet evaluated":
        code = "-5"
    else:
        code = s.split(":")[0].strip()
    label = DOSAGE_ASSERTION_LABELS.get(code)
    if label is None:

        return {"code": code, "label": s}
    return {"code": code, "label": label}


def csv_label_to_code(label: str) -> str | None:
    """Map a CSV assertion label to its numeric code (gate cross-check)."""
    label = (label or "").strip()
    if not label:
        return None
    for prefix, code in CSV_LABEL_TO_CODE.items():
        if label.startswith(prefix):
            return code
    return f"UNKNOWN:{label}"






def validity_record(row: dict) -> dict:
    """Stable record from one /api/validity row.

    VOLATILE (dropped): ``date``, ``released``, ``order``, ``report_id``
    (server-side bookkeeping), ``symbol_id`` (duplicate of hgnc_id).
    """
    return {
        "gene_symbol": row["symbol"],
        "hgnc_id": row["hgnc_id"],
        "disease_label": (row.get("disease_name") or "").strip(),
        "mondo_id": row.get("mondo"),
        "moi": row.get("moi"),
        "sop": row.get("sop"),
        "classification": (row.get("classification") or "").strip(),
        "expert_panel": (row.get("ep") or "").strip(),
        "affiliate_id": row.get("affiliate_id"),
        "animal_model_only": bool(row.get("animal_model_only")),
        "assertion_id": row.get("perm_id"),
    }


def dosage_record(row: dict) -> dict:
    """Stable record from one /api/dosage row.

    VOLATILE (dropped): ``date``/``rawdate`` (last-review bookkeeping),
    ``pli``/``hi``/``plof`` (external computed scores that update with their
    own releases), ``haplo_history``/``triplo_history``/``hhr``/``thr``,
    ``omimlink``/``omimcombo`` (presentation fields).
    """
    is_region = row.get("type") == 1
    return {
        "record_type": "region" if is_region else "gene",
        "symbol": (row.get("symbol") or "").strip(),
        "id": row.get("hgnc_id"),
        "cytoband": row.get("location"),
        "grch37": row.get("grch37"),
        "grch38": row.get("grch38"),
        "haploinsufficiency": normalize_dosage_assertion(row.get("haplo_assertion")),
        "triplosensitivity": normalize_dosage_assertion(row.get("triplo_assertion")),
        "haplo_disease": row.get("haplo_disease"),
        "haplo_mondo": row.get("haplo_mondo"),
        "triplo_disease": row.get("triplo_disease"),
        "triplo_mondo": row.get("triplo_mondo"),
        "omim": row.get("omim"),
        "morbid": row.get("morbid"),
    }


def actionability_record(columns: list, row: list) -> dict:
    """Stable record from one actionability flat-table row.

    The flat API returns {columns: [...], rows: [[...], ...]}; a record is
    dict(zip(columns, row)) with volatile fields dropped.

    VOLATILE (dropped): ``lastUpdated``, ``lastAuthor``, ``latestSearchDate``
    (curation bookkeeping), ``topicIri``/``contextIri`` (server URLs).
    """
    d = dict(zip(columns, row))
    genes = [g.strip() for g in str(d.get("geneOrVariant") or "").split(",") if g.strip()]
    return {
        "doc_id": d.get("docId"),
        "curation_type": d.get("curationType"),
        "context": d.get("context"),
        "release": d.get("release"),
        "release_date": d.get("releaseDate"),
        "genes": genes,
        "gene_omim": d.get("geneOmim"),
        "disease": d.get("disease"),
        "disease_omim": d.get("omim"),
        "status_overall": d.get("status-overall"),
        "outcome": d.get("outcome"),
        "outcome_scoring_group": d.get("outcomeScoringGroup"),
        "intervention": d.get("intervention"),
        "intervention_scoring_group": d.get("interventionScoringGroup"),
        "severity": d.get("severity"),
        "likelihood": d.get("likelihood"),
        "nature_of_intervention": d.get("natureOfIntervention"),
        "effectiveness": d.get("effectiveness"),
        "overall_score": d.get("overall"),
    }


def erepo_record(interp: dict) -> dict:
    """Stable record from one ERepo variantInterpretation (light context).

    Unordered collections sorted: hgvs, evidenceLinks, evidenceCodes.
    VOLATILE (dropped): ``warnings`` (transient server annotations).
    """
    guidelines = []
    for g in interp.get("guidelines") or []:
        agents = []
        for a in g.get("agents") or []:
            met, not_met = [], []
            for ec in a.get("evidenceCodes") or []:
                (met if ec.get("status") == "Met" else not_met).append(ec.get("label"))
            agents.append({
                "agent_id": a.get("@id"),
                "affiliation": a.get("affiliation"),
                "outcome": (a.get("outcome") or {}).get("label"),
                "evidence_codes_met": sorted(met),
                "evidence_codes_not_met": sorted(not_met),
            })
        agents.sort(key=lambda x: x["agent_id"] or "")
        guidelines.append({
            "guideline": g.get("label"),
            "guideline_id": g.get("@id"),
            "outcome": (g.get("outcome") or {}).get("label"),
            "agents": agents,
        })
    guidelines.sort(key=lambda x: x["guideline_id"] or "")
    cond = interp.get("condition") or {}
    gene = interp.get("gene") or {}
    return {
        "interpretation_id": interp.get("@id"),
        "uuid": interp.get("uuid"),
        "caid": interp.get("caid"),
        "clinvar_variation_id": interp.get("variationId"),
        "gene_symbol": gene.get("label"),
        "gene_ncbi_id": gene.get("NCBI_id"),
        "condition_id": cond.get("@id"),
        "condition_label": cond.get("label"),
        "hgvs": sorted(interp.get("hgvs") or []),
        "evidence_links": sorted((e.get("@id") or "") for e in interp.get("evidenceLinks") or []),
        "published_date": interp.get("publishedDate"),
        "guidelines": guidelines,
    }






def canonicalize(obj) -> bytes:
    """Canonical UTF-8 JSON bytes: sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
