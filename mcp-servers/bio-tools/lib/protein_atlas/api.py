"""Public API: get_gene (per-gene record/summary) and search (bulk download).

Symbol resolution: HPA's per-gene route is keyed by Ensembl gene ID. A
symbol is resolved through search_download (columns g,gs,eg) by exact
case-insensitive match on the Gene field, falling back to the Gene synonym
list; resolution must be unique or AmbiguousSymbolError is raised with the
candidates.
"""
from __future__ import annotations

import re

from .client import HpaClient
from .records import summarize

ENSG_RE = re.compile(r"^ENSG\d{11}$")



DEFAULT_SEARCH_COLUMNS = "g,gs,eg,gd,up,chr,chrp,scl"
RESOLVE_COLUMNS = "g,gs,eg"


class SymbolNotFoundError(KeyError):
    def __init__(self, symbol: str):
        super().__init__(f"no HPA gene with symbol or synonym {symbol!r}")
        self.symbol = symbol


class AmbiguousSymbolError(KeyError):
    def __init__(self, symbol: str, candidates: list[tuple[str, str]]):
        super().__init__(
            f"symbol {symbol!r} matches multiple genes: {candidates}")
        self.symbol = symbol
        self.candidates = candidates


def is_ensg(s: str) -> bool:
    return bool(ENSG_RE.match(s))


class ProteinAtlas:
    def __init__(self, client: HpaClient | None = None):
        self.client = client or HpaClient()


    def search(self, query: str, columns: str = DEFAULT_SEARCH_COLUMNS) -> list[dict]:
        """Column-selected bulk search over HPA (search_download.php)."""
        return self.client.search_download(query, columns)


    def resolve_symbol(self, symbol: str) -> str:
        """Resolve a gene symbol (or synonym) to its Ensembl gene ID."""
        rows = self.client.search_download(symbol, RESOLVE_COLUMNS)
        want = symbol.upper()
        exact = [r for r in rows
                 if str(r.get("Gene", "")).upper() == want and r.get("Ensembl")]
        if not exact:
            exact = [r for r in rows if r.get("Ensembl") and any(
                str(s).upper() == want for s in (r.get("Gene synonym") or []))]
        ensgs = sorted({r["Ensembl"] for r in exact})
        if not ensgs:
            raise SymbolNotFoundError(symbol)
        if len(ensgs) > 1:
            raise AmbiguousSymbolError(
                symbol, [(r.get("Gene"), r["Ensembl"]) for r in exact])
        return ensgs[0]


    def get_gene(self, gene: str, full: bool = False) -> dict:
        """Per-gene HPA record for an Ensembl gene ID or gene symbol.

        full=False (default): grouped stable summary (records.summarize).
        full=True: the complete per-gene JSON dict as served by HPA.
        """
        ensg = gene if is_ensg(gene) else self.resolve_symbol(gene)
        record = self.client.gene_json(ensg)
        return record if full else summarize(record)
