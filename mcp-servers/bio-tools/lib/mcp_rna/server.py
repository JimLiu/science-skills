"""FastMCP server exposing the rfam-families fleet tool.

All retrieval (pacing, retries, content negotiation, deterministic sorting)
lives in the ``rfam-families`` package; this layer is marshalling only.

Known upstream limitations (surfaced honestly, never wrapped or retried):

* ``search_sequence``: Rfam's async cmscan job backend was DOWN at fleet
  build time (2026-06-08) — valid submissions got an HTML 500 "Please come
  back later" while input validation still worked. The fleet raises
  ``SearchUnavailable`` in that case; this server passes the error through.
* ``get_sequence_regions``: upstream 403s this route for very large
  families (e.g. RF00005 with >5M regions) — the error is passed through.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)
from rfam_families import RfamFamilies

mcp = FastMCP("mcp-rna")

_tool: RfamFamilies | None = None


def _rfam() -> RfamFamilies:
    global _tool
    if _tool is None:
        _tool = RfamFamilies()
    return _tool


def _cap_text_payload(result: dict, field: str, max_bytes: int) -> dict:
    """Omit a huge text field instead of blowing the MCP transport limit.

    Metadata/sha256 always survive; the caller can re-request with a larger
    max_bytes or fetch from rfam.org directly using the returned accession.
    """
    text = result.get(field)
    if isinstance(text, str) and len(text.encode()) > max_bytes:
        size = len(text.encode())
        result = dict(result)
        del result[field]
        result[f"{field}_omitted"] = (
            f"{field} is {size} bytes > max_bytes={max_bytes}; metadata and "
            f"sha256 are included — re-call with a larger max_bytes to get "
            f"the full text"
        )
        result.setdefault("size_bytes", size)
    return result


@mcp.tool(annotations=READ_ONLY)
def get_family(family: str) -> dict:
    """Rfam family metadata record.

    Args:
        family: Rfam accession ("RF00005") or family id ("tRNA") — both
            resolve.

    Returns a flattened record — rfam_acc, rfam_id, description, RNA type,
    seed/full sequence counts (num_seed, num_full), gathering/trusted/noise
    cutoffs, clan, curation info — plus the complete upstream JSON in "raw".
    """
    return _rfam().get_family(family)


@mcp.tool(annotations=READ_ONLY)
def get_seed_alignment(family: str, fmt: str = "stockholm", max_bytes: int = 400_000) -> dict:
    """Seed alignment of an Rfam family.

    Args:
        family: Rfam accession or family id.
        fmt: "stockholm" (default; includes the consensus secondary
            structure line) or "fasta" (aligned, gapped FASTA).

    Returns {"family", "format", "num_sequences", "sequence_names",
    "sha256", "alignment"} — the sha256 lets you verify integrity without
    re-downloading. Large families (e.g. RF00005/tRNA) have multi-MB seed
    alignments that exceed MCP transport limits: when the text is larger
    than max_bytes (default 400000) the "alignment" field is omitted and
    "alignment_omitted"/"size_bytes" explain why — metadata, counts and
    sha256 are always returned. Raise max_bytes explicitly if your client
    can take the payload.
    """
    result = _rfam().get_alignment(family, fmt=fmt)
    return _cap_text_payload(result, "alignment", max_bytes)


@mcp.tool(annotations=READ_ONLY)
def get_covariance_model(family: str, max_bytes: int = 400_000) -> dict:
    """Infernal covariance model (CM file) of an Rfam family.

    Returns {"family", "header" (parsed fields: NAME, ACC, STATES, CLEN,
    W, ...), "size_bytes", "sha256", "cm" (the full CM text, usable
    directly with Infernal cmsearch/cmscan)}. CMs of large families exceed
    MCP transport limits: when the text is larger than max_bytes (default
    400000) the "cm" field is omitted and "cm_omitted"/"size_bytes" explain
    why — header, size and sha256 are always returned. Raise max_bytes
    explicitly if your client can take the payload.
    """
    result = _rfam().get_cm(family)
    return _cap_text_payload(result, "cm", max_bytes)


@mcp.tool(annotations=READ_ONLY)
def get_tree(family: str) -> dict:
    """Seed phylogenetic tree of an Rfam family (NHX/Newick text).

    Returns {"family", "num_leaf_labels", "sha256", "tree"}.
    """
    return _rfam().get_tree(family)


@mcp.tool(annotations=READ_ONLY)
def get_sequence_regions(family: str) -> dict:
    """All full-region hits of an Rfam family across sequence databases.

    Returns {"family", "declared_count" (the server's own count line),
    "num_regions" (parsed rows), "regions": [{accession, start, end,
    description, ...}, ...]}.

    Known upstream limitation: rfam.org returns HTTP 403 on this route for
    very large families (e.g. RF00005/tRNA with ~5.3M regions); that error
    is surfaced as-is. Check num_full via get_family first.
    """
    return _rfam().sequence_regions(family)


@mcp.tool(annotations=READ_ONLY)
def get_structure_mapping(family: str) -> dict:
    """PDB residue-level structure mappings of an Rfam family.

    Returns {"family", "num_mappings", "num_pdb_ids", "pdb_ids",
    "mapping": [{pdb_id, chain, pdb_start, pdb_end, seq_start, seq_end,
    cm_start, cm_end, ...}, ...]} sorted deterministically.
    """
    return _rfam().structure_mapping(family)


@mcp.tool(annotations=READ_ONLY)
def accession_to_id(accession: str) -> dict:
    """Convert an Rfam accession to its family id (e.g. RF00005 -> "tRNA")."""
    return {"accession": accession, "rfam_id": _rfam().acc_to_id(accession)}


@mcp.tool(annotations=READ_ONLY)
def id_to_accession(family_id: str) -> dict:
    """Convert an Rfam family id to its accession (e.g. "tRNA" -> RF00005)."""
    return {"rfam_id": family_id, "accession": _rfam().id_to_acc(family_id)}


@mcp.tool(annotations=READ_ONLY)
def search_sequence(
    sequence: str,
    max_wait_s: float = 300.0,
    poll_interval_s: float = 5.0,
) -> dict:
    """Search a single RNA sequence against all Rfam covariance models (cmscan).

    Submits an asynchronous job to rfam.org and polls until done.

    Args:
        sequence: RNA/DNA sequence (plain string, no FASTA header);
            practical upstream limit ~10 kb.
        max_wait_s: give up (TimeoutError) after this many seconds.
        poll_interval_s: seconds between result polls.

    Returns {"job_id", "num_hits", "families", "hits": {family_id:
    [hit records with e-values, scores, alignment blocks], ...},
    "search_sequence"}.

    KNOWN LIMITATION: the upstream job backend was down at build time
    (2026-06-08) — valid submissions fail with "SearchUnavailable ... Please
    come back later" while invalid sequences still get a proper 400. The
    error is surfaced as-is; there is no local fallback.
    """
    return _rfam().search_sequence(sequence, max_wait_s=max_wait_s,
                                   poll_interval_s=poll_interval_s)


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
