"""CellGuide MCP server — cell type information from CellXGene."""

from typing import Any

from .client import CellGuideClient
from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank
from mcp_servers_common.errors import raise_on_error_payload
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations
from .resolver import CellTypeResolver



mcp = GuardedFastMCP("cellguide")


_client = CellGuideClient()
_resolver = CellTypeResolver(_client)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@raise_on_error_payload
async def get_cell_type_info(cell_type: str) -> dict[str, Any]:
    """Get detailed information about a cell type including description, synonyms, and basic metadata.

    Use this to learn about what a cell type is.

    Args:
        cell_type: Cell type ID (e.g., CL:0000622) or name (e.g., 'acinar cell')
    """
    cell_id, basic_info = await _resolver.resolve_with_info(cell_type)

    if not cell_id or not basic_info:
        similar = await _client.search_cell_types(cell_type, limit=5)
        if similar:
            suggestions = [f"{c['name']} ({c['id']})" for c in similar]
            return {"error": f"Cell type '{cell_type}' not found", "suggestions": suggestions}
        return {"error": f"Cell type '{cell_type}' not found. No similar cell types found."}

    description_data = await _client.get_description(cell_id)
    markers = await _client.get_marker_genes(cell_id, limit=5)
    top_markers = [{"symbol": m.get("symbol"), "name": m.get("name")} for m in markers]
    tissues = await _client.get_cell_tissues(cell_id)
    tissue_names = [t["name"] for t in tissues[:10]]

    return {
        "id": cell_id,
        "name": basic_info.get("name", ""),
        "synonyms": basic_info.get("synonyms", []),
        "ontology_description": basic_info.get("clDescription", ""),
        "description": description_data.get("description", ""),
        "description_source": description_data.get("source", ""),
        "references": description_data.get("references", []),
        "top_marker_genes": top_markers,
        "found_in_tissues": tissue_names,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def search_cell_types(query: str, limit: int = 10) -> dict[str, Any]:
    """Search for cell types by name or synonym.

    Use this when you need to find cell types matching a query.

    Args:
        query: Search query (e.g., 'acinar', 'T cell', 'neuron')
        limit: Maximum number of results

    Returns ``{result: [{id, name, synonyms, ontology_description}, ...]}``.
    """


    if blank(query):
        raise ValueError("query must be non-empty")
    matches = await _client.search_cell_types(query, limit=limit)

    results = [
        {
            "id": m.get("id"),
            "name": m.get("name"),
            "synonyms": m.get("synonyms", []),
            "ontology_description": m.get("clDescription", ""),
        }
        for m in matches
    ]





    return {"result": results}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@raise_on_error_payload
async def get_marker_genes(cell_type: str, marker_type: str = "computational", limit: int = 20) -> dict[str, Any]:
    """Get marker genes for a cell type. Returns genes that are specifically expressed in this cell type.

    Args:
        cell_type: Cell type ID or name
        marker_type: 'computational' (data-derived) or 'canonical' (literature-based)
        limit: Maximum number of genes to return
    """



    if marker_type not in ("computational", "canonical"):
        raise ValueError(
            f"marker_type must be 'computational' or 'canonical', "
            f"got {marker_type!r}")
    cell_id = await _resolver.resolve(cell_type)

    if not cell_id:
        return {"error": f"Cell type '{cell_type}' not found"}

    info = await _client.get_cell_info(cell_id)
    genes = await _client.get_marker_genes(cell_id, marker_type, limit)

    formatted_genes = []
    for g in genes:
        gene_info = {
            "symbol": g.get("symbol"),
            "name": g.get("name"),
            "marker_score": round(g.get("marker_score", 0), 3),
            "specificity": round(g.get("specificity", 0), 3),
            "mean_expression": round(g.get("me", 0), 3),
            "percent_cells": round(g.get("pc", 0) * 100, 1),
        }
        dims = g.get("groupby_dims", {})
        if dims:
            gene_info["organism"] = dims.get("organism_ontology_term_label", "")
            gene_info["tissue_context"] = dims.get("tissue_ontology_term_label", "")
        formatted_genes.append(gene_info)

    return {
        "cell_type_id": cell_id,
        "cell_type_name": info.get("name", "") if info else "",
        "marker_type": marker_type,
        "genes_found": len(formatted_genes),
        "marker_genes": formatted_genes,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@raise_on_error_payload
async def get_source_data(cell_type: str) -> dict[str, Any]:
    """Get source datasets and publications for a cell type. Shows where cell type data comes from.

    Args:
        cell_type: Cell type ID or name
    """
    cell_id = await _resolver.resolve(cell_type)

    if not cell_id:
        return {"error": f"Cell type '{cell_type}' not found"}

    info = await _client.get_cell_info(cell_id)
    collections = await _client.get_source_collections(cell_id)

    formatted_collections = []
    for c in collections:
        formatted_collections.append(
            {
                "collection_name": c.get("collection_name"),
                "collection_url": c.get("collection_url"),
                "publication_title": c.get("publication_title"),
                "publication_url": c.get("publication_url"),
                "tissues": [t.get("label") for t in c.get("tissue", [])],
                "diseases": [d.get("label") for d in c.get("disease", [])],
                "organisms": [o.get("label") for o in c.get("organism", [])],
            }
        )

    return {
        "cell_type_id": cell_id,
        "cell_type_name": info.get("name", "") if info else "",
        "num_collections": len(formatted_collections),
        "source_collections": formatted_collections,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@raise_on_error_payload
async def get_cell_tissues(cell_type: str) -> dict[str, Any]:
    """Get tissues where a cell type is found. Shows the anatomical locations of a cell type.

    Args:
        cell_type: Cell type ID or name
    """
    cell_id = await _resolver.resolve(cell_type)

    if not cell_id:
        return {"error": f"Cell type '{cell_type}' not found"}

    info = await _client.get_cell_info(cell_id)
    tissues = await _client.get_cell_tissues(cell_id)

    return {
        "cell_type_id": cell_id,
        "cell_type_name": info.get("name", "") if info else "",
        "num_tissues": len(tissues),
        "tissues": tissues,
    }


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
