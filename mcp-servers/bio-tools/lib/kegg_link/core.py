"""Core operations: batched /link and /conv, /find helpers, tidy mapping records.

KEGG REST accepts up to 10 entries per /link and /conv request, joined with "+"
(https://www.kegg.jp/kegg/rest/keggapi.html).  This module batches input ID lists
accordingly, parses the flat two-column responses, and emits tidy records that keep
provenance: which operation produced the row, which databases were involved, which
request (path + batch index) returned it, and the input ID it belongs to.

Determinism: output records are sorted by (input ID order as given, target_id), and
inputs with zero hits are reported explicitly in ``LinkResult.missing_ids`` instead of
silently disappearing as they do in the raw flat text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from .client import KeggClient

LINK_CONV_BATCH_SIZE = 10





COLUMNS = [
    "source_id",
    "source_db",
    "target_id",
    "target_db",
    "operation",
    "batch_index",
    "request_index",
]






def split_prefix(kegg_id: str) -> tuple[str, str]:
    """Split a prefixed KEGG identifier into (database, local id).

    ``hsa:7157`` -> ("hsa", "7157"); ``cpd:C00031`` -> ("cpd", "C00031");
    an un-prefixed id returns ("", id).
    """
    if ":" in kegg_id:
        db, local = kegg_id.split(":", 1)
        return db, local
    return "", kegg_id


def parse_two_column(text: str) -> list[tuple[str, str]]:
    """Parse a /link or /conv flat-text response into (left, right) tuples.

    Blank lines and a possible trailing newline are ignored.  Both /link and /conv
    return the *query* entry in the first column and the linked/converted entry in
    the second column.
    """
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            raise ValueError(f"unexpected /link or /conv line (expected 2 columns): {line!r}")
        pairs.append((parts[0], parts[1]))
    return pairs


def parse_find(text: str) -> list[dict]:
    """Parse a /find response into [{entry_id, description}] records."""
    records: list[dict] = []
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        entry_id = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else ""
        records.append({"entry_id": entry_id, "description": description})
    return records


def chunk_ids(ids: list[str], size: int = LINK_CONV_BATCH_SIZE) -> list[list[str]]:
    """Split *ids* into consecutive batches of at most *size* (order preserved)."""
    if size < 1:
        raise ValueError("batch size must be >= 1")
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _normalise_query_id(returned_id: str, query_ids: list[str]) -> str:
    """Map an ID as returned in column 1 back to the exact input spelling.

    KEGG echoes the query entry with its database prefix; for organism gene IDs and
    prefixed inputs this matches the input verbatim.  If an input was given without a
    prefix (e.g. ``C00031`` instead of ``cpd:C00031``), match on the local part.
    """
    if returned_id in query_ids:
        return returned_id
    _, local = split_prefix(returned_id)
    for q in query_ids:
        if q == local or split_prefix(q)[1] == local:
            return q
    return returned_id






@dataclass
class LinkResult:
    """Tidy mapping table plus provenance for one batched /link or /conv operation."""

    operation: str
    target_db: str
    query_ids: list[str]
    records: list[dict] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)
    request_paths: list[str] = field(default_factory=list)

    def per_id_targets(self) -> dict[str, list[str]]:
        """Return {input_id: sorted list of target_ids} (empty list for missing IDs)."""
        out: dict[str, list[str]] = {qid: [] for qid in self.query_ids}
        for rec in self.records:
            out.setdefault(rec["source_id"], []).append(rec["target_id"])
        return {k: sorted(v) for k, v in out.items()}

    def to_tsv(self, header: bool = True, include_request_manifest: bool = True) -> str:
        """Serialize to TSV. The request manifest (one ``# request[i]\t<path>`` comment
        line per batched request) is emitted once at the top so every row's
        ``request_index`` resolves to a full URL path without repeating it per row."""
        prefix = ""
        if include_request_manifest:
            prefix = "".join(
                f"# request[{i}]\t{path}\n" for i, path in enumerate(self.request_paths)
            )
        return prefix + records_to_tsv(self.records, header=header)


def records_to_tsv(records: list[dict], header: bool = True) -> str:
    """Serialize tidy records to a TSV string with the canonical column order."""
    lines: list[str] = []
    if header:
        lines.append("\t".join(COLUMNS))
    for rec in records:
        lines.append("\t".join(str(rec[c]) for c in COLUMNS))
    return "\n".join(lines) + ("\n" if lines else "")


def canonicalize_records(records: list[dict]) -> list[tuple]:
    """Canonical form for run-to-run comparison: drop request provenance that could
    legitimately vary if batch size were changed (it does not vary between identical
    runs, but the gate compares scientific content), keep all scientific content,
    sort stably."""
    keyed = [
        (rec["source_id"], rec["source_db"], rec["target_id"], rec["target_db"], rec["operation"])
        for rec in records
    ]
    return sorted(keyed)






def _run_two_column_op(
    client: KeggClient,
    op: str,
    target_db: str,
    ids: list[str],
    batch_size: int = LINK_CONV_BATCH_SIZE,
) -> LinkResult:
    if op not in ("link", "conv"):
        raise ValueError(f"unsupported operation: {op!r}")
    if not ids:
        raise ValueError("ids must be a non-empty list")

    seen: set[str] = set()
    ordered_ids = [i for i in ids if not (i in seen or seen.add(i))]

    result = LinkResult(operation=op, target_db=target_db, query_ids=ordered_ids)
    order_index = {qid: i for i, qid in enumerate(ordered_ids)}

    raw_records: list[dict] = []
    for batch_index, batch in enumerate(chunk_ids(ordered_ids, batch_size)):
        path = (f"/{op}/{quote(str(target_db), safe='')}/"
                + "+".join(quote(str(i), safe=":") for i in batch))
        text = client.get_text(path)
        result.request_paths.append(path)
        for left, right in parse_two_column(text):
            source_id = _normalise_query_id(left, batch)
            source_db, _ = split_prefix(source_id)
            tdb, _ = split_prefix(right)
            raw_records.append(
                {
                    "source_id": source_id,
                    "source_db": source_db,
                    "target_id": right,
                    "target_db": tdb or target_db,
                    "operation": op,
                    "batch_index": batch_index,
                    "request_index": batch_index,
                }
            )


    raw_records.sort(key=lambda r: (order_index.get(r["source_id"], len(order_index)), r["target_id"]))
    result.records = raw_records

    hit_ids = {r["source_id"] for r in raw_records}
    result.missing_ids = [qid for qid in ordered_ids if qid not in hit_ids]
    return result


def link(
    client: KeggClient,
    ids: list[str],
    target_db: str,
    batch_size: int = LINK_CONV_BATCH_SIZE,
) -> LinkResult:
    """Cross-reference *ids* against *target_db* via batched /link requests.

    Example: ``link(client, ["hsa:7157", "hsa:672"], "pathway")``.
    """
    return _run_two_column_op(client, "link", target_db, ids, batch_size)


def conv(
    client: KeggClient,
    ids: list[str],
    target_db: str,
    batch_size: int = LINK_CONV_BATCH_SIZE,
) -> LinkResult:
    """Convert *ids* to/from outside identifiers via batched /conv requests.

    Examples: ``conv(client, ["ncbi-geneid:7157"], "hsa")`` (NCBI -> KEGG organism
    gene) or ``conv(client, ["hsa:7157"], "ncbi-geneid")`` (KEGG -> NCBI).
    """
    return _run_two_column_op(client, "conv", target_db, ids, batch_size)


def find(client: KeggClient, database: str, query: str) -> list[dict]:
    """Keyword search helper: /find/<database>/<query> -> [{entry_id, description}].

    Useful for resolving names (e.g. compound names) to KEGG IDs before /link.
    The query is URL-path safe-encoded by requests; spaces are allowed.
    """
    path = f"/find/{quote(str(database), safe='')}/{quote(query, safe='')}"
    text = client.get_text(path)
    return parse_find(text)






def parse_gene_symbols(description: str) -> list[str]:
    """Symbol list from a /find/<org> gene row description.

    KEGG gene descriptions are ``SYM1, SYM2, ...; <name>``; rows without a
    ``;`` (rare, unnamed entries) are treated as having no symbol list.
    """
    head = description.split(";", 1)[0] if ";" in description else ""
    return [s.strip() for s in head.split(",") if s.strip()]


def filter_exact_symbol(records: list[dict], symbol: str) -> list[dict]:
    """Keep only /find rows whose symbol list contains *symbol* exactly
    (case-insensitive).

    KEGG's /find matches the query as a SUBSTRING anywhere in the entry line
    — ``/find/hsa/TP53`` returns TP53BP2, TP53I3, even MAD1L1 (alias TP53I9)
    before TP53 itself — so exact-symbol resolution REQUIRES this client-side
    filter; trusting the raw hit order returns the wrong gene.
    """
    want = symbol.strip().lower()
    out = []
    for rec in records:
        symbols = parse_gene_symbols(rec.get("description", ""))
        if any(s.lower() == want for s in symbols):
            out.append({**rec, "symbols": symbols})
    return out


def gene_ids_by_symbol(
    client: KeggClient,
    symbol: str,
    organism: str = "hsa",
) -> dict:
    """Resolve a gene symbol to KEGG gene ID(s) via /find/<organism>/<symbol>.

    Returns ``{symbol, organism, n_matches, matches}`` where each match is
    ``{entry_id, symbols, description}`` and ``matches`` is sorted by entry_id.
    Exact-match semantics over the row's symbol list (see
    :func:`filter_exact_symbol`); zero matches is a normal outcome
    (``n_matches == 0``), not an error. Symbols that are aliases of several
    genes return ALL of them — the caller decides, nothing is silently picked.
    """
    if not symbol or not symbol.strip():
        raise ValueError("symbol must be non-empty")
    hits = find(client, organism, symbol.strip())
    matches = sorted(filter_exact_symbol(hits, symbol),
                     key=lambda r: r["entry_id"])
    return {
        "symbol": symbol.strip(),
        "organism": organism,
        "n_matches": len(matches),
        "matches": [
            {"entry_id": m["entry_id"], "symbols": m["symbols"],
             "description": m["description"]}
            for m in matches
        ],
    }
