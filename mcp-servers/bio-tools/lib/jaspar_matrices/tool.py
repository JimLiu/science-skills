"""High-level surface mirroring the 8 tooluniverse/jaspar MCP methods.

Every list route is fully paginated (DRF page-number pagination, page_size
pinned to the server cap of 1000) and count-verified: the number of rows
retrieved must equal the API's own ``count`` field, else JasparApiError.

UPSTREAM TRAP (verified 2026-06-08): ``/matrix/?species=<tax_id>`` is silently
IGNORED by the server — the working species filter is ``tax_id=``. This module
accepts ``species=`` for MCP-surface compatibility and maps it to ``tax_id=``
upstream, so callers get the filter they asked for.
"""
from __future__ import annotations

from mcp_servers_common.pathseg import seg

from .client import JasparClient, JasparApiError

PAGE_SIZE = 1000


def _walk(client: JasparClient, path: str, params: dict) -> dict:
    """Full DRF pagination walk; returns {count, results} with completeness check."""
    params = {k: v for k, v in params.items() if v is not None}
    params["page_size"] = PAGE_SIZE
    payload = client.get_json(path, params=params)
    count = payload["count"]
    results = list(payload["results"])
    next_url = payload.get("next")
    while next_url:
        payload = client.get_json(next_url)
        results.extend(payload["results"])
        next_url = payload.get("next")
    if len(results) != count:
        raise JasparApiError(
            f"pagination walk of {path} returned {len(results)} rows "
            f"but API count={count}")
    return {"count": count, "results": results}




def get_matrix(client: JasparClient, matrix_id: str) -> dict:
    """Full record for one versioned matrix ID (e.g. 'MA0002.2'), incl. PFM."""
    if "." not in matrix_id:
        raise ValueError(
            f"{matrix_id!r} is a base ID, not a versioned matrix ID; "
            "use matrix_versions() to enumerate versions")
    return client.get_json(f"/matrix/{seg(matrix_id)}/")


def matrix_versions(client: JasparClient, base_id: str) -> dict:
    """All versions for a base ID (e.g. 'MA0002'); count-verified."""
    if "." in base_id:
        base_id = base_id.split(".")[0]
    return _walk(client, f"/matrix/{seg(base_id)}/versions/", {})




def list_matrices(client: JasparClient, collection: str | None = None,
                  tax_group: str | None = None, tax_id: int | str | None = None,
                  species: int | str | None = None, name: str | None = None,
                  search: str | None = None, version: str | None = None) -> dict:
    """Filtered, fully-paginated profile listing.

    ``species`` is an alias for ``tax_id`` (the upstream ``species=`` query
    parameter is silently ignored by the server; ``tax_id=`` is the filter
    that actually works). ``version='latest'`` restricts to latest versions.
    With no filters this is the full TF profile listing
    (tooluniverse JASPAR_get_transcription_factors).
    """
    if species is not None:
        if tax_id is not None and str(tax_id) != str(species):
            raise ValueError("pass either tax_id or species, not conflicting both")
        tax_id = species
    return _walk(client, "/matrix/", {
        "collection": collection, "tax_group": tax_group, "tax_id": tax_id,
        "name": name, "search": search, "version": version,
    })




def list_species(client: JasparClient) -> dict:
    """All species with profiles in JASPAR (tax_id + name); count-verified."""
    return _walk(client, "/species/", {})


def list_taxa(client: JasparClient) -> dict:
    """All taxonomic groups (e.g. vertebrates, plants); count-verified."""
    return _walk(client, "/taxon/", {})


def list_collections(client: JasparClient) -> dict:
    """All collections (CORE, UNVALIDATED, ...); count-verified."""
    return _walk(client, "/collections/", {})


def list_releases(client: JasparClient) -> dict:
    """All JASPAR releases (year, release_number, active flag); count-verified."""
    return _walk(client, "/releases/", {})
