"""mcp-omics-archives server — omics data archives (expression, metabolomics,
metagenomics, proteomics).

Clean tier-2 schemas over five accuracy-gated fleet packages:

* ``arrayexpress-experiments`` — ArrayExpress functional-genomics experiments
  (BioStudies API: totalHits-verified search, flattened records, files, SDRF samples)
* ``geo-meta``                 — NCBI GEO series/sample metadata via E-utilities
  (db=gds) + targeted SOFT headers; never downloads data tables
* ``metabolights-meta``        — MetaboLights study metadata (parsed ISA payloads)
* ``mgnify-studies``           — MGnify metagenomics studies/analyses
  (count-verified JSON:API pagination)
* ``pride-projects``           — PRIDE Archive proteomics projects
  (api_total-verified search, normalized records)

This layer is marshalling only: retrieval, pacing (<=2 req/s per EBI host,
NCBI E-utilities etiquette), retries and count verification live in the
fleet packages.
"""

from __future__ import annotations

import re
import urllib.parse
from functools import lru_cache
from typing import Any

import httpx
from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import none_if_blank, reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)


from arrayexpress_experiments import (
    SearchSpec,
    fetch_experiment,
    get_experiment_files,
    get_experiment_samples,
    search_experiments,
)
from geo_meta import fetch_series_batch, search_series
from metabolights_meta import (
    MetaboLightsNotFoundError,
    extract_study_metadata,
    get_study_files,
    list_public_studies,
    search_data_files,
)
from mgnify_studies import fetch_studies, fetch_study_analyses, search_studies
from pride_projects import (
    fetch_project,
    find_projects_for_protein,
    search_project_proteins,
    search_projects,
)

from . import marshal



mcp = GuardedFastMCP("mcp-omics-archives")



@lru_cache(maxsize=1)
def _ae_client():
    from arrayexpress_experiments import BioStudiesClient
    return BioStudiesClient()


@lru_cache(maxsize=1)
def _geo_client():
    from geo_meta import PoliteClient
    return PoliteClient()


@lru_cache(maxsize=1)
def _mtbls_client():
    from metabolights_meta import MetaboLightsClient
    return MetaboLightsClient()


@lru_cache(maxsize=1)
def _mgnify_client():
    from mgnify_studies import MGnifyClient
    return MGnifyClient()


@lru_cache(maxsize=1)
def _pride_client():
    from pride_projects import PrideClient
    return PrideClient()


def _cap(records: list, max_returned: int | None) -> tuple[list, bool]:
    """Slice an already-complete record list for output; never affects retrieval."""
    if max_returned is not None and max_returned >= 0 and len(records) > max_returned:
        return records[:max_returned], True
    return records, False





@mcp.tool(annotations=READ_ONLY)
def arrayexpress_search_experiments(
    query: str | None = None,
    organism: str | None = None,
    study_type: str | None = None,
    technology: str | None = None,
    released_after: str | None = None,
    released_before: str | None = None,
    extra_facets: dict[str, str] | None = None,
    max_records: int | None = 50,
) -> dict[str, Any]:
    """Search ArrayExpress functional-genomics experiments (complete retrieval).

    Filters combine (AND): `query` free text (BioStudies/Lucene syntax),
    `organism` (e.g. 'Homo sapiens'), `study_type` (e.g. 'RNA-seq of coding
    RNA from single cells', 'ChIP-seq', 'transcription profiling by array'),
    `technology` (e.g. 'sequencing assay', 'array assay'), release-date range
    (inclusive ISO dates), and arbitrary extra facet=value pairs. Facet values
    are matched case-insensitively.

    Every result page is walked and the unique-record count is verified against
    the API's totalHits (total_hits in the output; a mismatch raises rather
    than silently truncating). `max_records` (default 50; raise for broader
    scans) stops the walk early for large result sets — then truncated=true
    and total_hits still reports the full count. Records are compact
    (accession, title, release_date, files, links, is_public) in deterministic
    release-date order; fetch detail with arrayexpress_get_experiment.
    """


    reject_blank(extra_facets, query=query, organism=organism,
                 study_type=study_type, technology=technology)
    spec = SearchSpec(
        query=query,
        organism=organism,
        study_type=study_type,
        technology=technology,
        released_after=released_after,
        released_before=released_before,
        extra_facets=extra_facets or {},
    )
    return search_experiments(spec, client=_ae_client(), max_records=max_records)


@mcp.tool(annotations=READ_ONLY)
def arrayexpress_get_experiment(accession: str) -> dict[str, Any]:
    """Fetch one ArrayExpress experiment as a flattened record.

    `accession` is e.g. 'E-MTAB-5061'. The deeply nested BioStudies submission
    JSON is flattened to the fields a functional-genomics analyst needs:
    accession, title, release date, study type, organisms, description, assay
    count, sample count, technology, assay-by-molecule, experimental
    designs/factors, authors + affiliations, publications (with DOI), protocol
    count/types, array designs, file count, files-by-type summary, total file
    bytes, and link targets. Use arrayexpress_get_experiment_files for the
    per-file inventory and arrayexpress_get_experiment_samples for SDRF rows.
    """
    return fetch_experiment(accession, client=_ae_client())


@mcp.tool(annotations=READ_ONLY)
def arrayexpress_get_experiment_files(accession: str) -> dict[str, Any]:
    """List every file of an ArrayExpress experiment with download endpoints.

    Per-file records (name, size in bytes, type, format, description) from the
    authoritative submission JSON, plus the HTTP/FTP download base links and
    the /info endpoint's independent file count (carried alongside so
    disagreements are visible).
    """
    return get_experiment_files(accession, client=_ae_client())


@mcp.tool(annotations=READ_ONLY)
def arrayexpress_get_experiment_samples(
    accession: str,
    max_rows_returned: int = 200,
) -> dict[str, Any]:
    """Fetch per-sample SDRF annotation rows for an ArrayExpress experiment.

    Locates the experiment's SDRF file, downloads and parses every row (sample
    name, characteristics[...], factor value[...], assay/data-file columns —
    headers exactly as in the MAGE-TAB, repeated headers suffixed #2/#3).
    Experiments without an SDRF (some sequencing submissions keep sample
    metadata in ENA only) return {"error": "no_sdrf"}.

    All rows are parsed and n_samples reports the true total; the output lists
    at most max_rows_returned rows (rows_truncated=true when capped — single-
    cell experiments can have thousands).
    """
    out = get_experiment_samples(accession, client=_ae_client())
    if "samples" in out:
        rows, truncated = _cap(out["samples"], max_rows_returned)
        out["samples"] = rows
        out["n_samples_returned"] = len(rows)
        out["rows_truncated"] = truncated
    return out





@mcp.tool(annotations=READ_ONLY)
def geo_search_series(term: str, retmax: int = 500) -> dict[str, Any]:
    """Search NCBI GEO DataSets (db=gds) and return series-level records.

    `term` is full E-utilities syntax, e.g.
    '"single cell rna seq"[All Fields] AND "Homo sapiens"[Organism] AND gse[ETYP]
    AND 2021/01/01:2021/12/31[PDAT]'. Add gse[ETYP] to restrict to series.

    Returns count (esearch's own total — may exceed the number of returned
    records when it is larger than retmax) and records: trimmed esummary docs
    sorted by accession (accession, title, summary, organism/taxon, GDS type,
    n_samples, publication date, platform, sample accessions+titles, FTP link,
    BioProject, PubMed ids). Call geo_get_series for full per-sample
    characteristics. Requests go through NCBI E-utilities at a polite pace.
    """



    reject_blank(term=term)
    return search_series(term, client=_geo_client(), retmax=retmax)


@mcp.tool(annotations=READ_ONLY)
def geo_get_series(accessions: list[str]) -> dict[str, Any]:
    """Fetch structured metadata for GEO series (GSE accessions), samples included.

    Per series: title, summary, overall design, organism/taxon, GDS type,
    platform IDs, publication date, BioProject, PubMed ids, series-level
    supplementary file URLs, and every sample (accession, title, organism,
    characteristics as tag/value pairs, library strategy/source/selection,
    instrument, per-sample supplementary file URLs). This covers both
    series-level and sample-level questions in one call.

    Sources: one esearch + one esummary for the batch, then two targeted SOFT
    *header* requests per series (2 + 2·N requests, NCBI-paced). Data tables
    and platform annotation tables are never downloaded. Large single-cell
    series (hundreds of samples) produce large records — batch accordingly.
    """
    records = fetch_series_batch(accessions, client=_geo_client())
    return {"n_requested": len(accessions), "records": records}





@mcp.tool(annotations=READ_ONLY)
def metabolights_list_studies() -> dict[str, Any]:
    """List every public MetaboLights study accession.

    Returns the de-duplicated, numerically sorted accession list (MTBLS1,
    MTBLS2, ...) plus count and the API's own reported_count (these agree or
    something is wrong upstream). NOTE: the old connector's
    `metabolights_search_studies` was a documented no-op — upstream ignored the
    query parameter and returned this same listing; there is no server-side
    study search. To find studies by topic, fetch candidates with
    metabolights_get_studies and filter on their titles/descriptors, or search
    a cross-archive index instead.
    """
    return list_public_studies(client=_mtbls_client())


_MTBLS_NUM = re.compile(r"(\d+)")


def _mtbls_sort_key(acc: str) -> tuple:
    m = _MTBLS_NUM.search(acc)
    return (int(m.group(1)) if m else 1 << 60, acc)


@mcp.tool(annotations=READ_ONLY)
def metabolights_get_studies(
    accessions: list[str],
    include_samples: bool = False,
    max_sample_rows_returned: int = 200,
) -> dict[str, Any]:
    """Fetch structured metadata for MetaboLights studies (MTBLSxxx accessions).

    Per study (from the parsed ISA payload): title, description, status,
    release/submission year, organisms (+ organism parts), assays (measurement
    / technology / platform per assay sheet), study design factors, design
    descriptors (ontology terms), sample count, and protocols (name +
    description: sample collection, extraction, chromatography, mass
    spectrometry, NMR, data transformation, metabolite identification...).

    With include_samples=true the parsed sample table is attached per study
    (header-keyed rows: source name, characteristics, factor values;
    n_rows_total always reports the true size, output capped at
    max_sample_rows_returned rows with rows_truncated flag). Private or
    unknown accessions are listed in not_found. Output is sorted by numeric
    accession; duplicates are de-duplicated.
    """
    client = _mtbls_client()
    unique = sorted({a.strip().upper() for a in accessions if a.strip()},
                    key=_mtbls_sort_key)
    records: list[dict[str, Any]] = []
    not_found: list[str] = []
    for acc in unique:
        try:
            payload = client.get_json(
                f"studies/public/study/{urllib.parse.quote(acc, safe='')}")
        except MetaboLightsNotFoundError:
            not_found.append(acc)
            continue
        record = extract_study_metadata(payload)
        content = payload.get("content") or {}
        record["protocols"] = marshal.metabolights_protocols(content)
        if include_samples:
            record["sample_table"] = marshal.metabolights_sample_table(
                content.get("sampleTable"), max_rows=max_sample_rows_returned)
        records.append(record)
    return {"n_requested": len(unique), "records": records, "not_found": not_found}


@mcp.tool(annotations=READ_ONLY)
def metabolights_get_study_files(
    accession: str,
    include_data_files: bool = True,
) -> dict[str, Any]:
    """Complete file inventory for a public MetaboLights study.

    study_folder lists the top-level study directory (ISA-Tab metadata files
    i_/s_/a_*.txt, MAF metabolite tables m_*.tsv, and folder entries);
    data_files recursively lists the FILES raw-data folder (set
    include_data_files=false to skip that second, sometimes slow request).
    Deterministically sorted; volatile timestamps dropped.
    """
    return get_study_files(accession, include_data_files=include_data_files,
                           client=_mtbls_client())


@mcp.tool(annotations=READ_ONLY)
def metabolights_search_data_files(
    accession: str,
    pattern: str | None = None,
) -> dict[str, Any]:
    """Glob search over a MetaboLights study's raw-data folder (FILES tree).

    `pattern` is a filename glob applied server-side and re-verified
    client-side, e.g. '*.mzML', '*.raw', '*.zip'; omit it to list every data
    file. Results are relative paths under the study folder ('FILES/...'),
    sorted.
    """
    return search_data_files(accession, pattern=pattern, client=_mtbls_client())








@mcp.tool(annotations=READ_ONLY, blank_as_absent={"query", "biome_lineage"})
def mgnify_search_studies(
    query: str | None = None,
    biome_lineage: str | None = None,
) -> dict[str, Any]:
    """Find MGnify metagenomics studies by free text OR biome lineage.

    Provide exactly one of: `query` — free-text search (e.g. 'coral',
    'wastewater sludge'); `biome_lineage` — a GOLD-style lineage (e.g.
    'root:Host-associated:Human:Digestive system:Large intestine' or
    'root:Engineered:Wastewater'), which includes all sub-lineages.

    The full listing is paginated to completion and verified against the API's
    own pagination count (count == len(records), or the call fails loudly).
    Records: MGYS accession, study name, abstract, biome lineages, sample
    count, centre name, ENA/BioProject accessions. Use mgnify_get_studies /
    mgnify_get_study_analyses for detail.
    """




    query = none_if_blank(query)
    biome_lineage = none_if_blank(biome_lineage)
    if (query is None) == (biome_lineage is None):
        raise ValueError("provide exactly one of 'query' or 'biome_lineage'")
    spec = ({"type": "search", "query": query} if query is not None
            else {"type": "biome", "lineage": biome_lineage})
    return search_studies(spec, client=_mgnify_client())


@mcp.tool(annotations=READ_ONLY)
def mgnify_get_studies(
    accessions: list[str],
    include_analyses: bool = False,
) -> dict[str, Any]:
    """Fetch structured records for MGnify studies (MGYS accessions).

    Per study: accession, ENA secondary accession (ERP/SRP), BioProject, study
    name, abstract, biome lineage(s), sample count, centre name, data
    origination, last update. With include_analyses=true each study
    additionally carries its complete analyses listing (count-verified
    pagination) plus analysis-count breakdowns by pipeline version and by
    experiment type — one extra paginated sweep per study. Unknown accessions
    are reported in missing, never dropped.
    """
    return fetch_studies(accessions, client=_mgnify_client(),
                         include_analyses=include_analyses)


@mcp.tool(annotations=READ_ONLY)
def mgnify_get_study_analyses(accession: str) -> dict[str, Any]:
    """List ALL analyses of one MGnify study (complete pagination).

    `accession` is an MGYS study accession. Returns analyses_count (the API's
    own total — retrieval is verified against it) and one record per MGYA
    analysis: pipeline version, experiment type (amplicon / metagenomic /
    assembly / ...), status, and the run / assembly / sample accessions it was
    computed from, sorted by MGYA accession.
    """
    return fetch_study_analyses(accession, client=_mgnify_client())





@mcp.tool(annotations=READ_ONLY)
def pride_search_projects(
    keyword: str | None = None,
    organism: str | None = None,
    instrument: str | None = None,
    disease: str | None = None,
    extra_filters: dict[str, str] | None = None,
    max_records_returned: int = 50,
) -> dict[str, Any]:
    """Search PRIDE Archive proteomics projects (complete, verified retrieval).

    Filters combine (AND): `keyword` free text (e.g. 'phosphoproteome',
    'single-cell proteomics'); `organism` / `instrument` / `disease` take the
    exact PRIDE facet strings (e.g. 'Homo sapiens (human)', 'Orbitrap Fusion
    Lumos', 'Covid-19'); extra_filters adds arbitrary field==value filters
    (e.g. {"experimentTypes": "Shotgun proteomics"}).

    The walk is bounded by `max_records_returned` (default 50; raise for
    broader scans — upstream sorts by accession ASC, so results are a stable
    prefix of the full set); api_total always carries the API's own true
    total and records_truncated flags a capped fetch — silent truncation is
    impossible. When the walk completes within the cap, the unique-project
    count is verified against api_total (a mismatch raises). Records are
    normalized (accession, title, organisms, organism parts, diseases,
    instruments, experiment types, quantification methods,
    submission/publication dates, submitters, lab PIs, references with PubMed
    id + DOI), sorted by accession. Output lists at most max_records_returned
    records (records_truncated=true when capped; api_total always carries the
    true count).
    """



    reject_blank(extra_filters, keyword=keyword, organism=organism,
                 instrument=instrument, disease=disease)
    spec: dict[str, Any] = {}
    if keyword:
        spec["keyword"] = keyword
    if organism:
        spec["organism"] = organism
    if instrument:
        spec["instrument"] = instrument
    if disease:
        spec["disease"] = disease
    if extra_filters:
        spec["extra_filters"] = extra_filters




    max_pages = max(1, -(-max_records_returned // 100))
    result = search_projects(_pride_client(), spec, max_pages=max_pages)
    complete = result.get("complete", True)
    records, capped = _cap(result.get("records", []), max_records_returned)
    result["records"] = records
    result["n_records_returned"] = len(records)
    result["records_truncated"] = capped or not complete
    return result


@mcp.tool(annotations=READ_ONLY)
def pride_get_projects(accessions: list[str]) -> dict[str, Any]:
    """Fetch full metadata for PRIDE projects by accession (e.g. 'PXD010154').

    Per project: title, description, sample/data processing protocols,
    organisms, organism parts, diseases, instruments, experiment types,
    quantification methods, submission/publication dates, submitters, lab
    heads, keywords, and references (PubMed id + DOI) — the same normalized
    record shape pride_search_projects returns, so the two are directly
    comparable. Unknown accessions are listed in not_found. Output is sorted
    by accession.
    """
    client = _pride_client()
    records: list[dict[str, Any]] = []
    not_found: list[str] = []
    for acc in sorted({a.strip() for a in accessions if a.strip()}):
        try:
            records.append(fetch_project(client, acc))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                not_found.append(acc)
            else:
                raise
    return {"n_requested": len(records) + len(not_found),
            "records": records, "not_found": not_found}


@mcp.tool(annotations=READ_ONLY)
def pride_search_project_proteins(
    project_accession: str,
    keyword: str | None = None,
) -> dict[str, Any]:
    """List protein evidence rows for one PRIDE affinity-proteomics project.

    Pages through the PRIDE affinity-proteomics protein index for
    `project_accession` until exhausted; rows (protein accession, gene symbol,
    protein name, per-project evidence fields) are sorted by protein
    accession. `keyword` filters server-side (matches accession, gene symbol,
    or protein name).

    NOTE: this index only serves affinity-proteomics projects. For classic
    mass-spec PXD submissions, per-project protein listings are not served by
    PRIDE — use pride_find_projects_for_protein for the protein→projects
    direction instead.
    """




    reject_blank(keyword=keyword)
    return search_project_proteins(project_accession, keyword=keyword,
                                   client=_pride_client())


@mcp.tool(annotations=READ_ONLY)
def pride_find_projects_for_protein(protein_accession: str) -> dict[str, Any]:
    """Find PRIDE projects containing a protein (MS-archive direction).

    `protein_accession` is a UniProt accession (e.g. 'P04637'). Returns the
    projects whose identification results include the protein, with sorted
    project accession lists — the complementary route for classic mass-spec
    (PXD) projects where per-project protein listings are not served. Feed the
    accessions to pride_get_projects for full metadata.
    """
    return find_projects_for_protein(protein_accession, client=_pride_client())


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
