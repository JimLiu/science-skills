"""mcp-research-resources server — FastMCP tools over grants-gov-search + antibody-registry.

Retrieval is delegated to the fleet packages; this module is marshalling only.
Grants.gov search2 is POST-only (GET returns 403) — the fleet client handles
that. The Antibody Registry has an anonymous depth cap: rows beyond offset 500
return HTTP 401 upstream; the fleet client stops at the cap and flags it.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

from grants_gov_search import DEFAULT_STATUSES, GrantsSearchSpec



mcp = GuardedFastMCP("mcp-research-resources")


@lru_cache(maxsize=1)
def _grants():
    from grants_gov_search import GrantsGovClient
    return GrantsGovClient()


@lru_cache(maxsize=1)
def _antibodies():
    from antibody_registry import AntibodyRegistryClient
    return AntibodyRegistryClient()





@mcp.tool(annotations=READ_ONLY)
def search_grants(
    keyword: str | None = None,
    opportunity_number: str | None = None,
    aln: str | None = None,
    agencies: list[str] | None = None,
    opportunity_statuses: list[str] | None = None,
    eligibilities: list[str] | None = None,
    funding_categories: list[str] | None = None,
    funding_instruments: list[str] | None = None,
    count_only: bool = False,
    max_records: int = 100,
    include_facets: bool = True,
) -> dict:
    """Search Grants.gov funding opportunities (search2 API; complete, count-verified retrieval).

    At least one search criterion is required. Statuses default to the API's
    own default ["forecasted", "posted"] (current opportunities); add "closed"
    and/or "archived" to search historical ones.

    Args:
        keyword: Free-text keyword search.
        opportunity_number: Exact opportunity number (e.g. "PAR-25-327").
        aln: Assistance Listing Number / CFDA (e.g. "93.866").
        agencies: Agency codes, e.g. ["HHS-NIH11"] (NIH), ["HHS-FDA"], ["NSF"].
        opportunity_statuses: Subset of ["forecasted", "posted", "closed",
            "archived"].
        eligibilities / funding_categories / funding_instruments: Grants.gov
            filter codes (the facet blocks in the response enumerate valid
            values with counts).
        count_only: Return just the hit count + facets (no record retrieval).
        max_records: Cap on opportunity records returned (the walk retrieves
            and count-verifies the complete set; `truncated` flags the cap).
        include_facets: Include facet blocks (status/agency/eligibility/
            category/instrument value counts — independent aggregations of the
            same query).

    Returns:
        {hit_count, n_returned, truncated, records: [{id, number, title,
         agencyCode, agencyName, oppStatus, openDate, closeDate, alnist, ...}],
         facets?}.
    """




    reject_blank(keyword=keyword, opportunity_number=opportunity_number,
                 aln=aln)
    statuses = "|".join(opportunity_statuses) if opportunity_statuses else DEFAULT_STATUSES
    spec = GrantsSearchSpec(
        keyword=keyword,
        opp_num=opportunity_number,
        aln=aln,
        agencies=tuple(agencies) if agencies else None,
        opp_statuses=statuses,
        eligibilities=tuple(eligibilities) if eligibilities else None,
        funding_categories=tuple(funding_categories) if funding_categories else None,
        funding_instruments=tuple(funding_instruments) if funding_instruments else None,
    )
    if count_only:
        hit_count, facets = _grants().count(spec)
        out: dict = {"hit_count": hit_count, "n_returned": 0, "truncated": False,
                     "records": []}
        if include_facets:
            out["facets"] = facets
        return out
    result = _grants().search(spec)
    records = result.records[:max_records]
    out = {
        "hit_count": result.hit_count,
        "n_returned": len(records),
        "truncated": result.hit_count > len(records),
        "records": records,
    }
    if include_facets:
        out["facets"] = result.facets
    return out





@mcp.tool(annotations=READ_ONLY)
def search_antibodies(
    query: str,
    page: int | None = None,
    page_size: int = 100,
    max_records: int = 500,
) -> dict:
    """Full-text search of the Antibody Registry (antibodyregistry.org, ~3.2M records).

    Token-based matching against antibody name / target / catalog text
    (upstream semantics: "TP53" and "p53" are different queries). With
    `page=None` all pages are walked up to `max_records` or the anonymous
    depth cap — rows beyond offset 500 require authentication upstream
    (flagged as `anonymous_limit_hit`, never silently dropped).

    Args:
        query: Search text (target name, catalog number, clone ID, ...).
        page: 1-based page for single-page retrieval; None walks pages.
        page_size: Rows per page (page * page_size must stay <= 500).
        max_records: Cap on rows retrieved in walk mode.

    Returns:
        {query, total_elements (index rows, not unique antibodies),
         retrieved, unique_ab_ids, complete, truncated_at_max_records,
         anonymous_limit_hit, items: [{abId, abName, abTarget, catalogNum,
         vendorName, cloneId, sourceOrganism, targetSpecies, ...}]}.
    """
    return _antibodies().search_antibodies(
        query, page=page, size=page_size, max_records=max_records
    )


@mcp.tool(annotations=READ_ONLY)
def get_antibody(antibody_id: str) -> dict:
    """Fetch Antibody Registry detail record(s) for one antibody accession / RRID.

    Args:
        antibody_id: Plain number ("3643095"), "AB_3643095", or
            "RRID:AB_3643095".

    Returns:
        {ab_id, rrid, record_count, records: [...]} — the upstream route is
        list-valued (an accession can map to multiple curated records, e.g.
        multi-vendor duplicates). A nonexistent id yields record_count 0,
        not an error.
    """
    return _antibodies().get_antibody(antibody_id)


@mcp.tool(annotations=READ_ONLY)
def find_antibodies_by_catalog(
    catalog_number: str,
    vendor: str | None = None,
    page_size: int = 100,
) -> dict:
    """Find antibodies by vendor catalog number (exact, case-insensitive).

    Implemented as full-text search + client-side exact matching on the
    catalog number (or its listed alternatives), because the upstream
    column-filter route returns HTTP 500 for every key.

    Args:
        catalog_number: Vendor catalog number, e.g. "ab32572".
        vendor: Optional vendor-name filter (exact, case-insensitive).
        page_size: Rows per underlying search page.

    Returns:
        {catalog_num, vendor, match_count, search_total_elements, matches}.
    """
    return _antibodies().by_catalog(catalog_number, vendor=vendor, size=page_size)


@mcp.tool(annotations=READ_ONLY)
def get_antibody_registry_stats() -> dict:
    """Registry-level statistics: total antibody count and last-update date.

    Returns the upstream /api/datainfo payload (registry size, last update).
    """
    return _antibodies().datainfo()


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
