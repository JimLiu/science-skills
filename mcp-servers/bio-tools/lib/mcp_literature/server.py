"""mcp-literature server — scholarly literature retrieval over OpenAlex +
arXiv.

Tier-2 domain server: citation graph, open-access status, author/venue
metrics (OpenAlex) and preprints beyond biology (arXiv). Complements —
does not duplicate — the pubmed and biorxiv domains: use those for
MEDLINE records and bioRxiv/medRxiv preprints; use this one to walk
citations/references, check OA status and venues, profile authors, and
reach physics/CS/stats preprints.

Retrieval is done by the fleet packages ``openalex-works`` (paced
<= 2 req/s; authenticates every request with the user's OpenAlex
``api_key`` — OpenAlex has no anonymous access, so a missing key
surfaces as a structured ``openalex_key_required`` tool result) and
``arxiv-fetch`` (paced >= 3 s between requests per the arXiv ToS); this
layer is marshalling only.
Every listing is honest: ``api_total`` carries the upstream's own count
and ``records_truncated`` flags any cap.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers_common.guarded import GuardedFastMCP
from mcp_servers_common.criteria import blank, none_if_blank, reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp_servers_common.ua import OpenAlexKeyRequired
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)



mcp = GuardedFastMCP("mcp-literature")





def _openalex_key_result(e: OpenAlexKeyRequired) -> dict:
    return {"error": "openalex_key_required", "message": str(e)}




@lru_cache(maxsize=1)
def _openalex():
    from openalex_works import OpenAlexWorks
    return OpenAlexWorks()


@lru_cache(maxsize=1)
def _arxiv():
    from arxiv_fetch import ArxivFetch
    return ArxivFetch()







@mcp.tool(annotations=READ_ONLY, blank_as_absent={"query"})
def openalex_search_works(query: str | None = None,
                          year_from: int | None = None,
                          year_to: int | None = None,
                          work_type: str | None = None,
                          open_access_only: bool = False,
                          venue: str | None = None,
                          sort: str = "relevance",
                          max_records: int = 50,
                          include_abstracts: bool = False) -> dict:
    """Search OpenAlex scholarly works (all disciplines, ~250M records)
    with year/type/OA/venue filters.

    Args:
        query: free-text search over title + abstract + full text, e.g.
            ``"prime editing off-target"``. Optional if at least one
            filter is set.
        year_from: earliest publication year (inclusive), e.g. ``2020``.
        year_to: latest publication year (inclusive).
        work_type: OpenAlex type filter — common values ``article``,
            ``review``, ``preprint``, ``book-chapter``, ``dataset``,
            ``dissertation``.
        open_access_only: if true, only works with a free-to-read copy.
        venue: restrict to one journal/repository — OpenAlex S-id
            (``S106963461``), openalex.org URL, ISSN (``1087-0156``), or a
            plain venue name (``"Nature Biotechnology"``; resolved to the
            top sources-index hit, surfaced in ``venue_resolved`` so the
            choice is visible — pass an exact ID to skip resolution).
        sort: ``relevance`` (default; needs a query, otherwise OpenAlex
            native order), ``cited_by_count``, or ``publication_date``.
        max_records: cap on returned records (default 50, hard ceiling
            500; pages of 200 are walked politely).
        include_abstracts: if true, each record carries ``abstract``
            (reconstructed from OpenAlex's inverted index — but ONLY for
            works whose declared license verifies open (cc-by/cc-by-sa/
            cc0/public-domain); -nc, -nd, and undeclared licenses get
            ``abstract=null`` plus an ``abstract_policy`` note with a
            link-out, and ``abstract_license`` carries the declared
            license. Null also where the publisher withholds the index
            entirely). Adds bulk — keep false for scans.

    Returns ``{query, filters, sort, api_total, n_records_returned,
    records_truncated, records}``. ``api_total`` is OpenAlex's own total
    match count — ``records_truncated=true`` means more matches exist
    than were returned (tighten filters or raise max_records). Each
    record: ``{openalex_id, doi, pmid, title, publication_year/date,
    type, language, is_retracted, authors[{author_id, name, orcid,
    position, is_corresponding, institutions}], source{source_id,
    display_name, issn_l, type}, biblio, cited_by_count, fwci,
    referenced_works_count, open_access{is_oa, oa_status, oa_url},
    best_oa_pdf_url, primary_topic, keywords}``.
    """










    query = none_if_blank(query)
    reject_blank(venue=venue)
    try:
        return _openalex().search_works(
            query=query, year_from=year_from, year_to=year_to,
            work_type=work_type, open_access_only=open_access_only,
            venue=venue, sort=sort, max_records=max_records,
            include_abstracts=include_abstracts)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)


@mcp.tool(annotations=READ_ONLY)
def openalex_get_work(work_id: str) -> dict:
    """Fetch one OpenAlex work in full — metadata, abstract (reconstructed
    from the inverted index), OA locations, and the complete outgoing
    reference ID list.

    Args:
        work_id: OpenAlex W-id (``W2981137429``), openalex.org URL, bare
            DOI (``10.1038/s41586-019-1711-4``) or doi.org URL.

    Returns the lean work record (see ``openalex_search_works``) plus
    ``abstract`` (reconstructed ONLY when the work's declared license
    verifies open — cc-by/cc-by-sa/cc0/public-domain; otherwise null with
    an ``abstract_policy`` link-out note and the declared license in
    ``abstract_license``; also null when the publisher withholds the
    inverted index entirely), ``referenced_works`` (outgoing reference
    W-ids, use ``openalex_references`` to hydrate them) and
    ``counts_by_year`` (citations per year, last decade). DOI lookups
    resolve via OpenAlex's claimant filter — upstream sometimes tags
    several works with one DOI; the most-cited claimant is selected and,
    when more than one exists, ``doi_claimants`` (all of them, with
    citation counts) + ``doi_resolution_note`` are included so the choice
    is visible. Raises a not-found error for unknown IDs/DOIs.
    """
    try:
        return _openalex().get_work(work_id)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)


@mcp.tool(annotations=READ_ONLY)
def openalex_citations(work_id: str, sort: str = "cited_by_count",
                       max_records: int = 50,
                       include_abstracts: bool = False) -> dict:
    """List works that CITE a given work (incoming citations), via
    OpenAlex's citation graph.

    Args:
        work_id: OpenAlex W-id, openalex.org URL, or DOI of the cited
            work (DOIs cost one extra resolution request).
        sort: ``cited_by_count`` (default — most influential citers
            first), ``publication_date`` (newest first), or ``relevance``
            (OpenAlex native order here).
        max_records: cap on returned citing works (default 50, ceiling
            500). Heavily-cited papers have tens of thousands of citers —
            check ``api_total`` and ``records_truncated``.
        include_abstracts: include reconstructed abstracts per record
            (bulky; default false; license-gated — see
            ``openalex_search_works``: only verified-open licenses
            reconstruct, others carry ``abstract_policy``).

    Returns ``{work_id, api_total, n_records_returned, records_truncated,
    records}`` — ``api_total`` is the true citing-work count (OpenAlex's
    ``cited_by_count`` for the work); records are lean work records. DOI
    inputs resolve to the most-cited claimant when upstream has duplicate
    DOI tags (``doi_claimants`` + ``doi_resolution_note`` appear in the
    output when that happens — see ``openalex_get_work``).
    """
    try:
        return _openalex().citations(work_id, sort=sort,
                                     max_records=max_records,
                                     include_abstracts=include_abstracts)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)


@mcp.tool(annotations=READ_ONLY)
def openalex_references(work_id: str, max_records: int = 100) -> dict:
    """List the works a given work CITES (outgoing references), hydrated
    to full metadata in reference-list order.

    Args:
        work_id: OpenAlex W-id, openalex.org URL, or DOI.
        max_records: cap on references hydrated to full records (default
            100, ceiling 500; hydration is batched 50 per request). The
            complete un-hydrated ID list is always returned.

    Returns ``{work_id, n_references (true outgoing-reference count),
    n_records_returned, records_truncated, references_not_hydrated,
    reference_ids (ALL outgoing W-ids), records}``. Records are lean work
    records (no abstracts) in the order the source work lists them.
    References OpenAlex has no standalone record for (merged/deleted
    works) cannot be hydrated — their IDs are listed explicitly in
    ``references_not_hydrated``, never silently dropped.
    """
    try:
        return _openalex().references(work_id, max_records=max_records)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)


@mcp.tool(annotations=READ_ONLY)
def openalex_search_authors(query: str, max_records: int = 25) -> dict:
    """Search OpenAlex author profiles by name.

    Args:
        query: author name, e.g. ``"David Liu"`` — matches display name
            and alternatives. Expect homonyms: check affiliations, topics
            and ORCID before trusting a match.
        max_records: cap on returned profiles (default 25, ceiling 500).

    Returns ``{query, api_total, n_records_returned, records_truncated,
    records}``; each record: ``{author_id, name, orcid, works_count,
    cited_by_count, h_index, i10_index, affiliations[{institution,
    years}], last_known_institutions, top_topics}``. Use the
    ``author_id`` with ``openalex_get_author`` for a works sample.
    """



    if blank(query):
        raise ValueError("query must be non-empty")
    try:
        return _openalex().search_authors(query, max_records=max_records)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)


@mcp.tool(annotations=READ_ONLY)
def openalex_get_author(author_id: str, works_sample: int = 10) -> dict:
    """Fetch one OpenAlex author profile plus their top-cited works.

    Args:
        author_id: OpenAlex A-id (``A5065535610``), openalex.org URL, or
            ORCID (``0000-0002-9943-7557`` or orcid.org URL). CAVEAT:
            OpenAlex's ORCID pointer sometimes resolves to a sparse
            duplicate profile — prefer the A-id of the right profile
            from ``openalex_search_authors`` (check works_count).
        works_sample: number of the author's most-cited works to attach
            (default 10, max 200; 0 skips the extra request).

    Returns the author record (see ``openalex_search_authors``) plus
    ``counts_by_year`` (works + citations per year), ``top_works_total``
    (the author's true total works count per the works index) and
    ``top_works`` (lean work records sorted by citations, no abstracts).
    """
    try:
        return _openalex().get_author(author_id, works_sample=works_sample)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)


@mcp.tool(annotations=READ_ONLY)
def openalex_venue_info(venue: str, max_records: int = 10) -> dict:
    """Look up journals/repositories ("sources") in OpenAlex — OA status,
    DOAJ listing, APC, citation metrics.

    Args:
        venue: an exact identifier — OpenAlex S-id (``S106963461``),
            openalex.org URL, or ISSN (``1087-0156``) — for a single
            record; anything else is treated as a name search (e.g.
            ``"Nature Biotechnology"``).
        max_records: cap for the name-search path (default 10, ceiling
            500). Ignored for exact-identifier lookups.

    Returns: exact lookup -> one source record + ``counts_by_year``;
    name search -> ``{query, api_total, n_records_returned,
    records_truncated, records}``. Source record: ``{source_id,
    display_name, type (journal/repository/conference), issn_l, issn,
    host_organization, country_code, homepage_url, is_oa, is_in_doaj,
    is_core, apc_usd, works_count, cited_by_count, h_index,
    two_year_mean_citedness, first/last_publication_year, top_topics}``.
    """


    if blank(venue):
        raise ValueError("venue must be non-empty")
    tool = _openalex()
    from openalex_works import normalize_source_id
    try:
        normalize_source_id(venue)
    except ValueError:





        v = venue.strip().lower()
        if v.startswith("http") and "openalex.org/" in v:
            raise ValueError(
                f"{venue!r} is an OpenAlex URL but not a source (S) id; "
                "pass an S-id, an ISSN, or a journal name")
        try:
            return tool.search_sources(venue, max_records=max_records)
        except OpenAlexKeyRequired as e:
            return _openalex_key_result(e)
    try:
        return tool.get_source(venue)
    except OpenAlexKeyRequired as e:
        return _openalex_key_result(e)






@mcp.tool(annotations=READ_ONLY, blank_as_absent={"query"})
def arxiv_search(query: str | None = None, category: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 start: int = 0, max_results: int = 25,
                 sort_by: str = "relevance",
                 sort_order: str = "descending") -> dict:
    """Search arXiv preprints (physics, math, CS, stats, q-bio, ...) via
    the official Atom API.

    Args:
        query: arXiv query string. Plain terms search all fields; field
            prefixes ``ti:`` (title), ``au:`` (author), ``abs:``
            (abstract) and booleans ``AND``/``OR``/``ANDNOT`` work, e.g.
            ``ti:"protein language model" AND abs:structure``. Optional
            if category or a date range is set.
        category: arXiv category code to AND in, e.g. ``q-bio.GN``,
            ``q-bio.PE``, ``cs.LG``, ``stat.ML``.
        date_from: earliest submission date, ``YYYY-MM-DD`` (inclusive).
        date_to: latest submission date, ``YYYY-MM-DD`` (inclusive).
        start: result offset for paging (0-based). The API paces 3 s
            between requests — page politely, don't hammer.
        max_results: page size (default 25, max 100 per call).
        sort_by: ``relevance`` (default), ``submittedDate``, or
            ``lastUpdatedDate``.
        sort_order: ``descending`` (default) or ``ascending``.

    Returns ``{search_query (the exact query sent), api_total (arXiv's
    own total match count), start_index, n_records_returned,
    records_truncated, sort_by, sort_order, records}``. Each record:
    ``{arxiv_id, version, id_versioned, title, abstract, authors,
    published, updated, primary_category, categories, doi, journal_ref,
    comment, abs_url, pdf_url}``. ``doi``/``journal_ref`` are present
    only after journal publication. Malformed queries raise an error
    (arXiv's HTTP-200 error feed is detected, never returned as data).
    """







    query = none_if_blank(query)
    reject_blank(category=category)
    return _arxiv().search(query=query, category=category,
                           date_from=date_from, date_to=date_to,
                           start=start, max_results=max_results,
                           sort_by=sort_by, sort_order=sort_order)


@mcp.tool(annotations=READ_ONLY)
def arxiv_get_papers(arxiv_ids: list[str]) -> dict:
    """Batch-fetch arXiv paper metadata (incl. abstracts) by ID — one
    paced request for up to 100 papers.

    Args:
        arxiv_ids: up to 100 arXiv IDs in any common form —
            ``2103.14030``, versioned ``2103.14030v2``, old-style
            ``q-bio/0601001``, ``arXiv:`` prefixed, or abs/pdf URLs.
            Unversioned IDs resolve to the latest version.

    Returns ``{n_requested, n_found, duplicates, not_found, records}`` —
    ``duplicates`` lists inputs that resolved to an already-returned paper
    (e.g. bare + versioned spellings of one id); records in
    the requested order, same shape as ``arxiv_search`` records. Unknown
    AND malformed IDs are listed in ``not_found`` (the API silently skips
    unknowns and rejects whole batches over malformed ones; this tool
    does neither). Withdrawn papers still return metadata — check
    ``comment`` for withdrawal notes.
    """
    return _arxiv().get_papers(arxiv_ids)


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
