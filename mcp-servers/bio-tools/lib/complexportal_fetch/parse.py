"""Parsing of raw Complex Portal web-service JSON into structured records.

All collections are sorted deterministically so repeated runs produce
byte-identical output.
"""
from __future__ import annotations

import re

GO_DATABASE_NAME = "gene ontology"

_STOICH_RE = re.compile(r"minValue:\s*(\d+)\s*,\s*maxValue:\s*(\d+)")
_CPX_NUM_RE = re.compile(r"CPX-(\d+)$")


def parse_stoichiometry(raw: str | None) -> tuple[int | None, int | None]:
    """'minValue: 2, maxValue: 2' -> (2, 2); None/unparseable -> (None, None)."""
    if not raw:
        return (None, None)
    m = _STOICH_RE.search(raw)
    if not m:
        return (None, None)
    return (int(m.group(1)), int(m.group(2)))


def split_species(raw: str | None) -> tuple[str | None, int | None]:
    """'Homo sapiens; 9606' -> ('Homo sapiens', 9606)."""
    if not raw:
        return (None, None)
    if ";" in raw:
        name, _, tax = raw.rpartition(";")
        tax = tax.strip()
        try:
            return (name.strip(), int(tax))
        except ValueError:
            return (raw.strip(), None)
    return (raw.strip(), None)


def complex_ac_sort_key(ac: str) -> tuple:
    """Numeric sort key for CPX accessions ('CPX-915' < 'CPX-2158')."""
    m = _CPX_NUM_RE.match(ac or "")
    if m:
        return (0, int(m.group(1)))
    return (1, ac or "")


def _participant_sort_key(p: dict) -> tuple:
    return (p.get("interactor_type") or "", p.get("identifier") or "", p.get("name") or "")


def _xref_sort_key(x: dict) -> tuple:
    return (x.get("database") or "", x.get("identifier") or "", x.get("qualifier") or "")


def parse_participant(raw: dict) -> dict:
    smin, smax = parse_stoichiometry(raw.get("stochiometry"))
    return {
        "identifier": raw.get("identifier"),
        "name": raw.get("name"),
        "description": raw.get("description"),
        "interactor_type": raw.get("interactorType"),
        "interactor_type_mi": raw.get("interactorTypeMI"),
        "biological_role": raw.get("bioRole"),
        "biological_role_mi": raw.get("bioRoleMI"),
        "stoichiometry_min": smin,
        "stoichiometry_max": smax,
        "stoichiometry_raw": raw.get("stochiometry"),
    }


def parse_complex(raw: dict) -> dict:
    """Parse a /complex/{AC} response into the structured record."""
    species_name, taxid = split_species(raw.get("species"))

    participants = sorted(
        (parse_participant(p) for p in raw.get("participants") or []),
        key=_participant_sort_key,
    )

    go_annotations: list[dict] = []
    cross_references: list[dict] = []
    for x in raw.get("crossReferences") or []:
        db = (x.get("database") or "").strip()
        entry = {
            "database": db,
            "identifier": x.get("identifier"),
            "qualifier": x.get("qualifier"),
            "description": x.get("description"),
        }
        if db.lower() == GO_DATABASE_NAME:
            go_annotations.append(
                {
                    "go_id": x.get("identifier"),
                    "aspect": x.get("qualifier"),
                    "term": x.get("description"),
                }
            )
        else:
            cross_references.append(entry)
    go_annotations.sort(key=lambda g: (g.get("go_id") or "", g.get("aspect") or ""))
    cross_references.sort(key=_xref_sort_key)

    evidence_raw = raw.get("evidenceType") or {}
    evidence = {
        "eco_code": evidence_raw.get("identifier"),
        "description": evidence_raw.get("description"),
        "confidence_score": evidence_raw.get("confidenceScore"),
    }

    return {
        "complex_ac": raw.get("complexAc"),
        "intact_ac": raw.get("ac"),
        "name": raw.get("name"),
        "systematic_name": raw.get("systematicName"),
        "synonyms": sorted(raw.get("synonyms") or []),
        "species_name": species_name,
        "taxid": taxid,
        "predicted_complex": raw.get("predictedComplex"),
        "evidence": evidence,
        "participants": participants,
        "go_annotations": go_annotations,
        "cross_references": cross_references,
        "functions": list(raw.get("functions") or []),
        "complex_assemblies": list(raw.get("complexAssemblies") or []),
        "release_dates": sorted(raw.get("releaseDates") or []),
    }


def parse_search_element(raw: dict) -> dict:
    """Parse one element of a /search/ response into a compact record."""
    species_name, taxid = split_species(raw.get("organismName"))
    interactors = sorted(
        (
            {
                "identifier": i.get("identifier"),
                "name": i.get("name"),
                "interactor_type": i.get("interactorType"),
                "stoichiometry_raw": i.get("stochiometry"),
            }
            for i in raw.get("interactors") or []
        ),
        key=lambda i: (i.get("interactor_type") or "", i.get("identifier") or ""),
    )
    return {
        "complex_ac": raw.get("complexAC"),
        "name": raw.get("complexName"),
        "species_name": species_name,
        "taxid": taxid,
        "predicted_complex": raw.get("predictedComplex"),
        "interactors": interactors,
    }
