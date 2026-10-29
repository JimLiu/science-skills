"""Parsing of raw OLS4 ontology JSON into structured records, plus canonicalization."""
from __future__ import annotations

import json
from typing import Any, Optional


RECORD_FIELDS = [
    "ontology_id",
    "title",
    "version",
    "version_iri",
    "loaded",
    "updated",
    "status",
    "num_terms",
    "num_properties",
    "num_individuals",
    "homepage",
    "preferred_prefix",
    "namespace",
    "file_location",
    "languages",
    "description",
]

DESCRIPTION_MAX_CHARS = 300


def parse_ontology_record(raw: dict) -> dict:
    """Convert one raw OLS4 ontology object (catalogue item or single-ID
    response — they share the same shape) into a structured record.

    Field provenance:
      top-level keys : ontologyId, loaded, updated, status,
                       numberOfTerms, numberOfProperties, numberOfIndividuals,
                       version (falls back to config.version), languages
      config.* keys  : title, versionIri, preferredPrefix, homepage,
                       namespace, fileLocation, description
    No values are invented: a missing/null upstream value stays None.
    """
    cfg = raw.get("config") or {}
    desc: Optional[str] = cfg.get("description")
    if isinstance(desc, str) and len(desc) > DESCRIPTION_MAX_CHARS:
        desc = desc[:DESCRIPTION_MAX_CHARS] + "..."
    version = raw.get("version")
    if version is None:
        version = cfg.get("version")
    languages = raw.get("languages")
    if isinstance(languages, list):
        languages = sorted(languages)
    record = {
        "ontology_id": raw.get("ontologyId"),
        "title": cfg.get("title"),
        "version": version,
        "version_iri": cfg.get("versionIri"),
        "loaded": raw.get("loaded"),
        "updated": raw.get("updated"),
        "status": raw.get("status"),
        "num_terms": raw.get("numberOfTerms"),
        "num_properties": raw.get("numberOfProperties"),
        "num_individuals": raw.get("numberOfIndividuals"),
        "homepage": cfg.get("homepage"),
        "preferred_prefix": cfg.get("preferredPrefix"),
        "namespace": cfg.get("namespace"),
        "file_location": cfg.get("fileLocation"),
        "languages": languages,
        "description": desc,
    }
    return {k: record[k] for k in RECORD_FIELDS}


def strip_links(raw: dict) -> dict:
    """Drop the volatile HAL `_links` block from a raw ontology object."""
    return {k: v for k, v in raw.items() if k != "_links"}


def canonicalize(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, compact separators, UTF-8.

    Used for run-to-run identity comparison and token counting.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
