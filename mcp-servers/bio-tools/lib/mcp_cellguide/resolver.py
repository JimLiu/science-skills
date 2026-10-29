"""
Cell Type Resolver - Resolve natural language cell type queries to ontology IDs.

Current implementation: Pass-through with ID normalization and name search.

Future enhancement: Embedding-based lookup where all Cell Ontology (CL) terms
are embedded. User queries like "pancreas digestive cell" would resolve to
CL:0000622 (acinar cell) via semantic similarity.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import CellGuideClient


def normalize_cell_id(cell_id: str) -> str | None:
    """
    Normalize a cell type ID to the standard colon format (CL:0000622).

    Args:
        cell_id: Cell ID in any format

    Returns:
        Normalized ID (CL:XXXXXXX) or None if not a valid ID
    """

    if re.match(r"^CL:\d{7}$", cell_id):
        return cell_id


    if re.match(r"^CL_\d{7}$", cell_id):
        return cell_id.replace("_", ":")


    if re.match(r"^\d{7}$", cell_id):
        return f"CL:{cell_id}"

    return None


class CellTypeResolver:
    """
    Resolve cell type queries to ontology IDs.

    Current behavior:
    1. If input is a valid CL ID (any format), normalize and return it
    2. Otherwise, search cell type metadata by name/synonym
    3. Return the best match or None

    Future enhancement:
    - Build embedding index from all cell type names/synonyms/descriptions
    - Use semantic similarity for natural language queries
    """

    def __init__(self, client: "CellGuideClient"):
        """
        Initialize the resolver.

        Args:
            client: CellGuide API client for fetching metadata
        """
        self.client = client

    async def resolve(self, query: str) -> str | None:
        """
        Resolve a query to a cell type ID.

        Args:
            query: Cell type ID or name query

        Returns:
            Normalized cell type ID (CL:XXXXXXX) or None if not found
        """
        query = query.strip()




        if not query:
            return None


        normalized = normalize_cell_id(query)
        if normalized:

            info = await self.client.get_cell_info(normalized)
            if info:
                return normalized

            return None


        matches = await self.client.search_cell_types(query, limit=1)
        if matches:
            return matches[0].get("id")

        return None

    async def resolve_with_info(self, query: str) -> tuple[str | None, dict | None]:
        """
        Resolve a query and return both the ID and cell info.

        Args:
            query: Cell type ID or name query

        Returns:
            Tuple of (cell_id, cell_info) or (None, None) if not found
        """
        cell_id = await self.resolve(query)
        if cell_id:
            info = await self.client.get_cell_info(cell_id)
            return cell_id, info
        return None, None
