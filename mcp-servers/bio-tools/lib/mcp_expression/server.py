"""mcp-expression: gene/tissue expression domain server (FastMCP, stdio).

Tools are thin marshalling wrappers over two fleet packages:

  * gtex-expression   — complete, count-verified GTEx Portal API v2 retrieval
                        (every paged route walked to completion; datasetId
                        pinned explicitly on every call, default gtex_v8).
  * panglaodb-markers — cached, checksum-pinned PanglaoDB cell-type marker
                        table (honest identifying User-Agent + sha256 pin
                        handled by the fleet client; frozen upstream since
                        27 Mar 2020).

No retrieval logic lives here — pagination, throttling, retry, count
verification and caching are all fleet behavior.
"""
from __future__ import annotations

import threading

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

import re

from gtex_expression import GtexClient, GtexExpression
from panglaodb_markers import PanglaoDB



mcp = GuardedFastMCP("mcp-expression")


_gtex_client = GtexClient()

_panglao: PanglaoDB | None = None
_panglao_lock = threading.Lock()


def _gtex(dataset_id: str) -> GtexExpression:
    return GtexExpression(client=_gtex_client, dataset_id=dataset_id)


_UNVERSIONED_ENSG_RE = re.compile(r"^ENSG\d+$")


def _with_unversioned_hint(result: dict, *gene_ids: str | None) -> dict:
    """When a GTEx expression/eQTL call returns zero rows AND any input is an
    unversioned ENSG id, append an actionable hint (06-25 probe item 9 — GTEx
    silently returns 0 rows for unversioned ids instead of erroring). Result
    pass-through in every other case (populated result, versioned ids,
    symbol inputs)."""
    unversioned = [g for g in gene_ids
                   if g and _UNVERSIONED_ENSG_RE.match(str(g))]
    if not unversioned:
        return result
    n = (result.get("total") if isinstance(result.get("total"), int)
         else len(result.get("data") or result.get("records") or []))
    if n:
        return result
    return {**result, "hint": (
        f"GTEx requires VERSIONED GENCODE ids (e.g. ENSG00000141510.19); "
        f"unversioned id(s) {unversioned} return zero rows. Call "
        f"gtex_resolve_genes({unversioned!r}) first and pass the returned "
        f"gencodeId, or use gtex_expression_summary(gene=...) which resolves "
        f"the version automatically.")}


def _db() -> PanglaoDB:


    global _panglao
    with _panglao_lock:
        if _panglao is None:
            _panglao = PanglaoDB()
        return _panglao






@mcp.tool(annotations=READ_ONLY)
def gtex_tissue_sites(dataset_id: str = "gtex_v8") -> dict:
    """List all GTEx tissue sites with metadata for a pinned dataset release.

    Returns every tissue site (54 in gtex_v8) with tissueSiteDetailId, display
    name, sample counts, eGene count and color codes. `total` is the
    API-verified row count. Use the returned tissueSiteDetailId values (e.g.
    'Liver', 'Brain_Cortex', 'Whole_Blood') as the tissue argument of the
    other gtex_* tools.

    dataset_id pins the GTEx release (default 'gtex_v8', GENCODE v26/GRCh38;
    'gtex_v10' uses GENCODE v39 — gene ids differ between releases).
    """
    return _gtex(dataset_id).tissue_sites()


@mcp.tool(annotations=READ_ONLY)
def gtex_dataset_info() -> dict:
    """List all GTEx dataset releases with their metadata.

    Returns every dataset release (datasetId, GENCODE version, genome build,
    dbSNP build, sample/subject/tissue counts). Use this to choose a
    dataset_id to pin for the other gtex_* tools and to know which GENCODE
    version its gene ids come from.
    """
    return _gtex("gtex_v8").dataset_info()


@mcp.tool(annotations=READ_ONLY)
def gtex_sample_info(tissue_site_detail_id: str | None = None,
                     data_type: str | None = None,
                     subject_id: str | None = None,
                     max_samples: int | None = None,
                     dataset_id: str = "gtex_v8") -> dict:
    """Get GTEx sample and donor metadata, optionally filtered.

    Filters: tissue_site_detail_id (e.g. 'Liver'), data_type (e.g. 'RNASEQ',
    'WGS'), subject_id (e.g. 'GTEX-14753'). Returns sample IDs, tissue types,
    ischemic time, RIN scores, Hardy scale, pathology notes, etc.

    The full filtered set is walked and count-verified: `total` is the true
    match count. By default ALL matching samples are returned (an unfiltered
    call returns tens of thousands of rows — filter, or set max_samples).
    When max_samples is set, `returned` < `total` and `truncated` is true.
    """



    reject_blank(tissue_site_detail_id=tissue_site_detail_id,
                 data_type=data_type, subject_id=subject_id,
                 dataset_id=dataset_id)
    out = _gtex(dataset_id).sample_info(
        tissue_site_detail_id=tissue_site_detail_id, data_type=data_type,
        subject_id=subject_id, max_items=max_samples)
    out["truncated"] = out["returned"] < out["total"]
    return out


@mcp.tool(annotations=READ_ONLY)
def gtex_resolve_genes(gene_ids: list[str],
                       dataset_id: str = "gtex_v8") -> dict:
    """Resolve gene symbols or unversioned Ensembl ids to versioned GENCODE ids.

    GTEx expression/eQTL routes require versioned GENCODE ids (e.g.
    'ENSG00000111640.14') and the version differs between releases
    (gtex_v8 = GENCODE v26, gtex_v10 = v39). Pass symbols ('GAPDH') or bare
    ENSG ids; returns matching reference-gene records with gencodeId,
    geneSymbol, gencodeVersion and genomeBuild. Call this first, then feed
    gencodeId into the expression/eQTL tools.
    """
    return _gtex(dataset_id).resolve_genes(gene_ids)


@mcp.tool(annotations=READ_ONLY)
def gtex_median_expression(gencode_ids: list[str],
                           tissue_site_detail_ids: list[str] | None = None,
                           dataset_id: str = "gtex_v8") -> dict:
    """Get median gene expression (TPM) for genes x tissues.

    gencode_ids must be versioned GENCODE ids for the pinned dataset (resolve
    symbols with gtex_resolve_genes first — wrong-version ids return 0 rows).
    Omit tissue_site_detail_ids to get all tissues. Fully paged and
    count-verified; `total` equals the number of (gene, tissue) rows returned.
    """
    return _with_unversioned_hint(
        _gtex(dataset_id).median_expression(
            gencode_ids, tissue_site_detail_ids=tissue_site_detail_ids),
        *gencode_ids)


@mcp.tool(annotations=READ_ONLY)
def gtex_expression_summary(gene: str, dataset_id: str = "gtex_v8") -> dict:
    """Summarize a gene's expression across ALL GTEx tissues, ranked by median TPM.

    Accepts a gene symbol ('GAPDH') or Ensembl id; the symbol is resolved to
    the pinned dataset's versioned GENCODE id automatically. Returns the
    resolved gene record plus every tissue ranked by descending median TPM —
    the quickest way to profile baseline tissue specificity of a target.
    Errors if the symbol is not in the GTEx reference.
    """
    return _gtex(dataset_id).expression_summary(gene)


@mcp.tool(annotations=READ_ONLY)
def gtex_gene_expression(gencode_id: str,
                         tissue_site_detail_ids: list[str] | None = None,
                         dataset_id: str = "gtex_v8") -> dict:
    """Get sample-level (not aggregated) expression TPM arrays for one gene.

    Returns, per tissue, the full per-sample TPM array with n_samples — use
    for distribution analysis rather than medians. gencode_id must be a
    versioned GENCODE id for the pinned dataset (use gtex_resolve_genes).
    Omit tissue_site_detail_ids for all tissues (large: ~17k values across
    54 tissues in gtex_v8).
    """
    return _with_unversioned_hint(
        _gtex(dataset_id).gene_expression(
            gencode_id, tissue_site_detail_ids=tissue_site_detail_ids),
        gencode_id)


@mcp.tool(annotations=READ_ONLY)
def gtex_top_expressed_genes(tissue_site_detail_id: str, n: int = 50,
                             filter_mt_gene: bool = True,
                             dataset_id: str = "gtex_v8") -> dict:
    """Get the top-n genes by median TPM in one tissue (API-side ranking).

    filter_mt_gene=True (default) excludes mitochondrial genes, which
    otherwise dominate every tissue. Returns genes in rank order with their
    median TPM; `total_genes_in_ranking` reports the full ranking size
    (~56k genes) while `returned` is the requested head.
    """
    return _gtex(dataset_id).top_expressed(
        tissue_site_detail_id, n=n, filter_mt_gene=filter_mt_gene)


@mcp.tool(annotations=READ_ONLY)
def gtex_eqtl_genes(tissue_site_detail_id: str,
                    max_genes: int | None = None,
                    dataset_id: str = "gtex_v8") -> dict:
    """Get all eGenes (genes with >=1 significant cis-eQTL) for a tissue.

    The complete eGene list is walked page-by-page and count-verified
    (e.g. Pancreas in gtex_v8 has 9,660 eGenes); `total` is the true count.
    Rows include empirical p-values, q-values and log2 allelic fold change.
    Set max_genes to cap the returned rows (`truncated` flags the cap;
    `total` stays exact).
    """
    out = _gtex(dataset_id).eqtl_genes(tissue_site_detail_id,
                                       max_items=max_genes)
    out["truncated"] = out["returned"] < out["total"]
    return out


@mcp.tool(annotations=READ_ONLY)
def gtex_single_tissue_eqtls(gencode_id: str | None = None,
                             variant_id: str | None = None,
                             tissue_site_detail_id: str | None = None,
                             dataset_id: str = "gtex_v8") -> dict:
    """Get significant single-tissue eQTL associations for a gene and/or variant.

    Provide gencode_id (versioned, for the pinned dataset — use
    gtex_resolve_genes) and/or variant_id (GTEx format, e.g.
    'chr19_44908684_T_C_b38'); optionally restrict to one tissue. Returns
    precomputed significant associations with p-values and normalized effect
    sizes (NES). Fully paged and count-verified.
    """
    return _with_unversioned_hint(
        _gtex(dataset_id).single_tissue_eqtls(
            gencode_id=gencode_id, variant_id=variant_id,
            tissue_site_detail_id=tissue_site_detail_id),
        gencode_id)


@mcp.tool(annotations=READ_ONLY)
def gtex_multi_tissue_eqtls(gencode_id: str,
                            variant_id: str | None = None,
                            dataset_id: str = "gtex_v8") -> dict:
    """Get multi-tissue eQTL meta-analysis (METASOFT) results for a gene.

    Returns per-variant METASOFT rows with per-tissue m-values (posterior
    probability of an eQTL effect in each tissue), NES, p-values and standard
    errors. Without variant_id this can return hundreds of rows (one per
    tested variant) — pass variant_id to narrow. gencode_id must be versioned
    for the pinned dataset.
    """
    return _with_unversioned_hint(
        _gtex(dataset_id).multi_tissue_eqtls(gencode_id,
                                             variant_id=variant_id),
        gencode_id)


@mcp.tool(annotations=READ_ONLY)
def gtex_calculate_eqtl(gencode_id: str, variant_id: str,
                        tissue_site_detail_id: str,
                        dataset_id: str = "gtex_v8") -> dict:
    """Calculate an eQTL on the fly for any gene-variant pair in one tissue.

    Unlike gtex_single_tissue_eqtls (precomputed significant associations
    only), this computes the association dynamically — including
    non-significant pairs. Returns p-value, NES, t-statistic, MAF and the
    per-sample genotype/expression arrays (deterministically sorted by
    (genotype, expression); upstream returns them in random order).
    """
    return _gtex(dataset_id).calculate_eqtl(
        gencode_id, variant_id, tissue_site_detail_id)






@mcp.tool(annotations=READ_ONLY)
def panglaodb_marker_genes(cell_type: str | None = None,
                           organ: str | None = None,
                           species: str | None = None,
                           sensitivity_min: float | None = None,
                           specificity_max: float | None = None,
                           canonical_only: bool = False,
                           max_rows: int = 500) -> dict:
    """Get cell-type marker genes from the PanglaoDB marker table.

    Filters (all optional, combined with AND):
      * cell_type / organ — case-insensitive exact match (enumerate valid
        values with panglaodb_options).
      * species — 'Hs' (human) or 'Mm' (mouse); rows tagged for both match
        either.
      * sensitivity_min / specificity_max — thresholds on the
        species-specific sensitivity/specificity columns (human columns when
        species is omitted); rows with missing values are excluded.
      * canonical_only — keep only canonical markers.

    Rows carry gene symbol, nicknames, ubiquitousness index, product
    description, germ layer, organ and per-species sensitivity/specificity.
    `total_matching` is the full match count; at most max_rows rows are
    returned (`truncated` flags a cap — the unfiltered table has 8,286 rows).

    The dataset is served from a local checksum-verified cache and is frozen
    upstream since 27 Mar 2020 (a historical snapshot — symbols predate later
    HGNC/MGI updates).
    """
    rows = _db().marker_genes(
        cell_type=cell_type, organ=organ, species=species,
        sensitivity_min=sensitivity_min, specificity_max=specificity_max,
        canonical_only=canonical_only)
    returned = rows[:max_rows] if max_rows is not None else rows
    return {"total_matching": len(rows), "returned": len(returned),
            "truncated": len(returned) < len(rows), "markers": returned}


@mcp.tool(annotations=READ_ONLY)
def panglaodb_options() -> dict:
    """Enumerate valid PanglaoDB filter values: species, organs, cell types.

    Returns the distinct species tags, the 29 organs, the 178 cell types, and
    a cell_types_by_organ index — use these exact strings as
    panglaodb_marker_genes arguments. (Known upstream data artifacts are
    reported verbatim: 3 corrupt rows have species '4', 30 have 'None'.)
    """
    return _db().options()


@mcp.tool(annotations=READ_ONLY)
def panglaodb_cell_types_for_gene(gene_symbol: str,
                                  include_synonyms: bool = False) -> dict:
    """Reverse lookup: which cell types is a gene a marker for?

    Case-insensitive match on the official gene symbol; set
    include_synonyms=True to also match pipe-delimited nicknames (e.g. 'K19'
    -> KRT19). Returns full marker rows plus matched_via ('official symbol'
    or 'synonym').
    """
    rows = _db().cell_types_for_gene(gene_symbol,
                                     include_synonyms=include_synonyms)
    return {"total": len(rows), "matches": rows}


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
