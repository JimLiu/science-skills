"""mcp-cancer-models server — cancer cell-line models, CRISPR dependencies,
and cBioPortal tumor-cohort genomics.

Tier-2 domain server (clean schemas; replaces the unshipped
tooluniverse/depmap connector). Retrieval is done by accuracy-gated fleet
packages; this layer is marshalling only:

  * ``depmap-models`` — Sanger Cell Model Passports (CMP) JSON:API;
    SIDM model IDs / SIDG gene IDs. All listings are COMPLETE pagination
    walks, count-verified against the API's own ``meta.count`` (the old
    connector did one request at the default page size with no count check).
  * ``cbioportal-studies`` — cBioPortal public REST API
    (www.cbioportal.org/api, keyless): patient-tumor sequencing studies,
    per-gene mutations / discrete copy-number events, cross-study mutation
    frequency, clinical attribute catalogues. Listings are complete and
    verified against the API's own ``total-count``, or explicitly capped
    with ``truncated`` + the true total — never silently truncated.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank, reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)



mcp = GuardedFastMCP("mcp-cancer-models")




@lru_cache(maxsize=1)
def _depmap():
    from depmap_models import DepMapModels
    return DepMapModels()


@mcp.tool(annotations=READ_ONLY)
def list_models(tissue: str | None = None,
                cancer_type: str | None = None) -> dict:
    """List cancer cell-line / organoid models from the Sanger Cell Model
    Passports, optionally filtered by tissue and/or cancer type. Complete
    pagination walk — the row count is verified against the API's own
    total, and ``count`` in the output is that verified total (no silent
    truncation). Unfiltered, this returns ~2000+ models; prefer a filter.

    Args:
        tissue: exact CMP tissue name (e.g. ``Lung``, ``Breast``,
            ``Large Intestine``). Case-sensitive exact match.
        cancer_type: exact CMP cancer-type name (e.g.
            ``Small Cell Lung Carcinoma``). Case-sensitive exact match.

    Returns ``{count, tissue, cancer_type, models}``; each model row:
    ``{model_id (SIDM...), names, model_type, growth_properties,
    crispr_ko_available, ...availability flags}``, sorted by model_id.
    """


    reject_blank(tissue=tissue, cancer_type=cancer_type)
    return _depmap().list_models(tissue=tissue, cancer_type=cancer_type)


@mcp.tool(annotations=READ_ONLY)
def get_model(model_id_or_name: str) -> dict:
    """Fetch one cell-line model's detail record by SIDM ID or exact
    name/synonym (e.g. ``SIDM00903`` or ``A549``; synonym matching is exact
    — ``K-562`` matches, ``K562`` does not; use ``search_models`` for fuzzy
    lookup).

    Returns ``{model_id, names, model_type, growth_properties,
    model_treatment, msi_status, ploidy_wes, ploidy_wgs, mutations_per_mb,
    tissue, cancer_type, sample_id}`` plus data-availability flags
    (``mutations_available``, ``cnv_available``, ``expression_available``,
    ``rnaseq_available``, ``crispr_ko_available``, ``drugs_available``,
    ``fusions_available``, ``methylation_available``,
    ``proteomics_available``, ``commercial_available``). Errors if the name
    is unknown or ambiguous (the error lists the candidate SIDM IDs).
    """
    return _depmap().get_model(model_id_or_name)


@mcp.tool(annotations=READ_ONLY)
def search_models(query: str) -> dict:
    """Fuzzy server-side search for cell-line models by name (e.g.
    ``K562``, ``HeLa``). Use this when you don't have the exact CMP
    name/synonym required by ``get_model``.

    Returns ``{query, count, models}`` with the same lean rows as
    ``list_models``, sorted by model_id.
    """


    if blank(query):
        raise ValueError("query must be non-empty")
    return _depmap().search_models(query)


@mcp.tool(annotations=READ_ONLY)
def gene_dependencies(gene_symbol: str, model_id: str | None = None) -> dict:
    """CRISPR knock-out dependency scores for one gene across cancer cell
    lines (dataset ``crispr_ko``; sources Sanger and Broad/DepMap). Higher
    Bayes factor = the cell line depends more on the gene (essentiality).
    Complete, count-verified walk — a whole-gene query returns ~1000+ rows
    (screened models × sources).

    Args:
        gene_symbol: official CMP gene symbol, exact (e.g. ``KRAS``). Use
            ``search_genes`` to resolve symbols first.
        model_id: optional SIDM model ID to restrict to one cell line
            (e.g. ``SIDM00903`` for A549 → typically 2 rows, one per
            source).

    Returns ``{gene: {gene_id (SIDG...), symbol, hgnc_id, cancer_driver,
    tumour_suppressor, ...}, model_id, count, dependencies}``; each row:
    ``{gene_id, model_id, source ('Sanger'|'Broad'), bf (Bayes factor),
    bf_scaled, fc_clean, fc_clean_qn (quantile-normalized cleaned fold
    change), mageck_fdr, qc_pass}``, sorted by (model_id, source).
    """
    return _depmap().gene_dependencies(gene_symbol, model_id=model_id)


@mcp.tool(annotations=READ_ONLY)
def search_genes(query: str, exact: bool = False) -> dict:
    """Look up genes in the Cell Model Passports by official symbol —
    resolves symbols to SIDG gene IDs for ``gene_dependencies``.

    Args:
        query: gene symbol or symbol fragment (e.g. ``BRCA`` matches BRCA1,
            BRCA2, ...).
        exact: True = exact symbol match; False (default) =
            case-insensitive substring. Matches OFFICIAL CMP symbols only —
            synonym search is not supported upstream.

    Returns ``{query, exact, count, genes}``; each gene:
    ``{gene_id (SIDG...), symbol, hgnc_id, hgnc_status, location,
    cancer_driver, tumour_suppressor, in_yusa_lib}``, sorted by gene_id.
    ``count`` is verified against the API total.
    """


    if blank(query):
        raise ValueError("query must be non-empty")
    return _depmap().search_genes(query, exact=exact)




@lru_cache(maxsize=1)
def _cbioportal():
    from cbioportal_studies import CBioPortalStudies
    return CBioPortalStudies()


@mcp.tool(annotations=READ_ONLY)
def cbioportal_list_studies(keyword: str | None = None,
                            cancer_type_id: str | None = None,
                            max_records: int = 500) -> dict:
    """Search/list cancer genomics studies on public cBioPortal — patient
    tumor sequencing cohorts (TCGA, MSK-IMPACT, GENIE subsets, trial
    cohorts...). Retrieval is complete and verified against the API's own
    total (~535 public studies); the output list is capped at
    ``max_records`` with an explicit ``truncated`` flag. Prefer a keyword —
    an unfiltered call returns every study.

    Args:
        keyword: server-side free-text match against study name /
            description / cancer type (e.g. ``glioma``, ``pancreatic``,
            ``msk``). Quirk (verified live): matching is token-based and
            inconsistent for long lowercase terms — ``glioma`` matches
            "Oligodendroglioma" studies but ``oligodendroglioma`` matches
            nothing. Prefer short stems (``glioma``, ``breast``) or
            exactly-capitalized words; broaden and filter client-side if a
            specific term returns 0.
        cancer_type_id: exact cBioPortal cancerTypeId to narrow results
            client-side (e.g. ``difg``, ``brca``, ``paad``, ``mixed``).
            Applied after the keyword match.
        max_records: output cap (default 500).

    Returns ``{keyword, cancer_type_id, api_total_for_keyword, count,
    truncated, studies}``; each study row: ``{study_id, name, description
    (trimmed ~240 chars), cancer_type_id, cancer_type, reference_genome,
    pmid, citation, sequenced_sample_count, cna_sample_count,
    structural_variant_count}``, sorted by study_id. ``count`` is the
    post-filter total; ``truncated`` is true when count > max_records.
    Quirk: the upstream ``allSampleCount`` field is broken (always 1) and
    is deliberately omitted — use ``sequenced_sample_count`` or
    ``cbioportal_get_study``'s verified ``sample_count``.
    """


    reject_blank(keyword=keyword, cancer_type_id=cancer_type_id)
    return _cbioportal().list_studies(keyword=keyword,
                                      cancer_type_id=cancer_type_id,
                                      max_records=max_records)


@mcp.tool(annotations=READ_ONLY)
def cbioportal_get_study(study_id: str) -> dict:
    """Fetch one cBioPortal study's full record: description, citation,
    verified sample/patient counts, per-platform data counts, and every
    molecular profile available for downstream queries (4 paced requests).

    Args:
        study_id: cBioPortal study id (e.g. ``msk_impact_2017``,
            ``brca_tcga_pan_can_atlas_2018``). Find ids with
            ``cbioportal_list_studies``. Unknown ids raise
            ``Study not found``.

    Returns the study record (``study_id, name, description, cancer_type,
    cancer_type_id, reference_genome, pmid, citation, public, groups,
    import_date``), data-availability counts (``sequenced_sample_count,
    cna_sample_count, mrna_rnaseq_v2_sample_count, rppa_sample_count,
    structural_variant_count, treatment_count, ...``), ``sample_count`` and
    ``patient_count`` (true totals from the API's own collection counts —
    NOT the broken upstream allSampleCount), and ``molecular_profiles``:
    ``[{molecular_profile_id, alteration_type (MUTATION_EXTENDED /
    COPY_NUMBER_ALTERATION / STRUCTURAL_VARIANT / MRNA_EXPRESSION / ...),
    datatype, name, description}]`` sorted by profile id — these ids feed
    the mutation/CNA tools implicitly (they auto-resolve profiles).
    """
    return _cbioportal().get_study(study_id)


@mcp.tool(annotations=READ_ONLY)
def cbioportal_mutations_in_gene(gene_symbol: str, study_id: str,
                                 max_records: int = 100) -> dict:
    """All somatic mutations in one gene across one cBioPortal study's
    samples: complete aggregate counts (always) + a capped, explicitly
    flagged per-mutation listing. Auto-resolves the study's
    MUTATION_EXTENDED profile and its all-samples list; the full row set is
    retrieved and verified against the API's own total before aggregating.

    Args:
        gene_symbol: HUGO symbol, e.g. ``KRAS``, ``TP53`` (resolved via the
            portal's gene service; unknown symbols raise ``Gene not
            found``).
        study_id: e.g. ``msk_impact_2017``. Studies without mutation data
            raise with the list of alteration types they DO have.
        max_records: cap on the detailed ``mutations`` list (default 100).
            Aggregates are computed over ALL rows regardless of the cap.

    Returns ``{gene: {symbol, entrez_gene_id}, study_id,
    molecular_profile_id, total_mutations, mutated_sample_count,
    mutation_type_counts, distinct_protein_changes, top_protein_changes
    (25 most recurrent, e.g. {'G12D': 181, ...}), truncated, mutations}``;
    each mutation row: ``{sample_id, patient_id, protein_change,
    mutation_type, mutation_status, chromosome, start/end_position,
    reference/variant_allele, variant_type, ncbi_build, protein_pos_start/
    end, tumor_alt_count, tumor_ref_count, refseq_mrna_id}``, sorted by
    genomic position. ``truncated`` flags total_mutations > max_records —
    never silently cut. Large cohorts (MSK-IMPACT: 10k+ samples) can carry
    thousands of rows for hot genes; the aggregates are usually what you
    want.
    """
    return _cbioportal().mutations_in_gene(gene_symbol, study_id,
                                           max_records=max_records)


@mcp.tool(annotations=READ_ONLY)
def cbioportal_mutation_frequency(gene_symbol: str,
                                  study_ids: list[str]) -> dict:
    """Fraction of sequenced samples carrying >= 1 somatic mutation in a
    gene, per study — compare a gene's mutation prevalence across up to 12
    cohorts in one call (e.g. KRAS: ~0.90 in paad_qcmg_uq_2016 vs ~0.15
    pan-cancer in msk_impact_2017).

    Args:
        gene_symbol: HUGO symbol (e.g. ``KRAS``).
        study_ids: 1-12 cBioPortal study ids (hard cap — call repeatedly
            for more; each study costs one paced request). Find ids with
            ``cbioportal_list_studies``.

    Returns ``{gene, count, frequencies, unknown_studies,
    no_mutation_data}``; ``frequencies`` rows (sorted by frequency desc):
    ``{study_id, study_name, molecular_profile_id, mutation_count (rows),
    mutated_samples (unique), sequenced_samples (denominator: the study's
    sequenced-sample total), frequency (rounded 4 dp; null if the study
    reports 0 sequenced samples)}``. Ids the API doesn't know land in
    ``unknown_studies``; studies lacking a mutation profile or an
    all-samples list land in ``no_mutation_data`` — nothing is silently
    dropped. Note mutated_samples counts samples, not patients (multi-
    sample patients count once per sample).
    """
    return _cbioportal().mutation_frequency(gene_symbol, study_ids)


@mcp.tool(annotations=READ_ONLY)
def cbioportal_cna_in_gene(gene_symbol: str, study_id: str,
                           event_type: str = "HOMDEL_AND_AMP",
                           max_records: int = 100) -> dict:
    """Discrete copy-number events (GISTIC-style calls) for one gene in one
    cBioPortal study: complete per-level counts (always) + a capped,
    flagged per-sample event listing. Auto-resolves the study's DISCRETE
    copy-number profile; retrieval is verified against the API's own total.

    Args:
        gene_symbol: HUGO symbol (e.g. ``EGFR``, ``CDKN2A``).
        study_id: e.g. ``msk_impact_2017``. Studies without discrete CNA
            data raise with the alteration types they DO have.
        event_type: which calls to return — ``HOMDEL_AND_AMP`` (default:
            high-confidence deep deletions + amplifications), ``HOMDEL``,
            ``AMP``, ``GAIN``, ``HETLOSS``, ``DIPLOID``, or ``ALL``.
            Warning: ``ALL``/``DIPLOID`` return ~one row per profiled
            sample (10k+ in large cohorts); aggregates still cover
            everything but the listing will be capped.
        max_records: cap on the detailed ``events`` list (default 100).

    Returns ``{gene, study_id, molecular_profile_id, event_type,
    total_events, altered_sample_count, alteration_counts (complete, e.g.
    {'amplification': 34}), truncated, events}``; each event:
    ``{sample_id, patient_id, alteration (-2..2), alteration_label
    (deep_deletion / shallow_deletion / diploid / gain / amplification)}``,
    sorted by sample_id. ``truncated`` flags total_events > max_records.
    """
    return _cbioportal().cna_in_gene(gene_symbol, study_id,
                                     event_type=event_type,
                                     max_records=max_records)


@mcp.tool(annotations=READ_ONLY)
def cbioportal_clinical_attributes(study_id: str,
                                   max_records: int = 200) -> dict:
    """Catalogue a cBioPortal study's clinical data dictionary — which
    patient/sample-level attributes exist (survival, stage, subtype,
    treatment, ...), before pulling clinical values elsewhere. Retrieval is
    complete and count-verified; the listing is capped with an explicit
    ``truncated`` flag.

    Args:
        study_id: e.g. ``brca_tcga_pan_can_atlas_2018`` (TCGA PanCan
            studies carry rich survival endpoints; targeted clinical
            cohorts are often sparser).
        max_records: cap on the ``attributes`` list (default 200; most
            studies have 20-100 attributes).

    Returns ``{study_id, total_attributes, patient_level_count,
    sample_level_count, survival_attributes (every OS_/DFS_/PFS_/DSS_
    attribute id present, e.g. ['OS_MONTHS','OS_STATUS',...]),
    has_overall_survival (OS_STATUS AND OS_MONTHS both present),
    truncated, attributes}``; each attribute: ``{attribute_id,
    display_name, description, datatype (STRING|NUMBER), level
    (patient|sample), priority}``, sorted by attribute_id.
    """
    return _cbioportal().clinical_attributes(study_id,
                                             max_records=max_records)


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
