"""ChEBI retrieval methods (entity fetch, search) over :class:`ChebiClient`.

Raw backend payloads are large (every synonym in every language, full
citation lists); this module normalizes them into lean, deterministic
records. Caps live in the tier-2 server.
"""

from __future__ import annotations

import re

from .client import ChebiClient

_CHEBI_RE = re.compile(r"^(?:CHEBI:)?(\d+)$", re.IGNORECASE)


def normalize_chebi_id(chebi_id: str | int) -> int:
    """Accept ``CHEBI:27732`` / ``27732`` / 27732; return the integer ID."""
    m = _CHEBI_RE.match(str(chebi_id).strip())
    if not m:
        raise ValueError(f"not a ChEBI ID: {chebi_id!r}")
    return int(m.group(1))


class ChebiOntology:
    """High-level ChEBI entity retrieval."""

    def __init__(self, client: ChebiClient | None = None):
        self.client = client or ChebiClient()

    def get_compound(self, chebi_id: str | int) -> dict:
        """Full normalized entity record (raises NotFound for unknown IDs).

        Secondary ChEBI IDs resolve: the backend serves the primary record
        and lists the requested ID under ``secondary_ids``.
        """
        num = normalize_chebi_id(chebi_id)
        raw = self.client.get_json(f"compound/{num}/")
        return _normalize_compound(raw)

    def search(self, term: str, size: int = 20, page: int = 1) -> dict:
        """Full-text search (names, synonyms, formula, InChIKey...).

        Returns ``{term, page, size, api_total, number_pages, results}``;
        ``api_total`` is the API's own total hit count, so callers can flag
        truncation honestly.
        """
        if not term or not term.strip():
            raise ValueError("term must be non-empty")
        if not (1 <= size <= 100):
            raise ValueError("size must be in [1, 100]")
        raw = self.client.get_json(
            "es_search/", params={"term": term.strip(), "size": size,
                                  "page": page})
        results = []
        for hit in raw.get("results", []):
            src = hit.get("_source", {})
            results.append({
                "chebi_accession": src.get("chebi_accession"),
                "name": src.get("name"),
                "definition": src.get("definition"),
                "stars": src.get("stars"),
                "formula": src.get("formula"),
                "charge": src.get("charge"),
                "mass": src.get("mass"),
                "monoisotopic_mass": src.get("monoisotopicmass"),
                "smiles": src.get("smiles"),
                "inchikey": src.get("inchikey"),
                "score": hit.get("_score"),
            })
        return {"term": term.strip(), "page": page, "size": size,
                "api_total": raw.get("total"),
                "number_pages": raw.get("number_pages"),
                "results": results}


def _normalize_compound(raw: dict) -> dict:
    names = raw.get("names") or {}
    synonyms = [n.get("name") for n in names.get("SYNONYM", [])
                if n.get("name")]
    iupac_names = [n.get("name") for n in names.get("IUPAC NAME", [])
                   if n.get("name")]
    chem = raw.get("chemical_data") or {}
    structure = raw.get("default_structure") or {}

    xrefs: list[dict] = []
    for xref_type, entries in sorted((raw.get("database_accessions") or {}).items()):
        for entry in entries:
            xrefs.append({"type": xref_type,
                          "accession": entry.get("accession_number"),
                          "source": entry.get("source_name"),
                          "url": entry.get("url")})

    relations = raw.get("ontology_relations") or {}


    outgoing = [_normalize_relation(r) for r in relations.get("outgoing_relations", [])]
    incoming = [_normalize_relation(r) for r in relations.get("incoming_relations", [])]

    roles = [{"chebi_accession": r.get("chebi_accession"),
              "name": r.get("name"),
              "definition": r.get("definition")}
             for r in (raw.get("roles_classification") or [])]

    return {
        "chebi_accession": raw.get("chebi_accession"),
        "name": raw.get("name"),
        "definition": raw.get("definition"),
        "stars": raw.get("stars"),
        "formula": chem.get("formula"),
        "charge": chem.get("charge"),
        "mass": chem.get("mass"),
        "monoisotopic_mass": chem.get("monoisotopic_mass"),
        "smiles": structure.get("smiles"),
        "inchi": structure.get("standard_inchi"),
        "inchikey": structure.get("standard_inchi_key"),
        "iupac_names": iupac_names,
        "synonyms": synonyms,
        "secondary_ids": list(raw.get("secondary_ids") or []),
        "xrefs": xrefs,
        "outgoing_relations": outgoing,
        "incoming_relations": incoming,
        "roles": roles,
        "modified_on": raw.get("modified_on"),
        "is_released": raw.get("is_released"),
    }


def _normalize_relation(rel: dict) -> dict:
    return {"relation_type": rel.get("relation_type"),
            "init_chebi_id": rel.get("init_id"),
            "init_name": rel.get("init_name"),
            "final_chebi_id": rel.get("final_id"),
            "final_name": rel.get("final_name")}
