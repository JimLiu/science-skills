"""mcp-zinc server — ZINC22 purchasable chemical space via CartBlanche22.

Tier-2 domain server (clean schemas; no hosted-connector twin). All
retrieval goes through ``mcp_zinc.client.ZincClient``, which encapsulates
the CartBlanche22 async contract (form-POST submit -> task uuid -> poll
``/search/result/<uuid>``) — tools are single synchronous calls and callers
never see the dance.

Caps are load-bearing: ZINC22 holds 230M+ purchasable compounds and a broad
similarity search can match enormous sets, so every tool bounds its response
(``max_results``/``count``, default 50, hard cap 500) and every list
response discloses ``total_available`` vs ``returned_count`` plus per-source
counts. Never stream an unbounded ZINC result into a tool response.

Similarity note: CartBlanche22 has ONE structure-search endpoint
(``smiles.txt``); the ``dist``/``adist`` Tanimoto-distance parameters span
exact match (0) through analog discovery (1-10). A separate
"similarity search" tool would submit the identical request, so analog
discovery is folded into ``zinc_search_by_smiles`` via those parameters
(documented in the tool description).
"""

from __future__ import annotations

import re
from functools import lru_cache

from mcp.server.fastmcp import FastMCP

from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations

from .client import (
    DEFAULT_OUTPUT_FIELDS,
    DEFAULT_TIMEOUT_S,
    FILES_BASE_URL,
    MAX_TIMEOUT_S,
    MIN_TIMEOUT_S,
    ZincClient,
    flatten_result,
)



READ_ONLY = ToolAnnotations(readOnlyHint=True)

mcp = FastMCP("mcp-zinc")

DEFAULT_MAX_RESULTS = 50
MAX_RESULTS_CAP = 500
MAX_IDS_PER_CALL = 100
MAX_IDS_3D = 50



KNOWN_SUBSETS = ("fragment", "lead-like", "drug-like", "lugs")

_TRANCHE_RE = re.compile(r"^H(\d{2})([PM])(\d{3})$")
_ZINC_ID_RE = re.compile(r"^ZINC[a-zA-Z]?\d+$", re.IGNORECASE)



_ID_DELIM_RE = re.compile(r"[,\s]")




@lru_cache(maxsize=1)
def _client() -> ZincClient:
    return ZincClient()


def _max_results(n: int) -> int:


    return max(1, min(MAX_RESULTS_CAP, int(n)))


def _timeout(seconds: float) -> float:
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, float(seconds)))


def _normalize_zinc_id(s: str) -> str:
    """Canonicalize a ZINC id for result-map lookup.

    Upstream returns zero-padded ``ZINC000000000012``; ``_ZINC_ID_RE`` accepts
    ``ZINC12`` (and lowercase), so without normalization a short-form input
    yields an authoritative-looking ``found: False``. Uppercases the prefix
    and zero-pads the trailing digit run to 12 digits; non-ZINC strings pass
    through unchanged (used to canonicalize upstream values too).
    """
    m = re.fullmatch(r"(ZINC[a-zA-Z]?)(\d+)", s, re.IGNORECASE)
    if not m:
        return s
    return f"{m.group(1).upper()}{int(m.group(2)):012d}"


def parse_tranche(tranche_name: str | None) -> tuple[dict, str] | None:
    """Decode a ZINC tranche code (``H##P###``/``H##M###``) into properties:
    heavy-atom count and the logP bin (P = positive, M = negative, value/100).
    The tranche encodes nothing else (no MW, no HBD).

    Returns ``(properties, validated_code)`` — fullmatch + the validated
    capture so callers interpolate ``m.group(0)`` into
    paths, never the raw upstream string. Non-string input returns None:
    the ``smiles.txt`` endpoint sends ``tranche`` as a dict, not a code
    string (the 06-25 probe crash — ``_tranche_properties`` handles that
    shape).
    """
    if not tranche_name or not isinstance(tranche_name, str):
        return None
    m = _TRANCHE_RE.fullmatch(tranche_name.strip())
    if not m:
        return None
    sign = 1 if m.group(2) == "P" else -1
    return ({"heavy_atoms": int(m.group(1)),
             "logp": sign * int(m.group(3)) / 100.0},
            m.group(0))


def _tranche_properties(rec: dict) -> dict | None:
    """Decode a record's tranche into ``{heavy_atoms, logp}`` across all
    three upstream shapes:

    - ``tranche_details``: pre-decoded dict (``smiles.txt`` endpoint) —
      used verbatim (heavy_atoms/logp; plus mwt when present).
    - ``tranche`` dict ``{h_num: "H13", p_num: "P130", ...}`` (``smiles.txt``)
      — reassembled into the code string and decoded.
    - ``tranche_name`` / ``tranche`` string (``substances.txt``,
      ``catitems.txt``) — decoded via ``parse_tranche``.
    """
    details = rec.get("tranche_details")
    if isinstance(details, dict) and "heavy_atoms" in details:
        props = {"heavy_atoms": details.get("heavy_atoms"),
                 "logp": details.get("logp")}
        if details.get("mwt") is not None:
            props["mwt"] = details["mwt"]
        return props
    tranche = rec.get("tranche_name") or rec.get("tranche")
    if isinstance(tranche, dict):
        tranche = f"{tranche.get('h_num', '')}{tranche.get('p_num', '')}"
    decoded = parse_tranche(tranche)
    return decoded[0] if decoded else None


def _annotate(records: list[dict]) -> list[dict]:
    """Additive enrichment: decode each record's tranche in place-shape
    (new ``tranche_properties`` key; upstream fields stay verbatim)."""
    out = []
    for rec in records:
        props = _tranche_properties(rec)
        out.append({**rec, "tranche_properties": props} if props else rec)
    return out


def _page(records: list[dict], counts: dict[str, int], cap: int,
          query: dict) -> dict:
    """The one bounded response shape: total-available vs returned, always."""
    total = len(records)
    page = _annotate(records[:cap])
    return {
        "query": query,
        "total_available": total,
        "returned_count": len(page),
        "truncated": total > len(page),
        "source_counts": counts,
        "records": page,
    }


def _require_ids(ids: list[str], bound: int, what: str,
                 pattern: re.Pattern[str] | None = None) -> list[str]:
    """Normalize and bound an id list.

    Entries are joined with commas into the upstream form field, so a single
    entry containing a delimiter would let one list element submit many ids
    and defeat ``bound`` — reject those with an actionable message. ZINC ids
    are additionally shape-validated; supplier codes are free-form so they
    get the delimiter check only.
    """
    cleaned = [str(i).strip() for i in ids if str(i).strip()]
    if not cleaned:
        raise ValueError(f"provide at least one {what}")
    if len(cleaned) > bound:
        raise ValueError(
            f"{len(cleaned)} {what}s exceeds the per-call bound of {bound} — "
            "split the lookup into smaller batches")
    for entry in cleaned:
        if _ID_DELIM_RE.search(entry):
            raise ValueError(
                f"{what} entry {entry!r} contains a comma or whitespace — "
                "pass each id as its own list element (the per-call bound "
                f"is {bound} and is enforced before the upstream join)")
        if pattern is not None and not pattern.match(entry):
            raise ValueError(
                f"{what} entry {entry!r} is not a valid {what} "
                f"(expected pattern {pattern.pattern})")
    return cleaned





@mcp.tool(annotations=READ_ONLY)
def zinc_search_by_id(zinc_ids: list[str],
                      max_results: int = DEFAULT_MAX_RESULTS,
                      timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Look up purchasable compounds in ZINC22/ZINC20 by ZINC identifier —
    answers "what is this compound and who sells it".

    Args:
        zinc_ids: one or more ZINC ids (e.g. ``ZINC000000000012``), max 100
            per call (one batched query — preferred over many single-id
            calls).
        max_results: response bound (default 50, hard cap 500).
        timeout_s: overall search budget in seconds (default 25, clamped to
            5-55 — under the 60s MCP transport ceiling). The async backend
            computes server-side; a timeout error names the task uuid so the
            search can be re-polled.

    Returns ``{query, total_available, returned_count, truncated,
    source_counts, records}``; each record carries ``zinc_id``, ``smiles``,
    ``catalogs`` (supplier list — the purchasability signal),
    ``tranche_name`` plus decoded ``tranche_properties`` (heavy atoms, logP
    bin), and ``source`` (``zinc22``/``zinc20``). Ids with no match simply
    have no record.
    """
    ids = _require_ids(zinc_ids, MAX_IDS_PER_CALL, "ZINC id", _ZINC_ID_RE)
    cap = _max_results(max_results)
    result = _client().search(
        "substances.txt",
        {"zinc_ids": ",".join(ids), "output_fields": DEFAULT_OUTPUT_FIELDS},
        timeout_s=_timeout(timeout_s))
    records, counts = flatten_result(result)
    return _page(records, counts, cap, {"zinc_ids": ids})


@mcp.tool(annotations=READ_ONLY)
def zinc_search_by_smiles(smiles: str, dist: int = 0, adist: int | None = None,
                          max_results: int = DEFAULT_MAX_RESULTS,
                          timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Search ZINC22's purchasable chemical space by structure — answers
    "what purchasable compounds look like this SMILES". This is BOTH the
    exact-match and the analog-discovery (similarity) tool: CartBlanche22
    exposes one structure-search endpoint whose ``dist`` parameter spans
    exact through diverse, so there is deliberately no separate
    similarity-search tool.

    Args:
        smiles: query SMILES string (sent verbatim as a form field — no URL
            encoding needed).
        dist: Tanimoto DISTANCE threshold 0-10 (a distance, not a percent
            similarity: 0 = exact structure match, 1-3 = close analogs,
            4-6 = moderate analogs, 7-10 = diverse chemical space; larger =
            looser = slower and many more hits).
        adist: anonymous-graph distance 0-10 (scaffold-shaped tolerance);
            defaults to ``dist`` like the reference workflows.
        max_results: response bound (default 50, hard cap 500). Broad
            searches can match enormous sets — raise ``dist`` gradually
            rather than starting loose.
        timeout_s: overall search budget in seconds (default 25, clamped to
            5-55 — under the 60s MCP transport ceiling); similarity searches
            are the slowest ZINC queries.

    Returns the standard bounded shape (``total_available`` vs
    ``returned_count``, per-source counts) with records as in
    ``zinc_search_by_id``.
    """
    if not smiles or not smiles.strip():
        raise ValueError("smiles must be a non-empty SMILES string")
    dist = int(dist)
    if not 0 <= dist <= 10:
        raise ValueError("dist must be 0-10 (Tanimoto distance; 0 = exact)")
    adist = dist if adist is None else int(adist)
    if not 0 <= adist <= 10:
        raise ValueError("adist must be 0-10 (anonymous-graph distance)")
    cap = _max_results(max_results)
    result = _client().search(
        "smiles.txt",
        {"smiles": smiles.strip(), "dist": dist, "adist": adist,
         "output_fields": DEFAULT_OUTPUT_FIELDS},
        timeout_s=_timeout(timeout_s))
    records, counts = flatten_result(result)
    return _page(records, counts, cap,
                 {"smiles": smiles.strip(), "dist": dist, "adist": adist})


@mcp.tool(annotations=READ_ONLY)
def zinc_search_by_supplier(supplier_codes: list[str],
                            max_results: int = DEFAULT_MAX_RESULTS,
                            timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Resolve vendor catalog numbers to ZINC compounds — answers "which
    ZINC substance is this supplier code, and what's its structure".

    Args:
        supplier_codes: one or more vendor catalog codes (e.g.
            ``MCULE-2311834287``), max 100 per call.
        max_results: response bound (default 50, hard cap 500).
        timeout_s: overall search budget in seconds (default 25, clamped to
            5-55 — under the 60s MCP transport ceiling).

    Returns the standard bounded shape; records additionally carry
    ``supplier_code`` alongside ``zinc_id``/``smiles``/``catalogs``.
    """
    codes = _require_ids(supplier_codes, MAX_IDS_PER_CALL, "supplier code")
    cap = _max_results(max_results)
    result = _client().search(
        "catitems.txt",
        {"supplier_codes": ",".join(codes),
         "output_fields": "zinc_id,smiles,supplier_code,catalogs,tranche_name"},
        timeout_s=_timeout(timeout_s))
    records, counts = flatten_result(result)
    return _page(records, counts, cap, {"supplier_codes": codes})


@mcp.tool(annotations=READ_ONLY)
def zinc_random_sample(count: int = DEFAULT_MAX_RESULTS,
                       subset: str | None = None,
                       timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Draw a random sample of purchasable compounds from ZINC22 — for
    building screening decks, property baselines, or decoy sets.

    Args:
        count: sample size; doubles as this tool's ``max_results`` (default
            50, hard cap 500 — larger decks should be drawn in several
            calls, not one unbounded pull).
        subset: optional predefined property filter — ``fragment``
            (MW < 250), ``lead-like`` (MW 250-350, logP <= 3.5),
            ``drug-like`` (MW 350-500, Lipinski), ``lugs`` (curated);
            other upstream subset names pass through verbatim.
        timeout_s: overall search budget in seconds (default 25, clamped to
            5-55 — under the 60s MCP transport ceiling).

    Returns the standard bounded shape with records as in
    ``zinc_search_by_id`` (random order; re-calling draws a fresh sample).
    """
    cap = _max_results(count)
    data: dict = {"count": cap, "output_fields": DEFAULT_OUTPUT_FIELDS}
    if subset:
        data["subset"] = subset
    result = _client().search("substance/random.txt", data,
                              timeout_s=_timeout(timeout_s))
    records, counts = flatten_result(result)
    return _page(records, counts, cap,
                 {"count": cap, "subset": subset,
                  "known_subsets": list(KNOWN_SUBSETS)})


@mcp.tool(annotations=READ_ONLY)
def zinc_get_3d(zinc_ids: list[str],
                timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Locate docking-ready 3D structures for ZINC compounds. ZINC22 ships
    pre-generated 3D conformers (DOCK ``.db2.gz``, ``.mol2.gz``, ``.sdf.gz``)
    in its file repository, organized by tranche — this tool resolves each
    id to its tranche and returns the repository locations to download from
    for docking prep (DOCK6, AutoDock Vina, etc.).

    Args:
        zinc_ids: ZINC ids to prepare (max 50 per call — 3D retrieval is
            per-compound work; keep batches small).
        timeout_s: overall lookup budget in seconds (default 25, clamped to
            5-55 — under the 60s MCP transport ceiling).

    Returns ``{query, returned_count, structures, repository_note}``;
    each structure entry carries ``zinc_id``, ``found``, ``smiles``,
    ``source``, ``tranche_name`` + ``tranche_properties``, and ``download``:
    the tranche's repository directory pattern under
    ``https://files.docking.org/zinc22/`` plus the available 3D formats.
    Sub-release directories (``zinc-22a``, ``zinc-22b``, …) must be browsed
    for exact file names — the repository does not expose a per-compound
    fetch URL.
    """
    ids = _require_ids(zinc_ids, MAX_IDS_3D, "ZINC id", _ZINC_ID_RE)
    canonical = [_normalize_zinc_id(i) for i in ids]
    result = _client().search(
        "substances.txt",
        {"zinc_ids": ",".join(canonical),
         "output_fields": DEFAULT_OUTPUT_FIELDS},
        timeout_s=_timeout(timeout_s))
    records, _counts = flatten_result(result)




    by_id: dict[str, dict] = {}
    for rec in records:
        rid = rec.get("zinc_id")
        if rid:
            by_id.setdefault(_normalize_zinc_id(str(rid)), rec)

    structures = []
    for zid, czid in zip(ids, canonical):
        rec = by_id.get(czid)
        if rec is None:
            structures.append({"zinc_id": zid, "found": False})
            continue
        decoded = parse_tranche(rec.get("tranche_name") or rec.get("tranche"))
        props, tranche = decoded if decoded else (None, None)
        entry: dict = {
            "zinc_id": rec.get("zinc_id") or czid,
            "found": True,
            "smiles": rec.get("smiles"),
            "source": rec.get("source"),
            "tranche_name": tranche,
            "tranche_properties": props,
        }
        if tranche and props:
            heavy_dir = f"H{props['heavy_atoms']:02d}"
            entry["download"] = {
                "repository": f"{FILES_BASE_URL}/",
                "tranche_path_pattern":
                    f"zinc-22*/{heavy_dir}/{tranche}/",
                "formats": {
                    "db2.gz": "DOCK 3.x/6 multi-conformer database",
                    "mol2.gz": "Tripos MOL2 with 3D coordinates",
                    "sdf.gz": "SDF with 3D coordinates",
                    "smi": "SMILES (no 3D; for bookkeeping)",
                },
            }
        structures.append(entry)
    return {
        "query": {"zinc_ids": ids},
        "returned_count": len(structures),
        "structures": structures,
        "repository_note": (
            "Browse the sub-release directories (zinc-22a, zinc-22b, …) "
            "under the tranche path for exact file names before bulk "
            "download; convert with OpenBabel / prepare_ligand4.py for "
            "your docking engine."),
    }


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
