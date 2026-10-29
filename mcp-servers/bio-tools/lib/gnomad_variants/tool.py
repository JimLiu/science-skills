"""gnomad-variants public surface: the 10 mirrored MCP methods."""
from __future__ import annotations

from . import queries as Q
from .client import GnomadClient, NotFound
from .records import (build_clinvar_row, build_constraint_record, build_mito_row,
                      build_short_variant_row, build_sv_row, build_variant_record,
                      sort_rows)


DATASETS = frozenset({
    "gnomad_r4", "gnomad_r4_non_ukb",
    "gnomad_r3", "gnomad_r3_controls_and_biobanks", "gnomad_r3_non_cancer",
    "gnomad_r3_non_neuro", "gnomad_r3_non_topmed", "gnomad_r3_non_v2",
    "gnomad_r2_1", "gnomad_r2_1_controls", "gnomad_r2_1_non_cancer",
    "gnomad_r2_1_non_neuro", "gnomad_r2_1_non_topmed", "exac",
})
SV_DATASETS = frozenset({"gnomad_sv_r4", "gnomad_sv_r2_1"})



DEFAULT_DATASET = "gnomad_r3"
DEFAULT_SV_DATASET = "gnomad_sv_r4"
MAX_REGION_BP = 1_000_000


def _check_dataset(dataset: str, allowed=DATASETS) -> str:
    if dataset not in allowed:
        raise ValueError(f"unknown dataset {dataset!r}; allowed: {sorted(allowed)}")
    return dataset


def _gene_args(gene_symbol: str | None, gene_id: str | None) -> dict:
    if (gene_symbol is None) == (gene_id is None):
        raise ValueError("pass exactly one of gene_symbol / gene_id")
    return {"symbol": gene_symbol, "geneId": gene_id}


class GnomadVariants:
    """High-level retrieval methods over the gnomAD GraphQL API."""

    def __init__(self, client: GnomadClient | None = None):
        self.client = client or GnomadClient()


    def get_variant(self, variant_id: str, dataset: str = DEFAULT_DATASET) -> dict | None:
        _check_dataset(dataset)
        try:
            data = self.client.query(Q.VARIANT, {"variantId": variant_id, "dataset": dataset})
        except NotFound:
            return None
        v = data.get("variant")
        return build_variant_record(v, dataset) if v else None


    def search_variants(self, query: str, dataset: str = DEFAULT_DATASET) -> list[str]:
        _check_dataset(dataset)
        data = self.client.query(Q.VARIANT_SEARCH, {"query": query, "dataset": dataset})
        return sorted(r["variant_id"] for r in data.get("variant_search") or [])


    def gene_variants(self, gene_symbol: str | None = None, gene_id: str | None = None,
                      dataset: str = DEFAULT_DATASET) -> dict:
        _check_dataset(dataset)
        args = _gene_args(gene_symbol, gene_id)
        args["dataset"] = dataset
        data = self.client.query(Q.GENE_VARIANTS, args)
        g = data.get("gene")
        if g is None:
            raise NotFound(f"gene {gene_symbol or gene_id}")
        rows = sort_rows([build_short_variant_row(v) for v in g.get("variants") or []])
        return {"gene_id": g["gene_id"], "symbol": g.get("symbol"),
                "chrom": g.get("chrom"), "start": g.get("start"), "stop": g.get("stop"),
                "dataset": dataset, "n_variants": len(rows), "variants": rows}


    def gene_constraint(self, gene_symbol: str | None = None,
                        gene_id: str | None = None) -> dict:
        data = self.client.query(Q.GENE_CONSTRAINT, _gene_args(gene_symbol, gene_id))
        g = data.get("gene")
        if g is None:
            raise NotFound(f"gene {gene_symbol or gene_id}")
        return build_constraint_record(g)


    def region_variants(self, chrom: str, start: int, stop: int,
                        dataset: str = DEFAULT_DATASET) -> dict:
        _check_dataset(dataset)
        if stop - start > MAX_REGION_BP:
            raise ValueError(f"region exceeds {MAX_REGION_BP} bp; split the query")
        data = self.client.query(Q.REGION_VARIANTS,
                                 {"chrom": chrom, "start": start, "stop": stop,
                                  "dataset": dataset})
        region = data.get("region") or {}
        rows = sort_rows([build_short_variant_row(v) for v in region.get("variants") or []])
        return {"chrom": chrom, "start": start, "stop": stop, "dataset": dataset,
                "n_variants": len(rows), "variants": rows}


    def liftover(self, variant_id: str, source_build: str = "GRCh37") -> list[dict]:
        if source_build not in ("GRCh37", "GRCh38"):
            raise ValueError("source_build must be GRCh37 or GRCh38")
        data = self.client.query(Q.LIFTOVER, {"source": variant_id, "rg": source_build})
        out = []
        for row in data.get("liftover") or []:
            out.append({"source": row.get("source"), "liftover": row.get("liftover"),
                        "datasets": sorted(row.get("datasets") or [])})
        return sorted(out, key=lambda r: (r["liftover"] or {}).get("variant_id") or "")


    def clinvar_variants(self, gene_symbol: str | None = None,
                         gene_id: str | None = None) -> dict:
        data = self.client.query(Q.CLINVAR_VARIANTS, _gene_args(gene_symbol, gene_id))
        g = data.get("gene")
        if g is None:
            raise NotFound(f"gene {gene_symbol or gene_id}")
        rows = sort_rows([build_clinvar_row(v) for v in g.get("clinvar_variants") or []])
        return {"gene_id": g["gene_id"], "symbol": g.get("symbol"),
                "clinvar_release_date": (data.get("meta") or {}).get("clinvar_release_date"),
                "n_variants": len(rows), "variants": rows}


    def structural_variants(self, gene_symbol: str | None = None,
                            gene_id: str | None = None,
                            dataset: str = DEFAULT_SV_DATASET) -> dict:
        _check_dataset(dataset, SV_DATASETS)
        args = _gene_args(gene_symbol, gene_id)
        args["dataset"] = dataset
        data = self.client.query(Q.STRUCTURAL_VARIANTS_GENE, args)
        g = data.get("gene")
        if g is None:
            raise NotFound(f"gene {gene_symbol or gene_id}")
        rows = sorted((build_sv_row(v) for v in g.get("structural_variants") or []),
                      key=lambda r: r["variant_id"])
        return {"gene_id": g["gene_id"], "symbol": g.get("symbol"), "dataset": dataset,
                "n_variants": len(rows), "variants": rows}


    def structural_variant(self, sv_id: str,
                           dataset: str = DEFAULT_SV_DATASET) -> dict | None:
        _check_dataset(dataset, SV_DATASETS)
        try:
            data = self.client.query(Q.STRUCTURAL_VARIANT,
                                     {"variantId": sv_id, "dataset": dataset})
        except NotFound:
            return None
        v = data.get("structural_variant")
        if v is None:
            return None
        row = build_sv_row(v)
        row["dataset"] = dataset
        return row


    def mitochondrial_variants(self, gene_symbol: str | None = None,
                               gene_id: str | None = None,
                               region: tuple[int, int] | None = None,
                               dataset: str = "gnomad_r4") -> dict:
        _check_dataset(dataset)
        if region is not None:
            if gene_symbol or gene_id:
                raise ValueError("pass gene OR region, not both")
            start, stop = region
            data = self.client.query(Q.MITOCHONDRIAL_VARIANTS_REGION,
                                     {"start": start, "stop": stop, "dataset": dataset})
            container = data.get("region") or {}
            scope = {"region": f"M:{start}-{stop}"}
        else:
            args = _gene_args(gene_symbol, gene_id)
            args["dataset"] = dataset
            data = self.client.query(Q.MITOCHONDRIAL_VARIANTS_GENE, args)
            container = data.get("gene")
            if container is None:
                raise NotFound(f"gene {gene_symbol or gene_id}")
            scope = {"gene_id": container["gene_id"], "symbol": container.get("symbol")}
        rows = sort_rows([build_mito_row(v)
                          for v in container.get("mitochondrial_variants") or []])
        return {**scope, "dataset": dataset, "n_variants": len(rows), "variants": rows}


    def run_battery(self, battery: dict) -> dict:
        out: dict = {}
        out["variants"] = [self.get_variant(s["variant_id"], s["dataset"])
                           for s in battery["variants"]]
        out["searches"] = [{"query": s["query"], "dataset": s["dataset"],
                            "variant_ids": self.search_variants(s["query"], s["dataset"])}
                           for s in battery["searches"]]
        out["constraint"] = [self.gene_constraint(gene_symbol=sym)
                             for sym in battery["constraint_genes"]]
        out["regions"] = [self.region_variants(s["chrom"], s["start"], s["stop"], s["dataset"])
                          for s in battery["regions"]]
        out["gene_variants"] = [self.gene_variants(gene_symbol=s["gene_symbol"],
                                                   dataset=s["dataset"])
                                for s in battery["gene_variants"]]
        out["liftover"] = [self.liftover(s["source_variant_id"], s["source_build"])
                           for s in battery["liftover"]]
        out["clinvar"] = [self.clinvar_variants(gene_symbol=sym)
                          for sym in battery["clinvar_genes"]]
        out["sv_lists"] = [self.structural_variants(gene_symbol=s["gene_symbol"],
                                                    dataset=s["dataset"])
                           for s in battery["sv_genes"]]
        out["svs"] = [self.structural_variant(s["sv_id"], s["dataset"])
                      for s in battery["svs"]]
        out["mito"] = [self.mitochondrial_variants(gene_symbol=s["gene_symbol"],
                                                   dataset=s["dataset"])
                       for s in battery["mito_genes"]]
        return out
