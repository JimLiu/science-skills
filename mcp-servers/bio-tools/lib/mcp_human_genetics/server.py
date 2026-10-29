"""mcp-human-genetics server — human genetic-association evidence.

Tier-2 domain server unioning three keyless public sources:

  * NHGRI-EBI **GWAS Catalog** (REST API v2) — curated genome-wide
    association study results (variant/gene/trait -> associations, studies);
  * **eQTL Catalogue** (API v2) — uniformly reprocessed molecular-QTL
    summary statistics across 750+ datasets (gene/variant/region -> eQTLs);
  * **PheWeb portals** (FinnGen R12, BioBank Japan) — biobank-scale PheWAS:
    one variant or gene against thousands of disease endpoints.

Retrieval is done by the fleet packages ``gwas_catalog``, ``eqtl_catalogue``
and ``pheweb_portals``; this layer is marshalling only.

Listing honesty: every list output carries ``returned`` + ``truncated``
(and ``api_total`` where the upstream publishes its own total); capped
results are most-significant-first prefixes wherever the source supports a
significance ordering. Silent truncation is impossible.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from mcp_servers_common.guarded import GuardedFastMCP
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)



mcp = GuardedFastMCP("mcp-human-genetics")

DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_PHENOS = 200




@lru_cache(maxsize=1)
def _gwas():
    from gwas_catalog import GwasCatalog
    return GwasCatalog()


@lru_cache(maxsize=1)
def _eqtl():
    from eqtl_catalogue import EqtlCatalogue
    return EqtlCatalogue()


@lru_cache(maxsize=1)
def _pheweb():
    from pheweb_portals import PhewebPortals
    return PhewebPortals()


def _asc_key(value):
    """None-last ascending sort key — rows missing a statistic must never
    crowd out real signal in a capped most-significant-first prefix."""
    return (value is None, value if value is not None else 0.0)


def _significance(row: dict):
    """Best available -log10(p) for a PheWAS row. FinnGen's ``mlogp`` is the
    primary statistic — its ``pval`` saturates at 5e-324 (live: mlogp 333 to
    1259 all share pval=5e-324), so pval alone cannot rank top hits; classic
    pheweb rows carry only ``pval``."""
    mlogp = row.get("mlogp")
    if mlogp is not None:
        return mlogp
    pval = row.get("pval")
    if pval is None:
        return None
    if pval <= 0.0:
        return math.inf
    return -math.log10(pval)


def _pval_key(row: dict) -> tuple:
    sig = _significance(row)
    return _asc_key(-sig if sig is not None else None)


def _cap_rows(rows: list, max_records: int) -> dict[str, Any]:
    if max_records < 1:
        raise ValueError("max_records/max_phenos must be >= 1")
    return {"total": len(rows), "returned": min(len(rows), max_records),
            "truncated": len(rows) > max_records, "rows": rows[:max_records]}




@mcp.tool(annotations=READ_ONLY)
def gwas_associations_for_variant(rs_id: str,
                                  max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """GWAS Catalog associations reported for one variant (rsID), most
    significant first.

    Args:
        rs_id: dbSNP rsID, e.g. ``rs7412`` (APOE) or ``rs699`` (AGT). Must be
            the catalog's current rsID — merged/retired IDs may return zero
            rows rather than an error.
        max_records: output cap (default 500). Trait-hub variants can carry
            1000+ associations; rows are server-sorted by p-value ascending,
            so a capped result is the top-signal prefix.

    Returns ``{rs_id, api_total, returned, truncated, associations}``.
    ``api_total`` is the catalog's own total; ``truncated`` flags a capped
    fetch. Each association row: ``{association_id, p_value,
    pvalue_mantissa, pvalue_exponent, pvalue_description, or_value, beta,
    ci_lower, ci_upper, range, risk_frequency, snp_effect_alleles (e.g.
    "rs7412-T"), rs_ids, locations ("chrom:pos"), mapped_genes, efo_traits
    [{efo_id, efo_trait}], bg_efo_traits, reported_trait,
    multi_snp_haplotype, snp_interaction, study_accession_id (GCST...),
    pubmed_id, first_author}``. ``or_value`` and ``beta`` are mutually
    exclusive per row (binary vs quantitative trait); ``p_value`` of 0.0
    means p < ~1e-308 (underflow — use mantissa/exponent).
    """
    out = _gwas().associations({"rs_id": rs_id.strip()}, max_records)
    return {"rs_id": rs_id.strip(), **out}


@mcp.tool(annotations=READ_ONLY)
def gwas_associations_for_gene(gene_symbol: str,
                               max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """GWAS Catalog associations whose variants are MAPPED to a gene
    (catalog's Ensembl pipeline mapping, not author-reported), most
    significant first.

    Args:
        gene_symbol: HGNC symbol, exact match, e.g. ``PCSK9``, ``APOE``.
            Case-sensitive upstream — pass the canonical uppercase symbol.
            Intergenic variants are mapped to flanking genes, so rows may
            sit outside the gene body.
        max_records: output cap (default 500); rows are server-sorted by
            p-value ascending, so a capped result is the top-signal prefix.

    Returns ``{gene_symbol, api_total, returned, truncated, associations}``
    with the same association row shape as ``gwas_associations_for_variant``.
    A nonexistent symbol returns ``api_total=0`` (exact-match filter), not
    an error.
    """
    out = _gwas().associations({"mapped_gene": gene_symbol.strip()},
                               max_records)
    return {"gene_symbol": gene_symbol.strip(), **out}





@mcp.tool(annotations=READ_ONLY, blank_as_absent={"efo_id", "efo_trait"})
def gwas_associations_for_trait(efo_id: str | None = None,
                                efo_trait: str | None = None,
                                max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """GWAS Catalog associations annotated to one EFO trait, most
    significant first.

    Args:
        efo_id: ontology term short form as used by the catalog, e.g.
            ``MONDO_0005010`` (coronary artery disorder), ``EFO_0004340``
            (body mass index), ``HP_0003124`` (hypercholesterolemia). The
            catalog migrated many historical EFO ids to MONDO/HP terms —
            resolve current ids with ``gwas_search_traits`` first (e.g.
            ``EFO_0001645`` no longer exists). Pass exactly one of efo_id /
            efo_trait.
        efo_trait: exact trait LABEL (e.g. ``coronary artery disorder``) —
            convenience alternative when you already know the precise label.
        max_records: output cap (default 500); rows are server-sorted by
            p-value ascending, so a capped result is the top-signal prefix.

    Returns ``{efo_id|efo_trait, api_total, returned, truncated,
    associations}`` with the same association row shape as
    ``gwas_associations_for_variant``. An unknown id/label returns
    ``api_total=0``, not an error.
    """




    _efo_id = (efo_id or "").strip()
    _efo_trait = (efo_trait or "").strip()
    if bool(_efo_id) == bool(_efo_trait):
        raise ValueError("pass exactly one of efo_id / efo_trait")
    filters = {"efo_id": _efo_id} if _efo_id else {"efo_trait": _efo_trait}
    out = _gwas().associations(filters, max_records)
    return {**filters, **out}


@mcp.tool(annotations=READ_ONLY)
def gwas_search_traits(query: str,
                       max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """Search GWAS Catalog EFO trait annotations by label substring — the
    entry point for resolving a disease/phenotype name to the ontology ids
    that ``gwas_associations_for_trait`` / ``gwas_search_studies`` take.

    Args:
        query: case-insensitive substring of the trait label, e.g.
            ``coronary`` matches "coronary artery disorder"
            (MONDO_0005010), "coronary artery calcification" (EFO_0004723)
            etc. The catalog mixes EFO, MONDO, HP and OBA ids — don't assume
            an EFO_ prefix.
        max_records: output cap (default 500).

    Returns ``{query, api_total, returned, truncated, efo_traits}``;
    each row ``{efo_id, efo_trait, uri}``, sorted by label. Count-verified
    against the catalog's own total when not capped.
    """




    if not query.strip():
        raise ValueError("query must be non-empty")
    out = _gwas().search_traits(query.strip(), max_records)
    return {"query": query.strip(), **out}


@mcp.tool(annotations=READ_ONLY)
def gwas_search_studies(efo_id: str | None = None,
                        efo_trait: str | None = None,
                        pubmed_id: str | None = None,
                        max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """Search GWAS Catalog studies by trait annotation or publication.

    Args:
        efo_id: ontology short form (e.g. ``MONDO_0005010``); resolve via
            ``gwas_search_traits``. Filters combine (AND) — usually pass
            just one.
        efo_trait: exact trait label alternative to efo_id.
        pubmed_id: PubMed ID of the study's publication, e.g. ``38714703``.
        max_records: output cap (default 500).

    Returns ``{filters, api_total, returned, truncated, studies}``; each
    study row: ``{accession_id (GCST...), disease_trait (author-reported),
    efo_traits, bg_efo_traits, pubmed_id, initial_sample_size,
    replication_sample_size, discovery_ancestry, replication_ancestry,
    genotyping_technologies, platforms, cohort,
    full_summary_stats_available, imputed, gxe}``. Count-verified against
    the catalog total when not capped. At least one filter is required
    (the unfiltered catalog is ~90k studies).
    """
    filters: dict[str, Any] = {}
    if efo_id:
        filters["efo_id"] = efo_id.strip()
    if efo_trait:
        filters["efo_trait"] = efo_trait.strip()
    if pubmed_id:
        filters["pubmed_id"] = str(pubmed_id).strip()
    if not filters:
        raise ValueError("pass at least one of efo_id / efo_trait / pubmed_id")
    out = _gwas().studies(filters, max_records)
    return {"filters": filters, **out}


@mcp.tool(annotations=READ_ONLY)
def gwas_get_study(accession_id: str) -> dict:
    """Fetch one GWAS Catalog study by its GCST accession.

    Args:
        accession_id: study accession, e.g. ``GCST90841394``. Listed in
            every association row (``study_accession_id``) and study search
            result.

    Returns ``{found, accession_id, study}`` where ``study`` is the same
    row shape as ``gwas_search_studies`` (null when the accession is
    unknown).
    """
    from gwas_catalog import NotFound
    acc = accession_id.strip()
    try:
        rec = _gwas().study(acc)
    except NotFound:
        return {"found": False, "accession_id": acc, "study": None}
    return {"found": True, "accession_id": acc, "study": rec}


@mcp.tool(annotations=READ_ONLY)
def gwas_get_variant(rs_id: str) -> dict:
    """Fetch one GWAS Catalog variant record (position, mapped genes,
    consequence) by rsID — lighter than pulling its associations.

    Args:
        rs_id: dbSNP rsID, e.g. ``rs7412``.

    Returns ``{found, rs_id, variant}``; ``variant`` is ``{rs_id, merged,
    functional_class, most_severe_consequence, alleles (e.g. "C/T
    (forward)"), mapped_genes, locations [{chromosome, position, region}],
    last_update_date}`` — positions are GRCh38 — or null when the rsID is
    not in the catalog. ``merged=1`` means the rsID was merged into another
    record upstream.
    """
    from gwas_catalog import NotFound
    rid = rs_id.strip()
    try:
        rec = _gwas().snp(rid)
    except NotFound:
        return {"found": False, "rs_id": rid, "variant": None}
    return {"found": True, "rs_id": rid, "variant": rec}




@mcp.tool(annotations=READ_ONLY)
def eqtl_list_datasets(study_label: str | None = None,
                       tissue_label: str | None = None,
                       quant_method: str | None = None,
                       max_records: int = 1000) -> dict:
    """List eQTL Catalogue datasets (one dataset = one study x tissue/cell
    type x quantification method).

    Args:
        study_label: exact study name, e.g. ``GTEx``, ``Alasoo_2018``,
            ``BLUEPRINT``.
        tissue_label: exact tissue/cell-type label, e.g. ``liver``,
            ``macrophage``, ``LCL``. (Exact match, lowercase in the
            catalogue.)
        quant_method: quantification method — ``ge`` (gene expression),
            ``exon``, ``tx``, ``txrev``, ``microarray``, ``leafcutter``,
            ``aptamer`` (plasma protein). For conventional gene-level eQTLs
            use ``ge``.
        max_records: output cap (default 1000; the full unfiltered
            catalogue is ~760 datasets, so the default returns everything).

    Returns ``{filters, returned, truncated, datasets}`` sorted by
    dataset_id; each dataset: ``{dataset_id (QTD...), study_id (QTS...),
    study_label, sample_group, tissue_id (ontology term), tissue_label,
    condition_label, quant_method, sample_size}``. The API publishes no
    total count; ``truncated=false`` proves the listing is complete
    (exhausted upstream).
    """
    filters: dict[str, Any] = {}
    if study_label:
        filters["study_label"] = study_label.strip()
    if tissue_label:
        filters["tissue_label"] = tissue_label.strip()
    if quant_method:
        filters["quant_method"] = quant_method.strip()
    out = _eqtl().datasets(filters, max_records)
    return {"filters": filters, **out}


@mcp.tool(annotations=READ_ONLY)
def eqtl_associations(dataset_id: str,
                      gene_id: str | None = None,
                      rsid: str | None = None,
                      variant: str | None = None,
                      pos: str | None = None,
                      nlog10p_min: float | None = None,
                      max_records: int = 1000) -> dict:
    """Molecular-QTL association rows from one eQTL Catalogue dataset,
    filtered by gene, variant or region.

    Args:
        dataset_id: QTD accession from ``eqtl_list_datasets``, e.g.
            ``QTD000266`` (GTEx liver, gene expression).
        gene_id: unversioned Ensembl gene ID, e.g. ``ENSG00000130203``
            (APOE). At least one of gene_id / rsid / variant / pos is
            required (the API rejects unfiltered scans).
        rsid: dbSNP rsID, e.g. ``rs7412``.
        variant: eQTL Catalogue variant string ``chr19_44908822_C_T``
            (chr-prefixed, underscore-separated, GRCh38).
        pos: genomic window ``chromosome:start-end`` (GRCh38, no chr
            prefix), e.g. ``19:44900000-44920000``.
        nlog10p_min: optional significance floor: only rows with
            -log10(p) >= this value (e.g. 5 keeps p <= 1e-5). Applied
            upstream.
        max_records: output cap (default 1000 = one page).

    Returns ``{dataset_id, filters, returned, truncated, associations}``;
    each row is the upstream record: ``{molecular_trait_id, gene_id,
    variant, rsid, chromosome, position, ref, alt, type (SNP/INDEL), beta,
    se, pvalue, nlog10p, maf, ac, an, r2, median_tpm}``. Rows cover ONLY
    the cis window the dataset tested (±1 Mb of each gene); an empty result
    means "not tested / not present", not "no eQTL anywhere". The API
    publishes no total count: ``truncated=false`` proves exhaustion,
    ``truncated=true`` means the cap was hit (narrow with nlog10p_min or a
    tighter region).
    """
    filters: dict[str, Any] = {}
    if gene_id:
        filters["gene_id"] = gene_id.strip()
    if rsid:
        filters["rsid"] = rsid.strip()
    if variant:
        filters["variant"] = variant.strip()
    if pos:
        filters["pos"] = pos.strip()
    if not filters:
        raise ValueError(
            "pass at least one of gene_id / rsid / variant / pos — the API "
            "rejects unfiltered association scans")
    if nlog10p_min is not None:
        filters["nlog10p"] = nlog10p_min
    out = _eqtl().associations(dataset_id.strip(), filters, max_records)
    return {**out, "filters": filters}




@mcp.tool(annotations=READ_ONLY)
def phewas_instances() -> dict:
    """List the public PheWeb PheWAS portals this server can query, with
    genome build and capability registry.

    Returns ``{instances: {key: {label, base_url, genome_build,
    capabilities, notes}}}``. ``capabilities`` name the endpoints each
    instance exposes: ``variant`` (phewas_variant), ``gene``
    (phewas_finngen_gene), ``phenotypes`` (phewas_list_phenotypes),
    ``autocomplete`` (phewas_search_phenotypes). NOTE the build split:
    FinnGen R12 variant IDs are GRCh38; BioBank Japan (pheweb.jp) is
    GRCh37/hg19 — liftover coordinates before cross-querying.
    """
    from pheweb_portals import INSTANCES
    return {"instances": {k: dict(v) for k, v in INSTANCES.items()}}


@mcp.tool(annotations=READ_ONLY)
def phewas_variant(instance: str, variant: str,
                   max_phenos: int = DEFAULT_MAX_PHENOS) -> dict:
    """PheWAS for one variant: its association statistics against every
    phenotype in a biobank PheWeb portal, most significant first.

    Args:
        instance: ``finngen`` (FinnGen R12, GRCh38) or ``bbj`` (BioBank
            Japan, GRCh37/hg19) — see ``phewas_instances``. Variant
            coordinates MUST be on the instance's build.
        variant: ``chrom-pos-ref-alt`` (``:``/``_`` separators and ``chr``
            prefix tolerated), e.g. ``19-44908822-C-T`` (APOE rs7412,
            GRCh38/finngen) or ``1-55505647-G-T`` (PCSK9 rs11591147,
            GRCh37/bbj).
        max_phenos: output cap (default 200). FinnGen returns ~2470
            phenotype rows per variant; rows are sorted by p-value
            ascending before capping.

    Returns ``{instance, genome_build, variant, variant_meta, total,
    returned, truncated, phenotypes}``; ``variant_meta`` carries {chrom,
    pos, ref, alt, rsids, nearest_genes, gnomad (lean AF block,
    FinnGen only)}. Each phenotype row: ``{phenocode, phenostring,
    category, pval, mlogp, beta, sebeta, af|maf, maf_case, maf_control,
    n_cases, n_controls, n_samples}`` (fields the instance doesn't publish
    are null; BBJ rows have af, FinnGen rows have maf triplets + mlogp).
    Unknown variants raise a not-found error.
    """
    from pheweb_portals import INSTANCES
    inst = instance.strip().lower()
    payload = _pheweb().variant_phenos(inst, variant)
    rows = sorted(payload["rows"], key=_pval_key)
    capped = _cap_rows(rows, max_phenos)
    return {"instance": inst,
            "genome_build": INSTANCES[inst]["genome_build"],
            "variant": variant, "variant_meta": payload["variant_meta"],
            "total": capped["total"], "returned": capped["returned"],
            "truncated": capped["truncated"], "phenotypes": capped["rows"]}


@mcp.tool(annotations=READ_ONLY)
def phewas_finngen_gene(gene_symbol: str,
                        max_phenos: int = DEFAULT_MAX_PHENOS) -> dict:
    """Gene-level PheWAS from FinnGen R12: for every disease endpoint, the
    best-associated variant in the gene region, most significant first.

    Args:
        gene_symbol: HGNC symbol, e.g. ``PCSK9``, ``APOE``. Unknown symbols
            raise a not-found error.
        max_phenos: output cap (default 200); FinnGen has ~2470 endpoints,
            one row each. Rows are sorted by p-value ascending before
            capping.

    Returns ``{instance: "finngen", genome_build: "GRCh38", gene_symbol,
    total, returned, truncated, phenotypes}``; each row is the
    ``phewas_variant`` row shape plus ``variant: {chrom, pos, ref, alt,
    varid, rsids}`` — the top variant for that endpoint in this gene's
    region (region != gene body; PheWeb pads gene boundaries). Most rows
    are null results (pval ~ 1) — the per-endpoint BEST variant is still
    reported; filter by pval yourself for significant hits.
    """
    rows = sorted(_pheweb().gene_phenos("finngen", gene_symbol.strip()),
                  key=_pval_key)
    capped = _cap_rows(rows, max_phenos)
    return {"instance": "finngen", "genome_build": "GRCh38",
            "gene_symbol": gene_symbol.strip(), "total": capped["total"],
            "returned": capped["returned"], "truncated": capped["truncated"],
            "phenotypes": capped["rows"]}


@mcp.tool(annotations=READ_ONLY)
def phewas_list_phenotypes(instance: str = "finngen",
                           max_records: int = 3000) -> dict:
    """Complete phenotype (disease endpoint) catalogue of a PheWeb
    instance, with case/control counts.

    Args:
        instance: currently only ``finngen`` exposes this endpoint (BBJ
            does not — use ``phewas_search_phenotypes`` there).
        max_records: output cap (default 3000 > FinnGen's ~2470 endpoints,
            so the default returns the complete catalogue).

    Returns ``{instance, total, returned, truncated, phenotypes}`` sorted
    by phenocode; each row: ``{phenocode (e.g. "T2D"), phenostring,
    category, num_cases, num_controls, num_gw_significant (count of
    genome-wide-significant loci for that endpoint)}``. Phenocodes are the
    ids used across FinnGen tools and at r12.finngen.fi/pheno/<code>.
    """
    rows = sorted(_pheweb().phenotypes(instance.strip().lower()),
                  key=lambda r: r.get("phenocode") or "")
    capped = _cap_rows(rows, max_records)
    return {"instance": instance.strip().lower(), "total": capped["total"],
            "returned": capped["returned"], "truncated": capped["truncated"],
            "phenotypes": capped["rows"]}


@mcp.tool(annotations=READ_ONLY)
def phewas_search_phenotypes(query: str, instance: str = "finngen",
                             max_records: int = DEFAULT_MAX_RECORDS) -> dict:
    """Search a PheWeb instance's phenotypes (and entities) by name — the
    entry point for resolving a disease name to a phenocode.

    Args:
        query: free-text phenotype query, e.g. ``diabetes``, ``asthma``.
            Matches phenotype names/codes; some instances also match gene
            names and rsIDs.
        instance: ``finngen`` (default) or ``bbj`` — both expose
            autocomplete.
        max_records: output cap (default 500; autocomplete responses are
            short lists, rarely capped).

    Returns ``{instance, query, total, returned, truncated, matches}``;
    each match: ``{display (human-readable label), phenocode, url
    (instance-relative, may be null)}``. Use the phenocode with
    ``phewas_list_phenotypes`` rows or the instance website; BBJ display
    strings embed the code in parentheses.
    """
    rows = _pheweb().autocomplete(instance.strip().lower(), query.strip())
    capped = _cap_rows(rows, max_records)
    return {"instance": instance.strip().lower(), "query": query.strip(),
            "total": capped["total"], "returned": capped["returned"],
            "truncated": capped["truncated"], "matches": capped["rows"]}


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
