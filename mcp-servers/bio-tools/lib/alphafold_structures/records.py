"""Record builders over the AlphaFold DB prediction API."""
from __future__ import annotations

import time

from .client import AlphaFoldClient, InvalidAccessionError, NotFoundError



MAX_IDS_PER_CALL = 40

_URL_FIELDS = {
    "cif": "cifUrl",
    "bcif": "bcifUrl",
    "pdb": "pdbUrl",
    "pae_image": "paeImageUrl",
    "pae_json": "paeDocUrl",
    "plddt_json": "plddtDocUrl",
    "msa": "msaUrl",
    "alphamissense_csv": "amAnnotationsUrl",
}


def parse_model(raw: dict, include_sequence: bool = False) -> dict:
    record = {
        "model_entity_id": raw.get("modelEntityId"),
        "entry_id": raw.get("entryId"),
        "provider_id": raw.get("providerId"),
        "tool_used": raw.get("toolUsed"),
        "uniprot_accession": raw.get("uniprotAccession"),
        "uniprot_id": raw.get("uniprotId"),
        "uniprot_description": raw.get("uniprotDescription"),
        "gene": raw.get("gene"),
        "organism_scientific_name": raw.get("organismScientificName"),
        "tax_id": raw.get("taxId"),
        "is_uniprot_reviewed": raw.get("isUniProtReviewed"),
        "is_reference_proteome": raw.get("isReferenceProteome"),
        "is_complex": raw.get("isComplex"),
        "sequence_length": len(raw["sequence"]) if raw.get("sequence") else None,
        "uniprot_start": raw.get("uniprotStart"),
        "uniprot_end": raw.get("uniprotEnd"),
        "global_plddt": raw.get("globalMetricValue"),
        "fraction_plddt": {
            "very_low": raw.get("fractionPlddtVeryLow"),
            "low": raw.get("fractionPlddtLow"),
            "confident": raw.get("fractionPlddtConfident"),
            "very_high": raw.get("fractionPlddtVeryHigh"),
        },
        "latest_version": raw.get("latestVersion"),
        "all_versions": raw.get("allVersions") or [],
        "model_created_date": raw.get("modelCreatedDate"),
        "urls": {k: raw[v] for k, v in _URL_FIELDS.items() if raw.get(v)},
    }
    if include_sequence:
        record["sequence"] = raw.get("sequence")
    return record


def fetch_prediction_record(
    client: AlphaFoldClient,
    uniprot_accession: str,
    include_sequence: bool = False,
) -> dict:
    """Full prediction record for one accession; no-model -> has_model=false."""
    accession = uniprot_accession.strip()
    try:
        models = client.get_prediction(accession)
    except NotFoundError:
        return {"uniprot_accession": accession, "has_model": False,
                "n_models": 0, "models": []}
    except InvalidAccessionError as exc:
        return {"uniprot_accession": accession, "has_model": False,
                "n_models": 0, "models": [], "error": f"invalid_accession: {exc}"}
    parsed = [parse_model(m, include_sequence=include_sequence) for m in models]
    return {
        "uniprot_accession": accession,
        "has_model": True,
        "n_models": len(parsed),
        "models": parsed,
    }


def fetch_coverage_records(
    client: AlphaFoldClient,
    uniprot_accessions: list[str],
    deadline_s: float = 40.0,
) -> dict:
    """Compact has-model flags for a batch of accessions.

    Blank entries and duplicates are stripped first (disclosed via
    n_blank_skipped / n_duplicate_skipped so the caller's request count always
    reconciles: n_unique + skipped == inputs); the batch ceiling applies to
    the UNIQUE set. One request per unique accession, in input order. Each
    record: has_model, n_models, latest_version / global_plddt /
    model_entity_id of the primary (first-listed) model. Invalid accessions
    get an explicit error field — never silently dropped. ``deadline_s`` bounds
    the per-accession wall-clock: accessions not reached before it elapses are
    returned in ``not_processed`` rather than overrunning the ~60s MCP
    transport budget (mirrors the sibling ClinVar/dbSNP batch tools).
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    n_blank = 0
    n_duplicate = 0
    for raw_acc in uniprot_accessions:
        acc = raw_acc.strip()
        if not acc:
            n_blank += 1
        elif acc in seen:
            n_duplicate += 1
        else:
            seen.add(acc)
            cleaned.append(acc)
    if len(cleaned) > MAX_IDS_PER_CALL:
        raise ValueError(
            f"{len(cleaned)} unique accessions requested; max "
            f"{MAX_IDS_PER_CALL} per call — split the batch")
    records: list[dict] = []
    not_processed: list[str] = []
    _deadline = time.monotonic() + deadline_s
    for acc in cleaned:
        if time.monotonic() >= _deadline:
            not_processed.append(acc)
            continue
        try:
            models = client.get_prediction(acc)
        except NotFoundError:
            records.append({"uniprot_accession": acc, "has_model": False})
            continue
        except InvalidAccessionError as exc:
            records.append({"uniprot_accession": acc, "has_model": False,
                            "error": f"invalid_accession: {exc}"})
            continue
        primary = models[0] if models else {}
        records.append({
            "uniprot_accession": acc,
            "has_model": bool(models),
            "n_models": len(models),
            "model_entity_id": primary.get("modelEntityId"),
            "latest_version": primary.get("latestVersion"),
            "global_plddt": primary.get("globalMetricValue"),
            "sequence_length": len(primary["sequence"]) if primary.get("sequence") else None,
        })
    return {
        "n_unique": len(cleaned),
        "n_blank_skipped": n_blank,
        "n_duplicate_skipped": n_duplicate,
        "not_processed": not_processed,
        "records": records,
    }
