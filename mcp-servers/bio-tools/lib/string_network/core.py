"""Core logic for string-network.

All parsing / ordering / summarisation functions are pure (no HTTP) so they can
be unit-tested offline; ``build_network`` wires them to a :class:`StringClient`.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
from typing import Any, Iterable

from .client import StringClient

TOOL_NAME = "string-network"
TOOL_VERSION = "0.3.0"


EVIDENCE_CHANNELS = ("nscore", "fscore", "pscore", "ascore", "escore", "dscore", "tscore")


NETWORK_TSV_COLUMNS = (
    "stringId_A",
    "stringId_B",
    "preferredName_A",
    "preferredName_B",
    "ncbiTaxonId",
    "score",
) + EVIDENCE_CHANNELS


VOLATILE_PROVENANCE_FIELDS = ("retrieved_at", "requests", "n_http_requests", "bytes_downloaded")






def parse_mapping_rows(
    symbols: list[str], rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split a get_string_ids response into (mapped, unmapped).

    ``rows`` is the parsed JSON from /api/json/get_string_ids called with
    ``echo_query=1`` and ``limit=1``; each row carries ``queryIndex`` (0-based
    position in the submitted identifier list). The returned ``mapped`` list is
    in input order; ``unmapped`` preserves input order for symbols with no hit.
    Together they partition the input exactly.
    """
    by_index: dict[int, dict[str, Any]] = {}
    for row in rows:
        idx = int(row["queryIndex"])

        by_index.setdefault(idx, row)

    mapped: list[dict[str, Any]] = []
    unmapped: list[str] = []
    for i, symbol in enumerate(symbols):
        row = by_index.get(i)
        if row is None:
            unmapped.append(symbol)
        else:
            mapped.append(
                {
                    "query": symbol,
                    "string_id": row["stringId"],
                    "preferred_name": row["preferredName"],
                    "ncbi_taxon_id": int(row["ncbiTaxonId"]),
                }
            )
    return mapped, unmapped


def map_identifiers(
    client: StringClient, symbols: list[str], species: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map gene symbols to STRING identifiers (get_string_ids, limit=1, echo_query=1)."""
    text = client.call(
        "json",
        "get_string_ids",
        {
            "identifiers": "\r".join(symbols),
            "species": species,
            "limit": 1,
            "echo_query": 1,
        },
    )
    rows = json.loads(text)
    return parse_mapping_rows(symbols, rows)






def parse_network_tsv(text: str) -> list[dict[str, str]]:
    """Parse the TSV body of /api/tsv/network into a list of row dicts."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    missing = [c for c in NETWORK_TSV_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"network TSV is missing expected columns: {missing}")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append(dict(zip(header, values)))
    return rows


def canonical_edges(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Convert raw network rows into deterministically ordered, de-duplicated, trimmed edges.

    Output edge schema (token-trimmed; STRING IDs live in the node table, not on edges):
      ``{"a": <preferred name>, "b": <preferred name>, "score": <combined>,
         "evidence": {<channel>: <subscore>, ... nonzero channels only}}``

    * endpoints within an edge are oriented so that
      (preferred_name_a, string_id_a) <= (preferred_name_b, string_id_b);
    * duplicate unordered pairs (by STRING ID) are collapsed, keeping the higher
      combined score;
    * edges are sorted by (name_a, name_b) with STRING IDs as tie-breakers;
    * scores are rounded to 3 decimals (STRING's own reported precision);
    * only non-zero evidence-channel subscores are kept under ``evidence``.
    """
    dedup: dict[frozenset[str], dict[str, Any]] = {}
    for row in rows:
        a = (row["preferredName_A"], row["stringId_A"])
        b = (row["preferredName_B"], row["stringId_B"])
        if b < a:
            a, b = b, a
        evidence = {}
        for channel in EVIDENCE_CHANNELS:
            value = round(float(row[channel]), 3)
            if value > 0:
                evidence[channel] = value
        edge = {
            "_sort_key": (a[0], b[0], a[1], b[1]),
            "a": a[0],
            "b": b[0],
            "score": round(float(row["score"]), 3),
            "evidence": evidence,
        }
        key = frozenset((a[1], b[1]))
        existing = dedup.get(key)
        if existing is None or edge["score"] > existing["score"]:
            dedup[key] = edge
    ordered = sorted(dedup.values(), key=lambda e: e["_sort_key"])
    for edge in ordered:
        edge.pop("_sort_key")
    return ordered


def fetch_network_edges(
    client: StringClient,
    string_ids: list[str],
    species: int,
    required_score: int,
) -> list[dict[str, Any]]:
    """Retrieve the interaction network for mapped STRING IDs and canonicalize edges."""
    text = client.call(
        "tsv",
        "network",
        {
            "identifiers": "\r".join(string_ids),
            "species": species,
            "required_score": required_score,
        },
    )
    return canonical_edges(parse_network_tsv(text))






def _degrees_by_name(
    mapped: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, int]:
    """Edge-count per preferred name (preferred names are unique per species in STRING)."""
    degree: dict[str, int] = {m["preferred_name"]: 0 for m in mapped}
    for edge in edges:
        for name in (edge["a"], edge["b"]):
            degree[name] = degree.get(name, 0) + 1
    return degree


def summarize(
    mapped: list[dict[str, Any]],
    unmapped: list[str],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Node/edge/score summary statistics for a retrieved network."""
    degree = _degrees_by_name(mapped, edges)
    scores = [e["score"] for e in edges]
    n_connected = sum(1 for d in degree.values() if d > 0)
    return {
        "n_input_symbols": len(mapped) + len(unmapped),
        "n_mapped": len(mapped),
        "n_unmapped": len(unmapped),
        "n_nodes": len(mapped),
        "n_connected_nodes": n_connected,
        "n_isolated_nodes": len(mapped) - n_connected,
        "n_edges": len(edges),
        "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
        "min_score": min(scores) if scores else None,
        "max_score": max(scores) if scores else None,
    }


def _node_table(
    mapped: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merged mapping + degree table: one row per mapped input symbol.

    This is the single place where STRING IDs appear in the output (edges carry
    preferred names only), and it doubles as the mapping-accountability record:
    every mapped input symbol has exactly one row here; unmapped symbols are
    listed separately under ``unmapped``.
    """
    degree = _degrees_by_name(mapped, edges)
    nodes = [
        {
            "query": m["query"],
            "name": m["preferred_name"],
            "string_id": m["string_id"],
            "degree": degree[m["preferred_name"]],
        }
        for m in mapped
    ]
    return sorted(nodes, key=lambda n: (n["name"], n["string_id"]))






def build_network(
    symbols: list[str],
    species: int = 9606,
    required_score: int = 700,
    client: StringClient | None = None,
    include_request_log: bool = True,
) -> dict[str, Any]:
    """Map gene symbols and retrieve their STRING interaction network.

    Returns a JSON-serialisable dict with mapping (incl. explicit unmapped
    list), deterministically ordered edges, node table, summary statistics and
    a provenance log.
    """
    if client is None:
        client = StringClient()
    symbols = [s.strip() for s in symbols if s and s.strip()]
    if not symbols:
        raise ValueError("no input symbols provided")
    if len(set(symbols)) != len(symbols):
        seen: set[str] = set()
        deduped = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        symbols = deduped

    log_start = len(client.request_log)
    version = client.get_version()
    mapped, unmapped = map_identifiers(client, symbols, species)
    string_ids = [m["string_id"] for m in mapped]
    edges = fetch_network_edges(client, string_ids, species, required_score) if string_ids else []

    requests_made = client.request_log[log_start:]
    provenance: dict[str, Any] = {
        "api_base_url": client.base_url,
        "caller_identity": client.caller_identity,
        "endpoints_used": ["json/version", "json/get_string_ids", "tsv/network"],
        "parameters": {
            "species": species,
            "required_score": required_score,
            "network_type": "functional",
            "get_string_ids.limit": 1,
            "get_string_ids.echo_query": 1,
            "network.add_nodes": 0,
        },
        "retrieved_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "n_http_requests": len(requests_made),
        "bytes_downloaded": sum(r.get("bytes", 0) for r in requests_made),
    }
    if include_request_log:
        provenance["requests"] = requests_made

    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "query": {
            "symbols": symbols,
            "species": species,
            "required_score": required_score,
        },
        "string_version": version,


        "nodes": _node_table(mapped, edges),
        "unmapped": unmapped,


        "edges": edges,
        "summary": summarize(mapped, unmapped, edges),
        "provenance": provenance,
    }






def canonicalize(result: dict[str, Any]) -> bytes:
    """Canonical byte representation of a result for run-to-run comparison.

    Drops only volatile operational metadata from ``provenance``
    (retrieved_at, the per-request log, request/byte counters); all scientific
    content (query, string_version, mapping, nodes, edges, summary) is kept.
    Keys are sorted and JSON is emitted compactly.
    """
    clean = copy.deepcopy(result)
    prov = clean.get("provenance", {})
    for field in VOLATILE_PROVENANCE_FIELDS:
        prov.pop(field, None)
    return json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
