"""
CellGuide API Client - Async HTTP client for fetching CellGuide data.

The CellGuide data is served as static JSON files from CloudFront:
- Base URL: https://cellguide.cellxgene.cziscience.com
- Data is versioned by snapshot identifiers
- Cell type IDs use underscores in URLs (CL_0000622) but colons in JSON (CL:0000622)
"""

import asyncio
import logging
import threading
from urllib.parse import quote
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://cellguide.cellxgene.cziscience.com"
HUMAN_TAXONOMY = "NCBITaxon_9606"


def to_url_format(cell_id: str) -> str:
    """
    Convert cell type ID to URL format (underscores).

    Examples:
        CL:0000622 -> CL_0000622
        CL_0000622 -> CL_0000622
        0000622 -> CL_0000622
    """

    if "_" in cell_id and cell_id.startswith("CL_"):
        return cell_id


    if ":" in cell_id:
        return cell_id.replace(":", "_")


    if cell_id.isdigit():
        return f"CL_{cell_id}"

    return cell_id


def to_json_format(cell_id: str) -> str:
    """
    Convert cell type ID to JSON format (colons).

    Examples:
        CL_0000622 -> CL:0000622
        CL:0000622 -> CL:0000622
        0000622 -> CL:0000622
    """

    if ":" in cell_id and cell_id.startswith("CL:"):
        return cell_id


    if "_" in cell_id:
        return cell_id.replace("_", ":")


    if cell_id.isdigit():
        return f"CL:{cell_id}"

    return cell_id


class CellGuideClient:
    """
    Async HTTP client for fetching CellGuide data.

    Caches:
    - Snapshot identifier (fetched once per instance)
    - Cell type metadata (large file, rarely changes)
    - Tissue metadata (small file, rarely changes)
    - Cell type to tissue mapping (rarely changes)
    """

    def __init__(self, timeout: float = 30.0):
        """
        Initialize the CellGuide client.

        Args:
            timeout: HTTP request timeout in seconds
        """
        self.timeout = timeout
        self._http_client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None


        self._client_rebind_lock = threading.Lock()


        self._snapshot_id: str | None = None
        self._celltype_metadata: dict[str, Any] | None = None
        self._tissue_metadata: dict[str, Any] | None = None
        self._tissue_mapping: dict[str, list[str]] | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client, bound to the RUNNING loop.

        The bio aggregate dispatches each tool call on a fresh worker event
        loop (mcp_bio/server.py worker dispatch), so a client cached across
        calls holds a connection pool bound to a closed loop — the 2nd call
        raises "Event loop is closed". Re-create
        when the running loop changes; standalone single-loop serving keeps
        full connection reuse. The superseded client is dropped un-awaited
        (its loop is gone; transports are already dead).

        Concurrency: the instance is a module-level
        singleton and worker loops run on distinct threads, so the
        check-create-return must be atomic and must return the client chosen
        UNDER the lock — re-reading the shared attribute after unlocking can
        hand back a sibling thread's client bound to a different loop.
        """
        loop = asyncio.get_running_loop()
        stale: httpx.AsyncClient | None = None
        with self._client_rebind_lock:
            if self._http_client is None or self._client_loop is not loop:
                stale = self._http_client
                self._http_client = httpx.AsyncClient(timeout=self.timeout)
                self._client_loop = loop
            client = self._http_client
        if stale is not None:





            try:
                await stale.aclose()
            except Exception:
                pass
        return client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def get_snapshot_id(self) -> str:
        """
        Get the latest snapshot identifier.

        Returns:
            Snapshot ID string (e.g., "1763135102")
        """
        if self._snapshot_id is not None:
            return self._snapshot_id

        client = await self._get_client()
        url = f"{BASE_URL}/latest_snapshot_identifier"

        response = await client.get(url)
        response.raise_for_status()

        self._snapshot_id = response.text.strip()
        logger.info(f"Fetched CellGuide snapshot ID: {self._snapshot_id}")
        return self._snapshot_id

    async def get_celltype_metadata(self) -> dict[str, Any]:
        """
        Get all cell type metadata.

        Returns:
            Dict mapping cell type IDs (colon format) to metadata:
            {
                "CL:0000622": {
                    "name": "acinar cell",
                    "id": "CL:0000622",
                    "clDescription": "A secretory cell...",
                    "synonyms": ["acinic cell", "acinous cell"]
                },
                ...
            }
        """
        if self._celltype_metadata is not None:
            return self._celltype_metadata

        snapshot = await self.get_snapshot_id()
        client = await self._get_client()
        url = f"{BASE_URL}/{snapshot}/celltype_metadata.json"

        response = await client.get(url)
        response.raise_for_status()

        self._celltype_metadata = response.json()
        logger.info(f"Fetched {len(self._celltype_metadata)} cell types")
        return self._celltype_metadata

    async def get_tissue_metadata(self) -> dict[str, Any]:
        """
        Get all tissue metadata.

        Returns:
            Dict mapping tissue IDs (colon format) to metadata
        """
        if self._tissue_metadata is not None:
            return self._tissue_metadata

        snapshot = await self.get_snapshot_id()
        client = await self._get_client()
        url = f"{BASE_URL}/{snapshot}/tissue_metadata.json"

        response = await client.get(url)
        response.raise_for_status()

        self._tissue_metadata = response.json()
        logger.info(f"Fetched {len(self._tissue_metadata)} tissues")
        return self._tissue_metadata

    async def get_tissue_mapping(self) -> dict[str, list[str]]:
        """
        Get cell type to tissue mapping.

        Returns:
            Dict mapping cell type IDs to lists of tissue IDs
        """
        if self._tissue_mapping is not None:
            return self._tissue_mapping

        snapshot = await self.get_snapshot_id()
        client = await self._get_client()
        url = f"{BASE_URL}/{snapshot}/ontology_tree/{HUMAN_TAXONOMY}/celltype_to_tissue_mapping.json"

        response = await client.get(url)
        response.raise_for_status()

        self._tissue_mapping = response.json()
        logger.info(f"Fetched tissue mapping for {len(self._tissue_mapping)} cell types")
        return self._tissue_mapping

    async def get_cell_info(self, cell_id: str) -> dict[str, Any] | None:
        """
        Get basic info for a specific cell type.

        Args:
            cell_id: Cell type ID (any format)

        Returns:
            Cell type metadata dict or None if not found
        """
        metadata = await self.get_celltype_metadata()
        json_id = to_json_format(cell_id)
        return metadata.get(json_id)

    async def get_description(self, cell_id: str) -> dict[str, Any]:
        """
        Get description for a cell type (validated first, fallback to GPT).

        Args:
            cell_id: Cell type ID (any format)

        Returns:
            Dict with "description" and optionally "references" and "source"
        """
        url_id = to_url_format(cell_id)
        client = await self._get_client()


        validated_url = f"{BASE_URL}/validated_descriptions/{quote(url_id, safe='')}.json"
        try:
            response = await client.get(validated_url)
            if response.status_code == 200:
                data = response.json()
                return {
                    "description": data.get("description", ""),
                    "references": data.get("references", []),
                    "source": "validated",
                }
        except Exception as e:
            logger.debug(f"No validated description for {cell_id}: {e}")


        gpt_url = f"{BASE_URL}/gpt_descriptions/{quote(url_id, safe='')}.json"
        try:
            response = await client.get(gpt_url)
            if response.status_code == 200:

                text = response.json()
                if isinstance(text, str):
                    return {"description": text, "references": [], "source": "gpt"}
        except Exception as e:
            logger.debug(f"No GPT description for {cell_id}: {e}")

        return {"description": "", "references": [], "source": "none"}

    async def get_marker_genes(
        self, cell_id: str, marker_type: str = "computational", limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        Get marker genes for a cell type.

        Args:
            cell_id: Cell type ID (any format)
            marker_type: "computational" or "canonical"
            limit: Max number of genes to return

        Returns:
            List of marker gene dicts with symbol, name, scores, etc.
        """
        url_id = to_url_format(cell_id)
        snapshot = await self.get_snapshot_id()
        client = await self._get_client()

        if marker_type == "canonical":
            url = f"{BASE_URL}/{snapshot}/canonical_marker_genes/{quote(url_id, safe='')}.json"
        else:
            url = f"{BASE_URL}/{snapshot}/computational_marker_genes/{quote(url_id, safe='')}.json"

        try:
            response = await client.get(url)
            if response.status_code == 200 and response.text.strip():
                genes = response.json()
                if isinstance(genes, list):

                    genes.sort(key=lambda g: g.get("marker_score", 0), reverse=True)
                    return genes[:limit]
            return []
        except Exception as e:
            logger.warning(f"Failed to get {marker_type} marker genes for {cell_id}: {e}")
            return []

    async def get_source_collections(self, cell_id: str) -> list[dict[str, Any]]:
        """
        Get source data collections for a cell type.

        Args:
            cell_id: Cell type ID (any format)

        Returns:
            List of collection dicts with name, URL, publication info, etc.
        """
        url_id = to_url_format(cell_id)
        snapshot = await self.get_snapshot_id()
        client = await self._get_client()

        url = f"{BASE_URL}/{snapshot}/source_collections/{quote(url_id, safe='')}.json"

        try:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            return []
        except Exception as e:
            logger.warning(f"Failed to get source collections for {cell_id}: {e}")
            return []

    async def get_cell_tissues(self, cell_id: str) -> list[dict[str, Any]]:
        """
        Get tissues where a cell type is found.

        Args:
            cell_id: Cell type ID (any format)

        Returns:
            List of tissue dicts with id and name
        """
        json_id = to_json_format(cell_id)


        mapping = await self.get_tissue_mapping()
        tissue_metadata = await self.get_tissue_metadata()

        tissue_ids = mapping.get(json_id, [])

        tissues = []
        for tid in tissue_ids:
            tissue_info = tissue_metadata.get(tid)
            if tissue_info:
                tissues.append(
                    {
                        "id": tid,
                        "name": tissue_info.get("name", ""),
                        "description": tissue_info.get("uberonDescription", ""),
                    }
                )
            else:
                tissues.append({"id": tid, "name": tid, "description": ""})

        return tissues

    async def search_cell_types(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Search for cell types by name or synonym.

        Args:
            query: Search query (case-insensitive)
            limit: Max results to return

        Returns:
            List of matching cell type metadata dicts
        """
        metadata = await self.get_celltype_metadata()
        query_lower = query.lower().strip()

        matches = []
        for _cell_id, info in metadata.items():
            name = info.get("name", "").lower()
            synonyms = [s.lower() for s in info.get("synonyms", [])]


            if query_lower in name:
                score = 100 if name == query_lower else (90 if name.startswith(query_lower) else 50)
                matches.append((score, info))
                continue


            for syn in synonyms:
                if query_lower in syn:
                    score = 80 if syn == query_lower else (70 if syn.startswith(query_lower) else 40)
                    matches.append((score, info))
                    break


        matches.sort(key=lambda x: (-x[0], x[1].get("name", "")))

        return [info for _, info in matches[:limit]]
