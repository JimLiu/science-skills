"""Structured term records extracted from OLS4 v1 term JSON, plus canonical serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict


RELATIONS = (
    "parents",
    "children",
    "ancestors",
    "descendants",
    "hierarchicalParents",
    "hierarchicalChildren",
    "hierarchicalAncestors",
    "hierarchicalDescendants",
)


@dataclass
class TermRecord:
    """Structured representation of one ontology term.

    ``parents`` is ``None`` when parent links were not requested, and a (possibly empty)
    list of ``{"curie", "iri", "label"}`` dicts when they were.
    """

    curie: str | None
    iri: str
    label: str | None
    ontology: str | None
    short_form: str | None
    synonyms: list[str] = field(default_factory=list)
    description: list[str] = field(default_factory=list)
    is_obsolete: bool = False
    has_children: bool = False
    parents: list[dict] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parent_ref(term: dict) -> dict:
    """Compact reference to a parent term."""
    return {
        "curie": term.get("obo_id") or term.get("short_form"),
        "iri": term.get("iri"),
        "label": term.get("label"),
    }


def record_from_v1(term: dict, parents: list[dict] | None = None) -> TermRecord:
    """Build a TermRecord from an OLS4 v1 ``terms`` JSON object."""
    synonyms = sorted(set(term.get("synonyms") or []))
    description = list(term.get("description") or [])
    sorted_parents = None
    if parents is not None:
        sorted_parents = sorted(parents, key=lambda p: (p.get("curie") or "", p.get("iri") or ""))
    return TermRecord(
        curie=term.get("obo_id") or term.get("short_form"),
        iri=term["iri"],
        label=term.get("label"),
        ontology=term.get("ontology_name"),
        short_form=term.get("short_form"),
        synonyms=synonyms,
        description=description,
        is_obsolete=bool(term.get("is_obsolete", False)),
        has_children=bool(term.get("has_children", False)),
        parents=sorted_parents,
    )


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, compact separators, no ASCII escaping."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
