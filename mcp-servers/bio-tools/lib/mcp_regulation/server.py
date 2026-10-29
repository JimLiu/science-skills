"""mcp-regulation: gene-regulation domain server (FastMCP, stdio).

Tools are thin marshalling wrappers over three fleet packages:

  * encode-search   — complete, count-verified retrieval from the ENCODE
                      portal JSON API (paged /report/ walk; /search/ ignores
                      `from` upstream, the fleet client handles it).
  * jaspar-matrices — complete, count-verified access to the JASPAR TFBS
                      profile REST API (full DRF pagination walks).
  * unibind-tfbs    — UniBind direct TF-DNA interactions: dataset catalog
                      (DRF REST) + genomic-region TFBS queries via the UCSC
                      hubApi against UniBind's registered public track hubs
                      (UniBind's own REST API has no region endpoint).

No retrieval logic lives here — pagination, throttling, retry and count
verification are all fleet behavior.

Note on JASPAR species filtering: the upstream ``species=`` query parameter
is silently IGNORED by jaspar.elixir.no; the working filter is ``tax_id=``.
These tools therefore expose ``tax_id`` only (the fleet maps it to the
parameter that actually filters).
"""
from __future__ import annotations

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

from encode_search import EncodeSearch
from jaspar_matrices import JasparClient
from jaspar_matrices import tool as jaspar
from unibind_tfbs import tool as unibind



mcp = GuardedFastMCP("mcp-regulation")

_encode = EncodeSearch()
_jaspar_client = JasparClient()


_unibind_client = unibind.make_unibind_client()
_ucsc_client = unibind.make_ucsc_client()


def _truncate(out: dict, rows_key_out: str, max_rows: int | None) -> dict:
    """Reshape a fleet search result: exact total + capped row list."""
    rows = out["rows"]
    kept = rows[:max_rows] if max_rows is not None else rows
    return {"total": out["total"], "returned": len(kept),
            "truncated": len(kept) < len(rows),
            "accessions": out["accessions"], rows_key_out: kept}






@mcp.tool(annotations=READ_ONLY)
def encode_search_experiments(assay_title: str | None = None,
                              target: str | None = None,
                              organism: str | None = None,
                              status: str = "released",
                              date_released_before: str | None = None,
                              extra_filters: dict[str, str] | None = None,
                              max_rows: int = 100) -> dict:
    """Search ENCODE functional genomics experiments (ChIP-seq, ATAC-seq, ...).

    Filters: assay_title (e.g. 'TF ChIP-seq', 'ATAC-seq'), target (protein
    label, e.g. 'CTCF'), organism (scientific name, e.g. 'Homo sapiens'),
    status (default 'released'), date_released_before (ISO date — makes the
    result set a closed historical window), plus arbitrary portal field
    filters via extra_filters (e.g. {"biosample_ontology.term_name": "K562"}).

    The COMPLETE result set is paged through and count-verified: `total` is
    exact and `accessions` lists every matching experiment accession. At most
    max_rows row summaries (assay, target, biosample, lab, date) are returned
    (`truncated` flags the cap). Broad queries walk every page server-side —
    always filter. Use encode_get_experiment for full detail on one hit.
    """



    reject_blank(extra_filters, assay_title=assay_title, target=target,
                 organism=organism, status=status,
                 date_released_before=date_released_before)
    out = _encode.search_experiments(
        assay_title=assay_title, target=target, organism=organism,
        status=status, date_released_before=date_released_before,
        extra_filters=extra_filters)
    return _truncate(out, "experiments", max_rows)


@mcp.tool(annotations=READ_ONLY)
def encode_search_biosamples(term_name: str | None = None,
                             classification: str | None = None,
                             organism: str | None = None,
                             status: str = "released",
                             date_created_before: str | None = None,
                             extra_filters: dict[str, str] | None = None,
                             max_rows: int = 100) -> dict:
    """Search ENCODE biosamples (cell lines, tissues, primary cells).

    Filters: term_name (ontology term, e.g. 'K562', 'liver'), classification
    ('cell line', 'tissue', 'primary cell', ...), organism (scientific name),
    status (default 'released'), date_created_before (ISO date), plus
    arbitrary portal field filters via extra_filters.

    Complete, count-verified retrieval: `total` is exact, `accessions` is the
    full matching list; at most max_rows row summaries are returned
    (`truncated` flags the cap). Use encode_get_biosample for one record's
    full detail.
    """

    reject_blank(extra_filters, term_name=term_name,
                 classification=classification, organism=organism,
                 status=status, date_created_before=date_created_before)
    out = _encode.search_biosamples(
        term_name=term_name, classification=classification,
        organism=organism, status=status,
        date_created_before=date_created_before, extra_filters=extra_filters)
    return _truncate(out, "biosamples", max_rows)


@mcp.tool(annotations=READ_ONLY)
def encode_list_files(file_format: str | None = None,
                      assay_term_name: str | None = None,
                      biosample_term_name: str | None = None,
                      status: str = "released",
                      date_created_before: str | None = None,
                      extra_filters: dict[str, str] | None = None,
                      max_rows: int = 100) -> dict:
    """List ENCODE data files by format / assay / biosample.

    Filters: file_format ('fastq', 'bam', 'bigWig', 'bed', ...),
    assay_term_name (e.g. 'ChIP-seq'), biosample_term_name (e.g. 'K562'),
    status (default 'released'), date_created_before (ISO date), plus
    arbitrary portal field filters via extra_filters (e.g.
    {"output_type": "peaks", "assembly": "GRCh38"}).

    CAUTION: assay_term_name takes the ontology term (what
    encode_search_experiments returns as assay_term_name, e.g. 'ChIP-seq',
    'ATAC-seq') — NOT the display assay_title (e.g. 'TF ChIP-seq',
    'Histone ChIP-seq'). An assay_title value here matches nothing and
    returns total=0; pass titles via extra_filters={"assay_title": ...}.

    Complete, count-verified: `total` is exact and `accessions` lists every
    matching file. File queries match MILLIONS of rows when unfiltered —
    always combine several filters (the full set is walked server-side). At
    most max_rows row summaries are returned (`truncated` flags the cap).
    Use encode_get_file for one file's full metadata (md5sum, href, ...).
    """


    reject_blank(extra_filters, file_format=file_format,
                 assay_term_name=assay_term_name,
                 biosample_term_name=biosample_term_name, status=status,
                 date_created_before=date_created_before)
    out = _encode.list_files(
        file_format=file_format, assay_term_name=assay_term_name,
        biosample_term_name=biosample_term_name, status=status,
        date_created_before=date_created_before, extra_filters=extra_filters)
    return _truncate(out, "files", max_rows)


@mcp.tool(annotations=READ_ONLY)
def encode_get_experiment(accession: str) -> dict:
    """Get one ENCODE experiment's metadata by accession (e.g. 'ENCSR000AKP').

    Returns a stable-field record: assay, target, biosample ontology +
    summary, description, lab, award project, release/submission dates,
    assemblies, replicate counts, replication type, dbxrefs, DOI and uuid.
    (Volatile portal fields — audits, re-run analyses, internal status — are
    deliberately excluded.)
    """
    return _encode.get_experiment(accession)


@mcp.tool(annotations=READ_ONLY)
def encode_get_file(accession: str) -> dict:
    """Get one ENCODE file's metadata by accession (e.g. 'ENCFF002JUR').

    Returns a stable-field record: format, output type/category, assay,
    assembly, parent dataset, biological replicates, file size, md5sums,
    run type, read length, lab, creation date, download href and uuid.
    """
    return _encode.get_file(accession)


@mcp.tool(annotations=READ_ONLY)
def encode_get_biosample(accession: str) -> dict:
    """Get one ENCODE biosample's metadata by accession (e.g. 'ENCBS013JZP').

    Returns a stable-field record: ontology term + classification, organism,
    summary/description, source, lot/product ids, treatments, culture dates,
    donor, lab, status and uuid.
    """
    return _encode.get_biosample(accession)






@mcp.tool(annotations=READ_ONLY)
def jaspar_get_matrix(matrix_id: str) -> dict:
    """Get one JASPAR TF binding profile by versioned matrix id (e.g. 'MA0002.2').

    Returns the full record including the position frequency matrix (PFM),
    TF name/class/family, species, data type, literature references and
    sequence logo URL. Requires a VERSIONED id ('MA0002.2', not 'MA0002') —
    use jaspar_matrix_versions to enumerate versions of a base id. Versioned
    matrices are immutable, so results are reproducible.
    """
    return jaspar.get_matrix(_jaspar_client, matrix_id)


@mcp.tool(annotations=READ_ONLY)
def jaspar_matrix_versions(base_id: str) -> dict:
    """List all versions of a JASPAR base matrix id (e.g. 'MA0002').

    Returns every released version with its matrix_id, name and URL —
    count-verified. Use this for reproducibility (pin an exact version before
    jaspar_get_matrix) or to track how a profile changed across releases.
    A versioned id ('MA0002.2') is accepted and reduced to its base.
    """
    return jaspar.matrix_versions(_jaspar_client, base_id)


@mcp.tool(annotations=READ_ONLY)
def jaspar_list_matrices(collection: str | None = None,
                         tax_group: str | None = None,
                         tax_id: int | None = None,
                         name: str | None = None,
                         search: str | None = None,
                         version: str | None = None,
                         max_rows: int = 1000) -> dict:
    """Search/list JASPAR TF binding profiles (the full profile catalog).

    Filters (all optional): collection ('CORE', 'UNVALIDATED', ...),
    tax_group ('vertebrates', 'plants', ...), tax_id (NCBI taxonomy id, e.g.
    9606 for human — this is how you filter by species; enumerate ids with
    jaspar_list_species), name (exact TF name, e.g. 'FOXA1'), search (free
    text), version='latest' (restrict to latest versions only; otherwise all
    versions are listed).

    The full filtered catalog is paginated through and count-verified:
    `count` is exact. At most max_rows summary rows (matrix_id, name,
    collection, sequence logo URL) are returned (`truncated` flags the cap).
    Use jaspar_get_matrix for the PFM of any hit.
    """



    reject_blank(collection=collection, tax_group=tax_group, name=name,
                 search=search, version=version)
    out = jaspar.list_matrices(
        _jaspar_client, collection=collection, tax_group=tax_group,
        tax_id=tax_id, name=name, search=search, version=version)
    rows = out["results"]
    kept = rows[:max_rows] if max_rows is not None else rows
    return {"count": out["count"], "returned": len(kept),
            "truncated": len(kept) < len(rows), "matrices": kept}


@mcp.tool(annotations=READ_ONLY)
def jaspar_list_species() -> dict:
    """List all species with JASPAR profiles (NCBI tax_id + name).

    Count-verified full listing. Use the tax_id values to filter
    jaspar_list_matrices (e.g. 9606 = Homo sapiens, 10090 = Mus musculus).
    """
    return jaspar.list_species(_jaspar_client)


@mcp.tool(annotations=READ_ONLY)
def jaspar_list_taxa() -> dict:
    """List all JASPAR taxonomic groups (vertebrates, plants, fungi, ...).

    Count-verified full listing. Use the group names as the tax_group filter
    of jaspar_list_matrices.
    """
    return jaspar.list_taxa(_jaspar_client)


@mcp.tool(annotations=READ_ONLY)
def jaspar_list_collections() -> dict:
    """List all JASPAR collections (CORE, UNVALIDATED, ...).

    Count-verified full listing. Use the collection names as the collection
    filter of jaspar_list_matrices (CORE = curated, non-redundant profiles).
    """
    return jaspar.list_collections(_jaspar_client)


@mcp.tool(annotations=READ_ONLY)
def jaspar_list_releases() -> dict:
    """List all JASPAR database releases (year, release number, active flag).

    Count-verified full listing. Record the active release when selecting
    motifs for reproducibility, or check release history before comparing
    results across JASPAR versions.
    """
    return jaspar.list_releases(_jaspar_client)






@mcp.tool(annotations=READ_ONLY)
def unibind_search_tfbs(tf_name: str | None = None,
                        cell_line: str | None = None,
                        species: str | None = None,
                        collection: str | None = None,
                        jaspar_id: str | None = None,
                        search: str | None = None,
                        max_rows: int = 200) -> dict:
    """Search UniBind ChIP-seq datasets with high-confidence TFBS predictions.

    UniBind (unibind.uio.no, 2021 release) maps direct TF-DNA interactions
    from ~10k public ChIP-seq datasets across 9 species. Each dataset is one
    (experiment, cell type, TF) triple carrying per-JASPAR-model TFBS calls.

    Filters (all optional, combined with AND; exact-match unless noted):
      * tf_name — TF gene symbol as used by UniBind (e.g. 'CTCF', 'FOXA1').
      * cell_line — UniBind cell/tissue title (e.g. 'K562 (myelogenous
        leukemia)'); these are verbose — prefer `search` for fuzzy matching.
      * species — scientific name, one of the 9 UniBind species (e.g.
        'Homo sapiens', 'Mus musculus').
      * collection — 'Robust' (TFBS supported by the best model; use this
        for high confidence) or 'Permissive'.
      * jaspar_id — versioned JASPAR matrix (e.g. 'MA0139.1').
      * search — free text matched across dataset fields.

    `total` is the API's own exact count; at most max_rows rows are returned
    (a stable prefix — `truncated` flags the cap). Rows carry tf_id (the
    dataset key for unibind_get_dataset), tf_name, parsed identifier +
    cell_line, and the dataset's ChIP-seq peak count (total_peaks — NOT the
    TFBS count; per-model TFBS counts live in unibind_get_dataset).
    """


    reject_blank(tf_name=tf_name, cell_line=cell_line, species=species,
                 collection=collection, jaspar_id=jaspar_id, search=search)
    return unibind.search_datasets(
        _unibind_client, tf_name=tf_name, cell_line=cell_line,
        species=species, collection=collection, jaspar_id=jaspar_id,
        search=search, max_rows=max_rows)


@mcp.tool(annotations=READ_ONLY)
def unibind_get_dataset(tf_id: str) -> dict:
    """Get one UniBind dataset's detail: per-model TFBS counts + file URLs.

    Args:
        tf_id: dataset key '<identifier>.<cell_line>.<TF>' as returned by
            unibind_search_tfbs (e.g.
            'ENCSR000AUE.A549_lung_carcinoma.CTCF').

    Returns the dataset's TF name, source identifiers (ENCODE/GEO/GTRD),
    cell lines, biological conditions, JASPAR matrix ids, ChIP-seq peak
    count, and one row per TFBS prediction model (DAMO/PWM/...) with
    total_tfbs, score/distance thresholds, adjusted CentriMo enrichment
    p-value, and direct BED/FASTA download URLs for the full site list —
    use those URLs (not an MCP call) to retrieve a dataset's complete TFBSs.
    """
    return unibind.get_dataset(_unibind_client, tf_id)


@mcp.tool(annotations=READ_ONLY)
def unibind_tfbs_in_region(genome: str, chrom: str, start: int, end: int,
                           tf_name: str | None = None,
                           collection: str = "Robust",
                           max_sites: int = 2000) -> dict:
    """TF binding sites overlapping a genomic region (UniBind 2021 maps).

    Served via the UCSC hubApi against UniBind's registered public track
    hubs (UniBind's own REST API has no region endpoint). Coordinates are
    0-based half-open on the hub assemblies.

    Args:
        genome: UCSC assembly — Robust hub: hg38, mm10, ce11, dm6, danRer11,
            sacCer3, rn6, araTha1; Permissive additionally spo2. (No hg19 —
            lift coordinates first.)
        chrom: chromosome with 'chr' prefix (e.g. 'chr1').
        start/end: interval (end - start <= 1,000,000 bp).
        tf_name: optional TF symbol filter (e.g. 'CTCF'), applied to the
            sites AFTER the region scan.
        collection: 'Robust' (default; TFBSs supported by the best model) or
            'Permissive'.
        max_sites: cap on returned site rows.

    HONEST-CAP SEMANTICS: at most 20,000 items are scanned from the track
    per call; `region_scan_complete=false` means the region has more sites
    than were scanned (narrow the window — a TF-dense 10 kb promoter region
    already holds ~2,300 sites) and, with tf_name set, matches may be
    missing. `n_matching` counts scanned sites passing the filter;
    `returned`/`truncated` describe the max_sites cap. Each site:
    chrom/start/end/strand + dataset, cell_line, tf_name, jaspar_matrix
    parsed from the track item name.
    """
    return unibind.tfbs_in_region(
        _ucsc_client, genome=genome, chrom=chrom, start=start, end=end,
        tf_name=tf_name, collection=collection, max_sites=max_sites)


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
