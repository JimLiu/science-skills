"""Attribute search against search.rcsb.org — count-verified paged retrieval.

The RCSB search API returns only identifiers + relevance scores; callers chain
to the data API (``fetch_entry_records``) for metadata. Zero results arrive as
HTTP 204 (no body) and requesting a page past the end returns a body without
``result_set`` — both are handled explicitly here.
"""
from __future__ import annotations

from .client import PDBClient, PDBError

PAGE_ROWS = 100
MAX_ROWS_LIMIT = 1000


EXPERIMENTAL_METHODS = {
    "X-RAY DIFFRACTION",
    "ELECTRON MICROSCOPY",
    "SOLUTION NMR",
    "SOLID-STATE NMR",
    "NEUTRON DIFFRACTION",
    "ELECTRON CRYSTALLOGRAPHY",
    "FIBER DIFFRACTION",
    "POWDER DIFFRACTION",
    "SOLUTION SCATTERING",
    "EPR",
    "INFRARED SPECTROSCOPY",
    "FLUORESCENCE TRANSFER",
    "THEORETICAL MODEL",
}


def _text_node(attribute: str, operator: str, value) -> dict:
    return {
        "type": "terminal",
        "service": "text",
        "parameters": {"attribute": attribute, "operator": operator, "value": value},
    }


def build_query(
    *,
    text: str | None = None,
    organism: str | None = None,
    taxonomy_id: int | None = None,
    uniprot_accession: str | None = None,
    experimental_method: str | None = None,
    max_resolution: float | None = None,
    ligand_comp_id: str | None = None,
) -> dict:
    """Build the search-API query node from the supported filters."""
    nodes: list[dict] = []
    if text:
        nodes.append({"type": "terminal", "service": "full_text",
                      "parameters": {"value": text}})
    if organism:
        nodes.append(_text_node(
            "rcsb_entity_source_organism.taxonomy_lineage.name",
            "exact_match", organism))
    if taxonomy_id is not None:
        nodes.append(_text_node(
            "rcsb_entity_source_organism.ncbi_taxonomy_id",
            "equals", int(taxonomy_id)))
    if uniprot_accession:
        nodes.append(_text_node(
            "rcsb_polymer_entity_container_identifiers"
            ".reference_sequence_identifiers.database_accession",
            "exact_match", uniprot_accession))
        nodes.append(_text_node(
            "rcsb_polymer_entity_container_identifiers"
            ".reference_sequence_identifiers.database_name",
            "exact_match", "UniProt"))
    if experimental_method:
        method = experimental_method.upper()
        if method not in EXPERIMENTAL_METHODS:
            raise ValueError(
                f"unknown experimental_method {experimental_method!r}; "
                f"one of: {', '.join(sorted(EXPERIMENTAL_METHODS))}")
        nodes.append(_text_node("exptl.method", "exact_match", method))
    if max_resolution is not None:
        nodes.append(_text_node(
            "rcsb_entry_info.resolution_combined",
            "less_or_equal", float(max_resolution)))
    if ligand_comp_id:
        nodes.append(_text_node(
            "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
            "exact_match", ligand_comp_id.upper()))

    if not nodes:
        raise ValueError(
            "at least one search criterion is required "
            "(text / organism / taxonomy_id / uniprot_accession / "
            "experimental_method / max_resolution / ligand_comp_id)")
    if len(nodes) == 1:
        return nodes[0]
    return {"type": "group", "logical_operator": "and", "nodes": nodes}


def search_structures(
    client: PDBClient,
    *,
    text: str | None = None,
    organism: str | None = None,
    taxonomy_id: int | None = None,
    uniprot_accession: str | None = None,
    experimental_method: str | None = None,
    max_resolution: float | None = None,
    ligand_comp_id: str | None = None,
    include_computed_models: bool = False,
    max_rows: int = 100,
) -> dict:
    """Paged PDB entry search; honest truncation against the API's own total.

    Returns ``{"total_count", "n_retrieved", "truncated", "records"}`` where
    records are ``{"pdb_id", "score"}`` in relevance order. ``truncated`` is
    true iff ``total_count > n_retrieved`` (capped by ``max_rows``); when the
    sweep is not truncated the retrieved row count is verified against
    ``total_count`` and a mismatch raises :class:`PDBError` — silent
    truncation is impossible.
    """
    if not 1 <= max_rows <= MAX_ROWS_LIMIT:
        raise ValueError(f"max_rows must be 1..{MAX_ROWS_LIMIT}")
    query = build_query(
        text=text, organism=organism, taxonomy_id=taxonomy_id,
        uniprot_accession=uniprot_accession,
        experimental_method=experimental_method,
        max_resolution=max_resolution, ligand_comp_id=ligand_comp_id)
    content_types = ["experimental"]
    if include_computed_models:
        content_types.append("computational")

    records: list[dict] = []
    total_count: int | None = None
    start = 0
    while True:
        rows = min(PAGE_ROWS, max_rows - len(records))
        payload = {
            "query": query,
            "return_type": "entry",
            "request_options": {
                "paginate": {"start": start, "rows": rows},
                "results_content_type": content_types,
            },
        }
        body = client.post_search(payload)
        if body is None:
            if records:
                raise PDBError("search API returned 204 mid-sweep")
            total_count = 0
            break
        total_count = body["total_count"]
        page = body.get("result_set", [])
        records.extend({"pdb_id": r["identifier"], "score": r.get("score")}
                       for r in page)
        start += len(page)
        if len(records) >= min(total_count, max_rows):
            break
        if not page:
            raise PDBError(
                f"search API returned an empty page at start={start} with "
                f"total_count={total_count} — count verification failed")

    truncated = total_count > len(records)
    if not truncated and len(records) != total_count:
        raise PDBError(
            f"retrieved {len(records)} rows but API reports total_count="
            f"{total_count} — count verification failed")
    return {
        "total_count": total_count,
        "n_retrieved": len(records),
        "truncated": truncated,
        "records": records,
    }
