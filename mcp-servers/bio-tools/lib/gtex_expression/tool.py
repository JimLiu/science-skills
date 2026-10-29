"""gtex-expression: complete, count-verified retrieval from the GTEx Portal API v2.

Mirrors the 12 tooluniverse/gtex MCP methods:

  tissue_sites          -> GET /dataset/tissueSiteDetail        (full paged walk)
  dataset_info          -> GET /metadata/dataset                (dataset releases)
  sample_info           -> GET /dataset/sample                  (full paged walk, filters)
  expression_summary    -> /reference/gene + /expression/medianGeneExpression (all tissues)
  median_expression     -> GET /expression/medianGeneExpression (genes x tissues)
  gene_expression       -> GET /expression/geneExpression       (sample-level TPM arrays)
  top_expressed         -> GET /expression/topExpressedGene     (first n, count-reported)
  eqtl_genes            -> GET /association/egene               (full paged walk)
  single_tissue_eqtls   -> GET /association/singleTissueEqtl    (full paged walk)
  multi_tissue_eqtls    -> GET /association/metasoft            (METASOFT meta-analysis)
  calculate_eqtl        -> GET /association/dyneqtl             (dynamic eQTL calculation)
  resolve_genes         -> GET /reference/gene                  (symbol -> versioned GENCODE id)

Every paged route is walked to completion and the retrieved row count is
verified against the API's own ``paging_info.totalNumberOfItems``; a mismatch
raises ``CountMismatch`` instead of returning silently truncated data.

``datasetId`` is an explicit argument everywhere (default ``gtex_v8``) — the
GTEx v2 API silently defaults to gtex_v8 today, but relying on the server-side
default is exactly the kind of unpinned behavior this tool exists to remove.
"""
from __future__ import annotations

import json
from typing import Any

from .client import GtexClient

DEFAULT_DATASET = "gtex_v8"
DEFAULT_PAGE_SIZE = 1000


_PAGED = True


class CountMismatch(RuntimeError):
    """Retrieved row count disagrees with the API-reported total."""


def canonicalize(obj: Any) -> bytes:
    """Stable byte serialization for equivalence checks (sorted keys, no spaces)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


class GtexExpression:
    """High-level, complete-retrieval interface to the GTEx Portal API v2."""

    def __init__(self, client: GtexClient | None = None,
                 dataset_id: str = DEFAULT_DATASET,
                 page_size: int = DEFAULT_PAGE_SIZE):
        self.client = client or GtexClient()
        self.dataset_id = dataset_id
        self.page_size = page_size


    def _walk(self, path: str, params: dict | None = None,
              max_items: int | None = None) -> dict:
        """Walk a paged route to completion (or to ``max_items``).

        Returns {"rows": [...], "total": N (API-reported), "pages_fetched": n}.
        Verifies len(rows) == total (or == max_items when truncating on purpose).
        """
        params = dict(params or {})
        params["itemsPerPage"] = (min(self.page_size, max_items)
                                  if max_items else self.page_size)
        page = 0
        rows: list = []
        total = None
        pages = 0
        while True:
            params["page"] = page
            payload = self.client.get_json(path, params=params)
            pi = payload["paging_info"]
            total = pi["totalNumberOfItems"]
            rows.extend(payload["data"])
            pages += 1
            if max_items is not None and len(rows) >= max_items:
                rows = rows[:max_items]
                break
            if page + 1 >= pi["numberOfPages"]:
                break
            page += 1
        expected = min(total, max_items) if max_items is not None else total
        if len(rows) != expected:
            raise CountMismatch(
                f"{path}: retrieved {len(rows)} rows but API reports "
                f"totalNumberOfItems={total} (expected {expected})")
        return {"rows": rows, "total": total, "pages_fetched": pages}


    def tissue_sites(self) -> dict:
        """All tissue sites with metadata for the pinned dataset."""
        w = self._walk("/dataset/tissueSiteDetail", {"datasetId": self.dataset_id})
        rows = sorted(w["rows"], key=lambda r: r["tissueSiteDetailId"])
        return {"dataset_id": self.dataset_id, "total": w["total"],
                "tissue_sites": rows}


    def dataset_info(self) -> dict:
        """All GTEx dataset releases (unpaged metadata route)."""
        payload = self.client.get_json("/metadata/dataset")
        rows = payload if isinstance(payload, list) else payload["data"]
        rows = sorted(rows, key=lambda r: r["datasetId"])
        return {"total": len(rows), "datasets": rows}


    def sample_info(self, tissue_site_detail_id: str | None = None,
                    data_type: str | None = None,
                    subject_id: str | None = None,
                    max_items: int | None = None) -> dict:
        """Sample records, optionally filtered; full paged walk unless max_items."""
        params: dict = {"datasetId": self.dataset_id}
        if tissue_site_detail_id:
            params["tissueSiteDetailId"] = tissue_site_detail_id
        if data_type:
            params["dataType"] = data_type
        if subject_id:
            params["subjectId"] = subject_id
        w = self._walk("/dataset/sample", params, max_items=max_items)
        rows = sorted(w["rows"], key=lambda r: r["sampleId"])
        return {"dataset_id": self.dataset_id, "total": w["total"],
                "returned": len(rows), "samples": rows}


    def resolve_genes(self, gene_ids: list[str]) -> dict:
        """Symbols / unversioned ENSG -> versioned GENCODE ids via /reference/gene."""
        w = self._walk("/reference/gene", {"geneId": gene_ids})
        rows = sorted(w["rows"], key=lambda r: (r["geneSymbol"], r["gencodeId"]))
        return {"total": w["total"], "genes": rows}


    def median_expression(self, gencode_ids: list[str],
                          tissue_site_detail_ids: list[str] | None = None) -> dict:
        """Median TPM for genes x tissues (all tissues when none given)."""
        params: dict = {"gencodeId": gencode_ids, "datasetId": self.dataset_id}
        if tissue_site_detail_ids:
            params["tissueSiteDetailId"] = tissue_site_detail_ids
        w = self._walk("/expression/medianGeneExpression", params)
        rows = sorted(w["rows"], key=lambda r: (r["gencodeId"], r["tissueSiteDetailId"]))
        return {"dataset_id": self.dataset_id, "total": w["total"],
                "medians": rows}


    def expression_summary(self, gene: str) -> dict:
        """Resolve a symbol, then median TPM across ALL tissues, ranked desc."""
        ref = self.resolve_genes([gene])
        exact = [g for g in ref["genes"] if g["geneSymbolUpper"] == gene.upper()
                 or g["gencodeId"] == gene or g["gencodeId"].split(".")[0] == gene]
        if not exact:
            raise ValueError(f"gene {gene!r} not found in GTEx reference")
        g = exact[0]
        med = self.median_expression([g["gencodeId"]])
        ranked = sorted(med["medians"], key=lambda r: (-r["median"], r["tissueSiteDetailId"]))
        return {"dataset_id": self.dataset_id,
                "gene": {"geneSymbol": g["geneSymbol"], "gencodeId": g["gencodeId"],
                         "gencodeVersion": g["gencodeVersion"],
                         "genomeBuild": g["genomeBuild"]},
                "n_tissues": len(ranked), "unit": "TPM",
                "tissues_ranked": [{"tissueSiteDetailId": r["tissueSiteDetailId"],
                                    "median": r["median"]} for r in ranked]}


    def gene_expression(self, gencode_id: str,
                        tissue_site_detail_ids: list[str] | None = None) -> dict:
        """Sample-level TPM arrays per tissue for one gene."""
        params: dict = {"gencodeId": gencode_id, "datasetId": self.dataset_id}
        if tissue_site_detail_ids:
            params["tissueSiteDetailId"] = tissue_site_detail_ids
        w = self._walk("/expression/geneExpression", params)
        rows = []
        for r in sorted(w["rows"], key=lambda r: r["tissueSiteDetailId"]):
            vals = r["data"]
            rows.append({"tissueSiteDetailId": r["tissueSiteDetailId"],
                         "gencodeId": r["gencodeId"], "geneSymbol": r["geneSymbol"],
                         "unit": r["unit"], "n_samples": len(vals), "tpm": vals})
        return {"dataset_id": self.dataset_id, "total": w["total"],
                "tissues": rows}


    def top_expressed(self, tissue_site_detail_id: str, n: int = 50,
                      filter_mt_gene: bool = True) -> dict:
        """Top-n genes by median TPM in one tissue (API-side ranking)."""
        params = {"tissueSiteDetailId": tissue_site_detail_id,
                  "datasetId": self.dataset_id,
                  "filterMtGene": "true" if filter_mt_gene else "false"}
        w = self._walk("/expression/topExpressedGene", params, max_items=n)
        return {"dataset_id": self.dataset_id,
                "tissueSiteDetailId": tissue_site_detail_id,
                "filter_mt_gene": filter_mt_gene,
                "total_genes_in_ranking": w["total"], "returned": len(w["rows"]),
                "genes": w["rows"]}


    def eqtl_genes(self, tissue_site_detail_id: str,
                   max_items: int | None = None) -> dict:
        """All eGenes for a tissue (full paged walk, count-verified)."""
        params = {"tissueSiteDetailId": tissue_site_detail_id,
                  "datasetId": self.dataset_id}
        w = self._walk("/association/egene", params, max_items=max_items)
        rows = sorted(w["rows"], key=lambda r: r["gencodeId"])
        return {"dataset_id": self.dataset_id,
                "tissueSiteDetailId": tissue_site_detail_id,
                "total": w["total"], "returned": len(rows), "egenes": rows}


    def single_tissue_eqtls(self, gencode_id: str | None = None,
                            variant_id: str | None = None,
                            tissue_site_detail_id: str | None = None) -> dict:
        """Significant single-tissue eQTLs for a gene and/or variant."""
        if not (gencode_id or variant_id):
            raise ValueError("provide gencode_id and/or variant_id")
        params: dict = {"datasetId": self.dataset_id}
        if gencode_id:
            params["gencodeId"] = gencode_id
        if variant_id:
            params["variantId"] = variant_id
        if tissue_site_detail_id:
            params["tissueSiteDetailId"] = tissue_site_detail_id
        w = self._walk("/association/singleTissueEqtl", params)
        rows = sorted(w["rows"], key=lambda r: (r["gencodeId"], r["variantId"],
                                                r["tissueSiteDetailId"]))
        return {"dataset_id": self.dataset_id, "total": w["total"],
                "eqtls": rows}


    def multi_tissue_eqtls(self, gencode_id: str,
                           variant_id: str | None = None) -> dict:
        """METASOFT multi-tissue meta-analysis rows for a gene (optionally 1 variant)."""
        params: dict = {"gencodeId": gencode_id, "datasetId": self.dataset_id}
        if variant_id:
            params["variantId"] = variant_id
        w = self._walk("/association/metasoft", params)
        rows = sorted(w["rows"], key=lambda r: r["variantId"])
        return {"dataset_id": self.dataset_id, "gencodeId": gencode_id,
                "total": w["total"], "associations": rows}


    def calculate_eqtl(self, gencode_id: str, variant_id: str,
                       tissue_site_detail_id: str) -> dict:
        """On-the-fly eQTL calculation (/association/dyneqtl) — unpaged.

        The upstream shuffles the order of the per-sample ``data`` /
        ``genotypes`` arrays on every call (verified live: identical multiset
        of (genotype, expression) pairs, different permutation each request).
        Sample order carries no meaning on this route, so the pairs are
        re-sorted deterministically by (genotype, expression value) — the
        genotype<->expression pairing itself is preserved exactly.
        """
        payload = self.client.get_json("/association/dyneqtl", params={
            "gencodeId": gencode_id, "variantId": variant_id,
            "tissueSiteDetailId": tissue_site_detail_id,
            "datasetId": self.dataset_id})
        out = {k: payload[k] for k in sorted(payload)}
        if isinstance(out.get("genotypes"), list) and isinstance(out.get("data"), list) \
                and len(out["genotypes"]) == len(out["data"]):
            pairs = sorted(zip(out["genotypes"], out["data"]))
            out["genotypes"] = [g for g, _ in pairs]
            out["data"] = [d for _, d in pairs]
        return {"dataset_id": self.dataset_id, **out}
