"""Structured study-metadata retrieval from the MetaboLights web service.

Primary endpoint per study:  GET /studies/public/study/{accession}
    -> ``content`` carries the parsed ISA-Tab payload: title, description, organisms,
       factors, design descriptors, assays (with measurement/technology/platform), and
       the full sample table.

Cross-check endpoints (used by the accuracy gate, not by the main retrieval path):
    GET /studies/{accession}/title       -> {"title": ...}
    GET /studies/{accession}/factors     -> {"factors": [...]}   (ISA factor objects)
    GET /studies/{accession}/organisms   -> {"organisms": [...]} (sample-sheet derived)
    GET /studies/{accession}/assays      -> {"data": {"assays": [{"filename": ...}]}}
    GET /studies/{accession}/s_{accession}.txt -> parsed sample sheet rows

Full public list:  GET /studies  -> {"content": [accessions...], "studies": <count>}
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from .client import MetaboLightsClient

_ACCESSION_RE = re.compile(r"^MTBLS\d+$")





def _accession_sort_key(acc: str) -> tuple:
    """Numeric-aware sort key: MTBLS2 < MTBLS10 < MTBLS100."""
    m = re.match(r"^([A-Z]+)(\d+)$", acc)
    if m:
        return (m.group(1), int(m.group(2)))
    return (acc, 0)


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization: sorted keys, compact separators, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)





def extract_study_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw ``/studies/public/study/{acc}`` JSON payload into a flat record.

    The record contains exactly the fields promised by the tool spec
    (title, organism, assay technology, factors, sample count) plus a small set of
    stable companion fields (description length is unbounded so it is kept verbatim).
    All list-valued fields are sorted for deterministic output.
    """
    content = payload.get("content")
    if not isinstance(content, dict):
        raise ValueError("payload has no 'content' object — not a public-study response")

    accession = _clean_str(content.get("studyIdentifier"))


    organisms = []
    for org in content.get("organism") or []:
        name = _clean_str(org.get("organismName"))
        part = _clean_str(org.get("organismPart"))
        if name or part:
            organisms.append({"organism": name, "organism_part": part})
    organisms.sort(key=lambda o: (o["organism"] or "", o["organism_part"] or ""))


    assays = []
    for a in content.get("assays") or []:
        assays.append(
            {
                "assay_number": a.get("assayNumber"),
                "measurement": _clean_str(a.get("measurement")),
                "technology": _clean_str(a.get("technology")),
                "platform": _clean_str(a.get("platform")),
                "filename": _clean_str(a.get("fileName")),
            }
        )
    assays.sort(key=lambda a: (a["assay_number"] if a["assay_number"] is not None else 0,
                               a["filename"] or ""))
    technologies = sorted({a["technology"] for a in assays if a["technology"]})


    factors = sorted({_clean_str(f.get("name")) for f in (content.get("factors") or [])
                      if _clean_str(f.get("name"))})


    descriptors = sorted({_clean_str(d.get("description"))
                          for d in (content.get("descriptors") or [])
                          if _clean_str(d.get("description"))})


    sample_table = content.get("sampleTable") or {}
    sample_rows = sample_table.get("data") or []
    sample_count = len(sample_rows)

    derived = content.get("derivedData") or {}

    record = {
        "accession": accession,
        "title": _clean_str(content.get("title")),
        "description": _clean_str(content.get("studyDescription")),
        "study_status": _clean_str(content.get("studyStatus")),
        "release_year": derived.get("releaseYear"),
        "submission_year": derived.get("submissionYear"),
        "organisms": organisms,
        "organism_names": sorted({o["organism"] for o in organisms if o["organism"]}),
        "assays": assays,
        "assay_count": len(assays),
        "technologies": technologies,
        "factors": factors,
        "descriptors": descriptors,
        "sample_count": sample_count,
    }
    return record





def get_study_metadata(accession: str, client: Optional[MetaboLightsClient] = None
                       ) -> Dict[str, Any]:
    """Fetch one study and return its structured metadata record."""
    accession = accession.strip().upper()
    if not _ACCESSION_RE.match(accession):
        raise ValueError(f"not a MetaboLights accession: {accession!r}")
    client = client or MetaboLightsClient()
    payload = client.get_json(f"studies/public/study/{accession}")
    record = extract_study_metadata(payload)
    if record["accession"] != accession:

        raise ValueError(
            f"requested {accession} but payload identifies as {record['accession']}"
        )
    return record


def get_studies_metadata(accessions: Iterable[str],
                         client: Optional[MetaboLightsClient] = None,
                         skip_missing: bool = False) -> List[Dict[str, Any]]:
    """Fetch a list of studies and return records in deterministic (numeric accession) order.

    Duplicate accessions are de-duplicated. With ``skip_missing=True``, private/missing
    studies are silently dropped instead of raising.
    """
    from .client import MetaboLightsNotFoundError

    client = client or MetaboLightsClient()
    unique = sorted({a.strip().upper() for a in accessions}, key=_accession_sort_key)
    records: List[Dict[str, Any]] = []
    for acc in unique:
        try:
            records.append(get_study_metadata(acc, client=client))
        except MetaboLightsNotFoundError:
            if not skip_missing:
                raise
    return records


def list_public_studies(client: Optional[MetaboLightsClient] = None) -> Dict[str, Any]:
    """Retrieve the full public study accession list.

    Returns ``{"accessions": [...sorted numerically...], "count": <len>,
    "reported_count": <the API's own 'studies' field>}``. ``count`` and
    ``reported_count`` should always agree; the accuracy gate checks this.
    """
    client = client or MetaboLightsClient()
    payload = client.get_json("studies")
    raw = payload.get("content") or []
    accessions = sorted({a.strip().upper() for a in raw if _clean_str(a)},
                        key=_accession_sort_key)
    return {
        "accessions": accessions,
        "count": len(accessions),
        "reported_count": payload.get("studies"),
    }
