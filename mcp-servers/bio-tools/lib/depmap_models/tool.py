"""depmap-models — public tool surface.

Mirrors the 5 tooluniverse/depmap MCP methods against the Sanger Cell Model
Passports JSON:API (SIDM model IDs / SIDG gene IDs):

    list_models(tissue=None, cancer_type=None)   complete, count-verified
    get_model(model_id_or_name)                  detail incl. tissue/MSI/ploidy
    search_models(q)                             server-side name search
    gene_dependencies(gene_symbol, model_id=None)  CRISPR KO rows (Sanger+Broad)
    search_genes(q, exact=False)                 symbol search

Every listing walks all JSON:API pages and asserts rows == meta.count.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from mcp_servers_common.pathseg import seg

from .client import CMPClient, CMPError
from .records import (shape_dependency, shape_gene, shape_model,
                      shape_model_row, sort_dependencies)

MODEL_INCLUDE = "sample.tissue,sample.cancer_type,model_msi_status"


def _like_literal(q: str) -> str:
    """Escape SQL-LIKE wildcards (and the backslash escape itself) so the
    query matches literally inside a pattern."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class DepMapModels:
    def __init__(self, client: CMPClient | None = None):
        self.client = client or CMPClient()



    def list_models(self, tissue: str | None = None,
                    cancer_type: str | None = None) -> dict[str, Any]:
        """All models, optionally filtered by tissue and/or cancer-type name
        (exact names as used by CMP, e.g. 'Lung', 'Small Cell Lung Carcinoma').
        Complete pagination walk; verifies row count against meta.count."""
        spec: list[dict[str, Any]] = []
        if tissue:
            spec.append({"name": "sample", "op": "has",
                         "val": {"name": "tissue", "op": "has",
                                 "val": {"name": "name", "op": "eq", "val": tissue}}})
        if cancer_type:
            spec.append({"name": "sample", "op": "has",
                         "val": {"name": "cancer_type", "op": "has",
                                 "val": {"name": "name", "op": "eq", "val": cancer_type}}})
        params = {"filter": self.client._filter(spec)} if spec else {}
        rows, total = [], 0
        for row, count in self.client._paged("/models", params):
            total = count
            rows.append(shape_model_row(row))
        if len(rows) != total:
            raise RuntimeError(
                f"pagination incomplete: {len(rows)} rows vs meta.count={total}")
        rows.sort(key=lambda r: r["model_id"])
        return {"count": total, "tissue": tissue, "cancer_type": cancer_type,
                "models": rows}



    def get_model(self, model_id_or_name: str) -> dict[str, Any]:
        """Model detail by SIDM ID or exact name/synonym (names op 'any')."""
        ident = model_id_or_name.strip()
        if ident.upper().startswith("SIDM"):
            doc = self.client._get(
                f"/models/{urllib.parse.quote(ident.upper(), safe='')}",
                {"include": MODEL_INCLUDE})
            if not doc.get("data"):
                raise CMPError(
                    f"no model with id {ident.upper()!r} "
                    "(the API returns an empty document for unknown SIDM ids)")
            return shape_model(doc["data"], doc.get("included"))
        spec = [{"name": "names", "op": "any", "val": ident}]
        doc = self.client._get(
            "/models", {"filter": self.client._filter(spec),
                        "include": MODEL_INCLUDE, "page[size]": "5"})
        data = doc.get("data") or []
        if not data:
            raise KeyError(f"no model with name {ident!r}")
        if len(data) > 1:
            ids = [d["id"] for d in data]
            raise KeyError(f"name {ident!r} is ambiguous: {ids}")
        return shape_model(data[0], doc.get("included"))



    def search_models(self, q: str) -> dict[str, Any]:
        """Server-side fuzzy model search (/search/{q}); models only.
        The query is sent verbatim (percent-encoded): whether /search is
        LIKE-backed is unverified (live probing of Sanger CMP is
        license-gated), so no escaping is applied there — only the
        dots-only segment guard."""
        doc = self.client._get(f"/search/{seg(str(q))}")
        rows = [shape_model_row(d) for d in doc.get("data") or []
                if d.get("type") == "model"]
        rows.sort(key=lambda r: r["model_id"])
        return {"query": q, "count": len(rows), "models": rows}



    def gene_dependencies(self, gene_symbol: str,
                          model_id: str | None = None) -> dict[str, Any]:
        """CRISPR KO dependency rows (dataset crispr_ko: Bayes factors,
        scaled BF, cleaned fold changes; sources Sanger and/or Broad) for one
        gene, optionally restricted to one model. Full pagination walk via the
        gene-scoped route (the unscoped 19.5M-row table is not queryable)."""
        gene = self._resolve_gene(gene_symbol)
        path = f"/genes/{urllib.parse.quote(str(gene['gene_id']), safe='')}/datasets/crispr_ko"
        params: dict[str, str] = {}
        if model_id:
            spec = [{"name": "model", "op": "has",
                     "val": {"name": "id", "op": "eq", "val": model_id.upper()}}]
            params["filter"] = self.client._filter(spec)
        rows, total = [], 0
        for row, count in self.client._paged(path, params):
            total = count
            rows.append(shape_dependency(row))
        if len(rows) != total:
            raise RuntimeError(
                f"pagination incomplete: {len(rows)} vs meta.count={total}")
        return {"gene": gene, "model_id": model_id, "count": total,
                "dependencies": sort_dependencies(rows)}



    def search_genes(self, q: str, exact: bool = False) -> dict[str, Any]:
        """Gene search by symbol. exact=True -> op eq; else case-insensitive
        substring (ilike %q%). NOTE: matches official CMP symbols only —
        synonym search is not supported upstream (names relationship 500s
        under ilike)."""
        if exact:
            spec = [{"name": "symbol", "op": "eq", "val": q}]
        else:
            spec = [{"name": "symbol", "op": "ilike", "val": f"%{_like_literal(q)}%"}]
        params = {"filter": self.client._filter(spec)}
        rows, total = [], 0
        for row, count in self.client._paged("/genes", params):
            total = count
            rows.append(shape_gene(row))
        if len(rows) != total:
            raise RuntimeError(
                f"pagination incomplete: {len(rows)} vs meta.count={total}")
        rows.sort(key=lambda r: r["gene_id"])
        return {"query": q, "exact": exact, "count": total, "genes": rows}



    def _resolve_gene(self, symbol: str) -> dict[str, Any]:
        spec = [{"name": "symbol", "op": "eq", "val": symbol}]
        doc = self.client._get("/genes", {"filter": self.client._filter(spec),
                                          "page[size]": "5"})
        data = doc.get("data") or []
        if not data:
            raise KeyError(f"no CMP gene with symbol {symbol!r}")
        if len(data) > 1:
            raise KeyError(f"symbol {symbol!r} ambiguous: {[d['id'] for d in data]}")
        return shape_gene(data[0])
