"""mcp-variants server — human variant lookup over gnomAD + CADD + ClinVar
+ dbSNP.

Tier-2 domain server (clean schemas; replaces the unshipped
tooluniverse/gnomad and tooluniverse/cadd connectors). Retrieval is done by
the accuracy-gated fleet packages ``gnomad-variants``, ``cadd-scores``,
``clinvar-records`` and ``dbsnp-records``; this layer is marshalling only.

Headline fix vs the old connector: tooluniverse/gnomad defaulted to the
``gnomad_r3`` dataset, which is genome-only and is missing r4 variants
entirely (e.g. BRAF V600E ``7-140753336-A-T`` is absent from r3). This
server defaults every gnomAD tool to ``gnomad_r4`` and pins the dataset in
every output.
"""

from __future__ import annotations

import os
from functools import lru_cache

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import reject_blank
from mcp_servers_common.errors import raise_on_error_payload
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp_servers_common.ua import (
    ContactEmailRequired, product_ua, require_contact_email)
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)



mcp = GuardedFastMCP("mcp-variants")



DEFAULT_DATASET = "gnomad_r4"
DEFAULT_SV_DATASET = "gnomad_sv_r4"
DEFAULT_CADD_VERSION = "GRCh38-v1.7"






NCBI_API_KEY = os.environ.get("NCBI_API_KEY") or None


def _contact_required_result(e: ContactEmailRequired) -> dict:
    return {"error": "contact_email_required", "message": str(e)}




@lru_cache(maxsize=1)
def _gnomad():
    from gnomad_variants import GnomadVariants
    return GnomadVariants()


@lru_cache(maxsize=1)
def _cadd():
    from cadd_scores import CaddScores
    return CaddScores()


@lru_cache(maxsize=1)
def _clinvar():
    from clinvar_records import ClinVarClient, ClinVarRecords
    client = ClinVarClient(
        email=require_contact_email(), tool="mcp-bio", api_key=NCBI_API_KEY)




    client.session.headers["User-Agent"] = product_ua()
    return ClinVarRecords(client)


@lru_cache(maxsize=1)
def _dbsnp():
    from dbsnp_records import DbsnpClient, DbsnpRecords
    client = DbsnpClient(
        email=require_contact_email(), tool="mcp-bio", api_key=NCBI_API_KEY)
    client.session.headers["User-Agent"] = product_ua()
    return DbsnpRecords(client)




@mcp.tool(annotations=READ_ONLY)
def get_variant(variant_id: str, dataset: str = DEFAULT_DATASET) -> dict:
    """Look up one gnomAD short variant by ID and return its population
    frequencies.

    Args:
        variant_id: gnomAD variant ID ``chrom-pos-ref-alt`` on the dataset's
            reference build (GRCh38 for r3/r4, GRCh37 for r2.1/ExAC), e.g.
            ``19-44908822-C-T`` (APOE rs7412). Use ``search_variants`` to
            resolve an rsID first.
        dataset: gnomAD dataset pin. Default ``gnomad_r4`` (the current
            release, exomes+genomes). Other values: ``gnomad_r4_non_ukb``,
            ``gnomad_r3`` (+ ``_controls_and_biobanks``/``_non_cancer``/
            ``_non_neuro``/``_non_topmed``/``_non_v2`` subsets; genome-only),
            ``gnomad_r2_1`` (+ subsets; GRCh37), ``exac``.

    Returns a dict: ``found`` (bool), ``variant_id``, ``dataset`` (the pin
    actually queried), and ``variant`` — null when not found, else
    ``{variant_id, dataset, reference_genome, chrom, pos, ref, alt, rsids,
    exome: {ac, an, af, homozygote_count, hemizygote_count, filters},
    genome: {...same keys}}``. ``exome``/``genome`` are null where the
    dataset has no such call set (e.g. r3 is genome-only).
    """
    rec = _gnomad().get_variant(variant_id, dataset=dataset)
    return {"found": rec is not None, "variant_id": variant_id,
            "dataset": dataset, "variant": rec}


@mcp.tool(annotations=READ_ONLY)
def search_variants(query: str, dataset: str = DEFAULT_DATASET) -> dict:
    """Search gnomAD for variant IDs matching a query string (rsID like
    ``rs7412``, a variant ID, or a prefix). Use this to resolve rsIDs to
    ``chrom-pos-ref-alt`` IDs for ``get_variant``.

    Args:
        query: search string (rsID or variant-ID text).
        dataset: gnomAD dataset pin (default ``gnomad_r4``; see
            ``get_variant`` for the full enum).

    Returns ``{query, dataset, n_matches, variant_ids}`` with
    ``variant_ids`` sorted.
    """
    ids = _gnomad().search_variants(query, dataset=dataset)
    return {"query": query, "dataset": dataset,
            "n_matches": len(ids), "variant_ids": ids}


@mcp.tool(annotations=READ_ONLY)
def gene_variants(gene_symbol: str | None = None, gene_id: str | None = None,
                  dataset: str = DEFAULT_DATASET) -> dict:
    """List ALL gnomAD short variants in a gene (complete listing — can be
    thousands of rows for large genes).

    Args:
        gene_symbol: HGNC symbol (e.g. ``APOE``). Pass exactly one of
            gene_symbol / gene_id.
        gene_id: Ensembl gene ID (e.g. ``ENSG00000130203``).
        dataset: gnomAD dataset pin (default ``gnomad_r4``; see
            ``get_variant``).

    Returns ``{gene_id, symbol, chrom, start, stop, dataset, n_variants,
    variants}``; each variant row has ``{variant_id, pos, ref, alt, rsids,
    exome, genome}`` frequency blocks, sorted by (pos, variant_id).
    """
    return _gnomad().gene_variants(gene_symbol=gene_symbol, gene_id=gene_id,
                                   dataset=dataset)


@mcp.tool(annotations=READ_ONLY)
def gene_constraint(gene_symbol: str | None = None,
                    gene_id: str | None = None) -> dict:
    """gnomAD gene constraint metrics: pLI, observed/expected LoF-missense-
    synonymous counts with oe ratios + 90% CI bounds, and per-class
    z-scores. Use to judge a gene's intolerance to loss-of-function
    (pLI >= 0.9 or oe_lof_upper (LOEUF) < 0.6 ~ LoF-intolerant).

    Args:
        gene_symbol: HGNC symbol (e.g. ``TP53``). Pass exactly one of
            gene_symbol / gene_id.
        gene_id: Ensembl gene ID.

    Returns ``{gene_id, symbol, canonical_transcript_id, chrom, start, stop,
    strand, constraint: {exp_lof, obs_lof, oe_lof, oe_lof_lower,
    oe_lof_upper, exp_mis, obs_mis, oe_mis, oe_mis_lower, oe_mis_upper,
    exp_syn, obs_syn, oe_syn, oe_syn_lower, oe_syn_upper, pli, lof_z, mis_z,
    syn_z}}``.
    """
    return _gnomad().gene_constraint(gene_symbol=gene_symbol, gene_id=gene_id)


@mcp.tool(annotations=READ_ONLY)
def region_variants(chrom: str, start: int, stop: int,
                    dataset: str = DEFAULT_DATASET) -> dict:
    """List ALL gnomAD short variants in a genomic region (max 1 Mb — split
    larger regions into consecutive windows).

    Args:
        chrom: chromosome name without ``chr`` prefix (``1``-``22``, ``X``,
            ``Y``; use ``M`` region queries via ``mitochondrial_variants``).
        start: region start (1-based, inclusive).
        stop: region end (inclusive). ``stop - start`` must be <= 1,000,000.
        dataset: gnomAD dataset pin (default ``gnomad_r4``; determines the
            reference build of the coordinates — GRCh38 for r3/r4).

    Returns ``{chrom, start, stop, dataset, n_variants, variants}`` with the
    same lean variant rows as ``gene_variants``.
    """
    return _gnomad().region_variants(chrom, start, stop, dataset=dataset)


@mcp.tool(annotations=READ_ONLY)
def liftover_variant(variant_id: str, source_build: str = "GRCh37") -> dict:
    """Map a variant ID between reference builds (GRCh37 <-> GRCh38) using
    gnomAD's liftover table.

    Args:
        variant_id: source variant ID ``chrom-pos-ref-alt`` on
            ``source_build``.
        source_build: build of the input ID — ``GRCh37`` (default) or
            ``GRCh38``. The route is directional: a GRCh38 ID with
            ``source_build=GRCh37`` returns zero results, not an error.

    Returns ``{source_variant_id, source_build, n_results, results}``; each
    result is ``{source: {variant_id, reference_genome}, liftover:
    {variant_id, reference_genome}, datasets}``.
    """
    rows = _gnomad().liftover(variant_id, source_build=source_build)
    return {"source_variant_id": variant_id, "source_build": source_build,
            "n_results": len(rows), "results": rows}


@mcp.tool(annotations=READ_ONLY)
def clinvar_variants(gene_symbol: str | None = None,
                     gene_id: str | None = None) -> dict:
    """List ClinVar variants in a gene as mirrored by gnomAD, with clinical
    significance, review status and gold stars. The output pins gnomAD's
    ClinVar snapshot via ``clinvar_release_date``.

    Args:
        gene_symbol: HGNC symbol (e.g. ``BRCA1``). Pass exactly one of
            gene_symbol / gene_id.
        gene_id: Ensembl gene ID.

    Returns ``{gene_id, symbol, clinvar_release_date, n_variants,
    variants}``; each row: ``{variant_id, clinvar_variation_id,
    clinical_significance, gold_stars, review_status, major_consequence,
    pos, transcript_id, in_gnomad}``.
    """
    return _gnomad().clinvar_variants(gene_symbol=gene_symbol, gene_id=gene_id)


@mcp.tool(annotations=READ_ONLY)
def structural_variants(gene_symbol: str | None = None,
                        gene_id: str | None = None,
                        dataset: str = DEFAULT_SV_DATASET) -> dict:
    """List gnomAD structural variants (deletions, duplications, insertions,
    inversions, CNVs...) overlapping a gene.

    Args:
        gene_symbol: HGNC symbol (e.g. ``TP53``). Pass exactly one of
            gene_symbol / gene_id.
        gene_id: Ensembl gene ID.
        dataset: SV dataset pin — ``gnomad_sv_r4`` (default, GRCh38) or
            ``gnomad_sv_r2_1`` (GRCh37). SV IDs are release-specific.

    Returns ``{gene_id, symbol, dataset, n_variants, variants}``; rows carry
    SV ID, type, position/length, allele counts/frequencies, filters,
    per-gene consequences, and calling algorithms/evidence where present.
    """
    return _gnomad().structural_variants(gene_symbol=gene_symbol,
                                         gene_id=gene_id, dataset=dataset)


@mcp.tool(annotations=READ_ONLY)
def get_structural_variant(sv_id: str,
                           dataset: str = DEFAULT_SV_DATASET) -> dict:
    """Look up one gnomAD structural variant by its release-specific SV ID.

    Args:
        sv_id: SV ID as listed by ``structural_variants`` (e.g.
            ``DEL_chr17_599b1512`` in gnomad_sv_r4). IDs do NOT carry across
            releases.
        dataset: SV dataset pin — ``gnomad_sv_r4`` (default) or
            ``gnomad_sv_r2_1``. Must match the release the ID came from.

    Returns ``{found, sv_id, dataset, structural_variant}`` where
    ``structural_variant`` is null when not found.
    """
    rec = _gnomad().structural_variant(sv_id, dataset=dataset)
    return {"found": rec is not None, "sv_id": sv_id, "dataset": dataset,
            "structural_variant": rec}


@mcp.tool(annotations=READ_ONLY)
def mitochondrial_variants(gene_symbol: str | None = None,
                           gene_id: str | None = None,
                           region_start: int | None = None,
                           region_stop: int | None = None,
                           dataset: str = DEFAULT_DATASET) -> dict:
    """List gnomAD mitochondrial variants with heteroplasmy-aware counts
    (``ac_het``, ``ac_hom``, ``max_heteroplasmy``) for a mitochondrial gene
    OR a chrM coordinate window.

    Args:
        gene_symbol: mitochondrial gene symbol (e.g. ``MT-TL1``). Pass a
            gene (symbol or ID) OR a region, not both.
        gene_id: Ensembl gene ID of a mitochondrial gene.
        region_start: chrM window start (pass together with region_stop).
        region_stop: chrM window end (inclusive).
        dataset: gnomAD dataset pin (default ``gnomad_r4``).

    Returns ``{gene_id+symbol | region, dataset, n_variants, variants}``;
    each row: ``{variant_id, pos, ac_het, ac_hom, an, max_heteroplasmy,
    filters}``.
    """
    if (region_start is None) != (region_stop is None):
        raise ValueError("pass region_start and region_stop together")
    region = (region_start, region_stop) if region_start is not None else None
    return _gnomad().mitochondrial_variants(gene_symbol=gene_symbol,
                                            gene_id=gene_id, region=region,
                                            dataset=dataset)




@mcp.tool(annotations=READ_ONLY)
def cadd_variant_score(chrom: str, pos: int, ref: str, alt: str,
                       version: str = DEFAULT_CADD_VERSION) -> dict:
    """CADD deleteriousness score for one SNV (ref>alt at chrom:pos).
    Higher PHRED = more likely deleterious (>=20 ~ top 1% of the genome).
    The reference allele is verified against ``ref`` — wrong-build
    coordinates fail loudly instead of returning a real-looking score for
    the wrong locus.

    Args:
        chrom: chromosome ``1``-``22``, ``X`` or ``Y`` (``chr`` prefix
            tolerated; CADD scores nuclear SNVs only).
        pos: 1-based position on the build embedded in ``version``.
        ref: reference allele (A/C/G/T) — must match the genome at pos.
        alt: alternate allele (A/C/G/T), different from ref.
        version: CADD release pinned WITH its genome build, e.g.
            ``GRCh38-v1.7`` (default), ``GRCh37-v1.7``, ``GRCh38-v1.6``,
            ``GRCh37-v1.6``. A bare ``v1.7`` is rejected here because
            upstream silently returns no rows for it.

    Returns ``{query: {type, version, chrom, pos, ref, alt}, record:
    {chrom, pos, ref, alt, raw_score, phred}}``. ``raw_score``/``phred``
    are the API's exact decimal strings. Errors: reference-allele mismatch
    (wrong build/typo), no row for alt, or empty result (position outside
    the scored genome).
    """
    return _cadd().variant_score(chrom, pos, ref, alt, version=version)


@mcp.tool(annotations=READ_ONLY)
def cadd_position_scores(chrom: str, pos: int,
                         version: str = DEFAULT_CADD_VERSION) -> dict:
    """CADD scores for ALL possible single-nucleotide substitutions at one
    genomic position (up to 3 records, one per alt allele).

    Args:
        chrom: chromosome ``1``-``22``, ``X`` or ``Y``.
        pos: 1-based position on the build embedded in ``version``.
        version: build-pinned CADD release (default ``GRCh38-v1.7``; see
            ``cadd_variant_score``).

    Returns ``{query: {type, version, chrom, pos}, records: [{chrom, pos,
    ref, alt, raw_score, phred}, ...]}`` sorted by (chrom, pos, ref, alt).
    Raises an error if the position has no scored rows.
    """
    return _cadd().position_scores(chrom, pos, version=version)


@mcp.tool(annotations=READ_ONLY)
def cadd_range_scores(chrom: str, start: int, end: int,
                      version: str = DEFAULT_CADD_VERSION) -> dict:
    """CADD scores for every SNV in a genomic window [start, end]
    (inclusive, max 100 bp — split larger spans into consecutive windows).
    One request scores the whole window; far cheaper than per-position
    calls.

    Args:
        chrom: chromosome ``1``-``22``, ``X`` or ``Y``.
        start: window start (1-based, inclusive).
        end: window end (inclusive); ``end - start + 1`` <= 100.
        version: build-pinned CADD release (default ``GRCh38-v1.7``; see
            ``cadd_variant_score``).

    Returns ``{query: {type, version, chrom, start, end, span_bp},
    n_records, n_positions_scored, records}`` with records sorted by
    (chrom, pos, ref, alt).
    """
    return _cadd().range_scores(chrom, start, end, version=version)









@mcp.tool(annotations=READ_ONLY)
@raise_on_error_payload
def clinvar_search(query: str, max_records: int = 50) -> dict:
    """Search ClinVar directly (live NCBI, not gnomAD's snapshot) and return
    matching variation records with clinical significance, review status and
    gold stars.

    Requires a contact email (Settings → Privacy → 'Share contact email
    with research data services') per NCBI E-utilities usage policy.

    Args:
        query: ClinVar Entrez query. Free text works (``"TP53 R175H"``,
            a condition name, an HGVS string), and fielded terms compose
            with AND/OR/NOT — useful fields: ``BRCA1[gene]``,
            ``pathogenic[CLIN_SIG]`` (also ``likely_pathogenic``,
            ``uncertain_significance``, ``benign``, ``conflicting...``),
            ``"Lynch syndrome"[dis]``, ``single_nucleotide_variant[Type of
            variation]``, ``clinsig_has_assertion[PROP]``. An rsID
            (``rs121913529``) also works, but ``clinvar_variant_by_rsid``
            returns fuller records for that case.
        max_records: page cap, 1-200 (default 50). The match TOTAL is always
            reported; when total > max_records the list is a capped prefix
            and ``truncated`` is true (results come in ClinVar's default
            relevance/recency order — narrow the query to see the rest).

    Returns ``{term, total, n_returned, truncated, missing_uids, records}``.
    ``missing_uids`` lists any matched IDs whose summary document NCBI
    failed to return (rare, transient — distinct from truncation; retry to
    recover them). Each record:
    ``{variation_id, accession (VCV), accession_version, title, obj_type,
    variant_type, canonical_spdi, cdna_change, protein_change, rsids,
    other_xrefs, genes, molecular_consequences, locations (GRCh38+GRCh37
    coordinates), allele_frequencies, germline_classification,
    clinical_impact_classification, oncogenicity_classification (each:
    description, review_status, gold_stars 0-4, last_evaluated, conditions
    with ontology xrefs; null when ClinVar has no classification on that
    axis), n_submissions (SCV count), supporting_submissions}``.
    Quirk: NCBI E-utilities intermittently returns HTTP 500 under load —
    retry once more a few seconds later if that surfaces.
    """



    reject_blank(query=query)
    try:
        return _clinvar().search(query, retmax=max_records)
    except ContactEmailRequired as e:
        return _contact_required_result(e)


@mcp.tool(annotations=READ_ONLY)
@raise_on_error_payload
def clinvar_get_records(accessions: list[str]) -> dict:
    """Fetch full ClinVar records for a batch of VCV/RCV accessions or bare
    variation IDs.

    Requires a contact email (Settings → Privacy → 'Share contact email
    with research data services') per NCBI E-utilities usage policy.

    Args:
        accessions: up to 50 identifiers, mixed forms accepted —
            ``VCV000045122`` (versioned ``VCV000045122.3`` ok; resolved
            locally, free), ``RCV000019428`` (each RCV costs one extra
            lookup request), or a bare ClinVar variation ID (``45122``).
            rsIDs are rejected — use ``clinvar_variant_by_rsid``. Note an
            RCV (one variant-condition pair) resolves to its parent VCV
            variation record.

    Returns ``{n_requested, records, not_found, missing_uids,
    not_processed}``. Records carry the full shape documented in
    ``clinvar_search`` plus ``requested_as`` (which input(s) mapped to the
    record), sorted by variation_id. ``not_found`` lists unknown accessions
    (definitive absence — RCVs that esearch proves unknown); ``missing_uids``
    lists inputs whose summary document NCBI dropped or error-flagged: for
    an RCV that just resolved via esearch this is a transient drop (the
    record EXISTS — retry); for a VCV/numeric input it is either a transient
    drop or a nonexistent id — retry to disambiguate, never conclude absence
    from one call; ``not_processed``
    lists RCVs skipped because the per-call time budget ran out (re-request
    just those — VCV/numeric inputs always resolve, they never land there).
    Never silently drops an input.
    """
    try:
        return _clinvar().records_by_accessions(accessions)
    except ContactEmailRequired as e:
        return _contact_required_result(e)


@mcp.tool(annotations=READ_ONLY)
@raise_on_error_payload
def clinvar_variant_by_rsid(rsid: str, max_records: int = 50) -> dict:
    """All ClinVar variation records that reference a dbSNP rsID, with full
    classifications (an rsID can map to several VCVs — one per alternate
    allele, e.g. rs121913529 covers KRAS G12D/G12V/G12A).

    Requires a contact email (Settings → Privacy → 'Share contact email
    with research data services') per NCBI E-utilities usage policy.

    Args:
        rsid: dbSNP reference SNP ID, e.g. ``rs7412`` (case-insensitive;
            must match ``rs<digits>``).
        max_records: cap, 1-200 (default 50); ``total`` always carries the
            true match count and ``truncated`` flags a capped listing.

    Returns ``{rsid, total, n_returned, truncated, missing_uids, records}``
    with the full record shape documented in ``clinvar_search`` (review
    status, gold stars, last-evaluated dates, SCV counts — the fields
    gnomAD's ClinVar mirror lacks). Records come in ClinVar's relevance
    order; ``missing_uids`` lists matches whose summary NCBI dropped
    (transient). ``total == 0`` means ClinVar has no record for the rsID.
    """
    try:
        return _clinvar().records_by_rsid(rsid, retmax=max_records)
    except ContactEmailRequired as e:
        return _contact_required_result(e)




@mcp.tool(annotations=READ_ONLY)
@raise_on_error_payload
def dbsnp_get_rsids(rsids: list[str]) -> dict:
    """Canonical dbSNP RefSNP records for a batch of rsIDs: GRCh38+GRCh37
    placements, alleles, gene context, per-study allele frequencies, and
    ClinVar cross-references.

    Requires a contact email (Settings → Privacy → 'Share contact email
    with research data services') per NCBI E-utilities usage policy.

    Args:
        rsids: up to 20 rsIDs (``rs<digits>``, case-insensitive). Each
            costs one paced NCBI Variation Services request, so large
            batches take ~1 s per rsID.

    Returns ``{n_requested, records, not_found, not_processed}``.
    ``not_found`` lists rs numbers dbSNP doesn't know; ``not_processed``
    lists rsIDs skipped when the per-call time budget ran out (re-request
    just those). Each record: ``{rsid, status, create_date,
    last_update_date, last_update_build_id, n_citations, citations_pmids
    (capped at 20, ``citations_truncated`` flags the cap), variant_type,
    mane_select_ids, placements, alleles}``. ``status`` is ``live``,
    ``merged`` (record instead carries ``merged_into`` — re-query those
    rsIDs), or ``no_data`` (withdrawn/unsupported). ``placements`` give
    1-based chromosome coordinates with ref/alts per assembly (GRCh38
    first, ``is_primary`` true). Each alt-allele entry: ``{allele, ref,
    spdi (0-based interbase), hgvs, frequencies: [{study, study_version,
    allele_count, total_count, af}] (ALFA, 1000Genomes, TOPMED, gnomAD...),
    clinvar: [{rcv_accession, clinical_significances, review_status,
    last_evaluated_date, disease_names}], genes: [{symbol, gene_id, name,
    orientation, consequences (SO terms), mane_select: [{transcript_hgvs,
    protein_spdi}]}]}``.
    """
    try:
        return _dbsnp().get_rsids(rsids)
    except ContactEmailRequired as e:
        return _contact_required_result(e)


@mcp.tool(annotations=READ_ONLY)
@raise_on_error_payload
def dbsnp_search_by_region(chrom: str, start: int, stop: int,
                           assembly: str = "GRCh38",
                           max_rsids: int = 200) -> dict:
    """List dbSNP rsIDs in a genomic window (esearch db=snp positional
    index — NCBI Variation Services has no region endpoint).

    Requires a contact email (Settings → Privacy → 'Share contact email
    with research data services') per NCBI E-utilities usage policy.

    Args:
        chrom: chromosome ``1``-``22``, ``X``, ``Y`` or ``MT`` (``chr``
            prefix tolerated).
        start: window start, 1-based inclusive.
        stop: window end, inclusive; span capped at 1 Mb (split larger
            regions into consecutive windows). Dense regions hold many
            thousands of rsIDs per kb — keep windows small or raise
            max_rsids.
        assembly: which positional index to query — ``GRCh38`` (default)
            or ``GRCh37``. Coordinates must be on the chosen assembly.
        max_rsids: listing cap, 1-1000 (default 200).

    Returns ``{chrom, start, stop, assembly, term (the exact Entrez query
    used), total (the API's own count), n_returned, truncated, rsids}``.
    ``truncated`` is true when total > n_returned — the list is then a
    prefix in Entrez default order (descending rs number), never a silent
    truncation. Feed rsIDs (<= 20 at a time) to ``dbsnp_get_rsids`` for
    full records.
    """
    try:
        return _dbsnp().search_by_region(chrom, start, stop, assembly=assembly,
                                         max_rsids=max_rsids)
    except ContactEmailRequired as e:
        return _contact_required_result(e)


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
