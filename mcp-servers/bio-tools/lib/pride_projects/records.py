"""Normalization of PRIDE Archive v2 payloads into structured project records.

Two upstream representations exist for the same project:

* the search index (``/search/projects``) returns flat string lists for
  organisms / instruments / diseases / etc. and references as a single
  ``"<line>--pubMed:<id>--doi: <doi>"`` string;
* the project detail endpoint (``/projects/{accession}``) returns CvParam
  objects (``{"accession": "NEWT:9606", "name": "Homo sapiens (human)", ...}``)
  and references as ``{"referenceLine", "pubmedID", "doi"}`` objects.

Both are normalized to the same record shape so the accuracy gate can compare
search-derived records against the single-project endpoint field by field.

Record shape (all keys always present):

    accession            str
    title                str
    organisms            [str]  (sorted, deduplicated names)
    organism_parts       [str]
    diseases             [str]
    instruments          [str]
    experiment_types     [str]
    softwares            [str]
    quantification_methods [str]
    keywords             [str]  (empty strings dropped)
    project_tags         [str]
    submission_date      str | None   (YYYY-MM-DD)
    publication_date     str | None   (YYYY-MM-DD)
    submitters           [str]  (display names)
    lab_pis              [str]
    affiliations         [str]
    references           [{"pubmed_id": int|None, "doi": str|None, "reference_line": str}]
    source               "search" | "detail"
"""

from __future__ import annotations

import re

_REF_SEARCH_RE = re.compile(
    r"^(?P<line>.*?)--pubMed:(?P<pm>[^-]*)--doi:\s*(?P<doi>.*)$", re.S
)


def _names(values) -> list[str]:
    """Extract a sorted, deduplicated list of display names.

    Accepts a list of plain strings (search index) or CvParam-like dicts
    (detail endpoint). Empty strings and None entries are dropped.
    """
    out = set()
    for v in values or []:
        if isinstance(v, dict):
            name = v.get("name") or ""
        else:
            name = str(v) if v is not None else ""
        name = name.strip()
        if name:
            out.add(name)
    return sorted(out)


def _person_names(values) -> list[str]:
    """Submitters / lab heads: search gives strings, detail gives contact dicts."""
    out = []
    for v in values or []:
        if isinstance(v, dict):
            name = (v.get("name") or "").strip()
            if not name:
                name = " ".join(
                    x for x in [(v.get("firstName") or "").strip(), (v.get("lastName") or "").strip()] if x
                )
        else:
            name = str(v).strip()
        if name:
            out.append(name)
    return sorted(set(out))


def _date(value) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _norm_doi(doi) -> str | None:
    if doi is None:
        return None
    d = str(doi).strip()
    if not d or d.lower() in {"null", "none"}:
        return None
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    d = re.sub(r"^doi:\s*", "", d, flags=re.I)
    return d.lower() or None


def _norm_pubmed(pm) -> int | None:
    if pm is None:
        return None
    s = str(pm).strip()
    if not s or s.lower() in {"null", "none", "0"}:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _references(values) -> list[dict]:
    """Normalize references from either representation, sorted for determinism."""
    refs = []
    for v in values or []:
        if isinstance(v, dict):
            refs.append(
                {
                    "pubmed_id": _norm_pubmed(v.get("pubmedID")),
                    "doi": _norm_doi(v.get("doi")),
                    "reference_line": (v.get("referenceLine") or "").strip(),
                }
            )
        else:
            text = str(v)
            m = _REF_SEARCH_RE.match(text)
            if m:
                refs.append(
                    {
                        "pubmed_id": _norm_pubmed(m.group("pm")),
                        "doi": _norm_doi(m.group("doi")),
                        "reference_line": m.group("line").strip(),
                    }
                )
            else:
                refs.append({"pubmed_id": None, "doi": None, "reference_line": text.strip()})
    refs.sort(key=lambda r: (r["pubmed_id"] or 0, r["doi"] or "", r["reference_line"]))
    return refs


def normalize_search_record(raw: dict) -> dict:
    """Normalize one item of the ``/search/projects`` response."""
    return {
        "accession": raw.get("accession"),
        "title": (raw.get("title") or "").strip(),
        "organisms": _names(raw.get("organisms")),
        "organism_parts": _names(raw.get("organismsPart")),
        "diseases": _names(raw.get("diseases")),
        "instruments": _names(raw.get("instruments")),
        "experiment_types": _names(raw.get("experimentTypes")),
        "softwares": _names(raw.get("softwares")),
        "quantification_methods": _names(raw.get("quantificationMethods")),
        "keywords": _names(raw.get("keywords")),
        "project_tags": _names(raw.get("projectTags")),
        "submission_date": _date(raw.get("submissionDate")),
        "publication_date": _date(raw.get("publicationDate")),
        "submitters": _person_names(raw.get("submitters")),
        "lab_pis": _person_names(raw.get("labPIs")),
        "affiliations": _names(raw.get("affiliations")),
        "references": _references(raw.get("references")),
        "source": "search",
    }


def normalize_detail_record(raw: dict) -> dict:
    """Normalize the ``/projects/{accession}`` response."""
    return {
        "accession": raw.get("accession"),
        "title": (raw.get("title") or "").strip(),
        "organisms": _names(raw.get("organisms")),
        "organism_parts": _names(raw.get("organismParts")),
        "diseases": _names(raw.get("diseases")),
        "instruments": _names(raw.get("instruments")),
        "experiment_types": _names(raw.get("experimentTypes")),
        "softwares": _names(raw.get("softwares")),
        "quantification_methods": _names(raw.get("quantificationMethods")),
        "keywords": _names(raw.get("keywords")),
        "project_tags": _names(raw.get("projectTags")),
        "submission_date": _date(raw.get("submissionDate")),
        "publication_date": _date(raw.get("publicationDate")),
        "submitters": _person_names(raw.get("submitters")),
        "lab_pis": _person_names(raw.get("labPIs")),
        "affiliations": _names(raw.get("affiliations")),
        "references": _references(raw.get("references")),
        "source": "detail",
    }





COMPARABLE_FIELDS = [
    "accession",
    "title",
    "organisms",
    "organism_parts",
    "diseases",
    "instruments",
    "experiment_types",
    "quantification_methods",
    "submission_date",
    "publication_date",
    "submitters",
    "lab_pis",
]


def comparable_view(record: dict, fields=None) -> dict:
    fields = fields or COMPARABLE_FIELDS
    return {k: record.get(k) for k in fields}
