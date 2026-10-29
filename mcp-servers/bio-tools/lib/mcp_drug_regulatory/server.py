"""mcp-drug-regulatory server — FastMCP tools over openfda-drugsfda + openfda-labels.

Retrieval is delegated to the fleet packages; this module is marshalling only.
Both endpoints are openFDA: anonymous rate limits apply (fleet clients pace
requests); result sets deeper than 26,000 rows are unreachable via skip
pagination and raise with guidance to narrow the query.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

from openfda_drugsfda import COUNT_FIELDS, SearchSpec
from openfda_labels import run_spec



mcp = GuardedFastMCP("mcp-drug-regulatory")


@lru_cache(maxsize=1)
def _drugsfda():
    from openfda_drugsfda import OpenFDADrugsFDAClient
    return OpenFDADrugsFDAClient()


@lru_cache(maxsize=1)
def _labels():
    from openfda_labels import OpenFDAClient
    return OpenFDAClient()


def _build_spec(
    brand: Optional[str],
    generic: Optional[str],
    active_ingredient: Optional[str],
    sponsor: Optional[str],
    marketing_status: Optional[str],
    dosage_form: Optional[str],
    route: Optional[str],
    application_number: Optional[str],
    pharm_class: Optional[str],
    pharm_class_type: str,
    search_type: str,
    submission_date_from: Optional[str],
    submission_date_to: Optional[str],
    raw_search: Optional[str],
) -> SearchSpec:
    return SearchSpec(
        brand=brand,
        generic=generic,
        active_ingredient=active_ingredient,
        sponsor=sponsor,
        marketing_status=marketing_status,
        dosage_form=dosage_form,
        route=route,
        application_number=application_number,
        pharm_class=pharm_class,
        pharm_class_type=pharm_class_type,
        search_type=search_type,
        submission_date_from=submission_date_from,
        submission_date_to=submission_date_to,
        raw=raw_search,
    )





def _yyyymmdd(date: str | None) -> str:
    """'2026-08-19' and '20260819' both give '20260819'; '' when absent."""
    return (date or "").replace("-", "")


def _latest_action(record: dict, lo: str = "", hi: str = "") -> str:
    """Newest submissions[].submission_status_date within [lo, hi] (YYYYMMDD,
    inclusive, '' = unbounded), as YYYYMMDD; '' when the record has none."""
    dates = (
        _yyyymmdd(s.get("submission_status_date")) for s in record.get("submissions") or []
    )
    in_window = (d for d in dates if d and (not lo or lo <= d) and (not hi or d <= hi))
    return max(in_window, default="")


def _newest_first(
    records: list[dict], date_from: str | None = None, date_to: str | None = None
) -> list[dict]:
    """Sort applications by their newest submission_status_date, descending;
    with date_from/date_to (YYYY-MM-DD or YYYYMMDD, inclusive, either may be
    None) only dates inside that window count. Records with no counting date
    sort last; ties break on application_number, descending."""
    lo, hi = _yyyymmdd(date_from), _yyyymmdd(date_to)
    return sorted(
        records,
        key=lambda r: (_latest_action(r, lo, hi), r.get("application_number") or ""),
        reverse=True,
    )


@mcp.tool(annotations=READ_ONLY)
def search_drug_applications(
    brand: str | None = None,
    generic: str | None = None,
    active_ingredient: str | None = None,
    sponsor: str | None = None,
    marketing_status: str | None = None,
    dosage_form: str | None = None,
    route: str | None = None,
    pharm_class: str | None = None,
    pharm_class_type: str = "epc",
    search_type: str = "and",
    submission_date_from: str | None = None,
    submission_date_to: str | None = None,
    raw_search: str | None = None,
    max_records: int = 100,
) -> dict:
    """Search FDA Drugs@FDA applications (NDA/ANDA/BLA) by any combination of filters.

    All text filters are exact-phrase matches. Use this for multi-field regulatory
    search: by brand/generic name, active ingredient, sponsor, marketing status
    ("Prescription", "Over-the-counter", "Discontinued"), dosage form, route, or
    pharmacologic class (therapeutic-class search: pharm_class + pharm_class_type
    "epc" (Established Pharmacologic Class) | "moa" (Mechanism of Action) |
    "cs" (Chemical Structure) | "pe" (Physiologic Effect)).

    Caveat: `generic` and `pharm_class` query the harmonized openfda block, which
    is absent on many older applications — those records are silently skipped by
    upstream openFDA. Retrieval is complete and count-verified; result sets over
    26,000 applications raise with guidance (narrow with submission_date_from/to).

    Args:
        search_type: "and" (default) or "or" — how the provided filters combine.
        submission_date_from/to: Constrain submissions.submission_status_date
            (YYYY-MM-DD); always ANDed. An application matches if any of its
            submissions, original or supplement, falls in the range.
        raw_search: Verbatim openFDA search= string (escape hatch; replaces the
            mapped filters and date bounds). Put spaces between terms, not '+'
            (it is URL-encoded to %2B and the query fails).
        max_records: Cap on records returned (the full set is still retrieved
            and counted; `truncated` flags the cap). Records come newest first,
            by each application's most recent submission_status_date (only
            dates inside submission_date_from/to count when given), so the cap
            drops the oldest.

    Returns:
        {total (API's own count), n_returned, truncated, order,
         note (when truncated: what was dropped and how to get it), last_updated,
         records: [{application_number, sponsor_name, products: [...],
         submissions: [...], openfda_generic_name, openfda_pharm_class_epc, ...}]}.
        A first approval is the submission with submission_type "ORIG" and
        submission_status "AP". `last_updated` is the dataset date: Drugs@FDA
        can trail the newest approvals by days and excludes CBER-regulated
        biologics (vaccines, blood products, cell and gene therapies), so
        cross-check "most recent" questions against a current source.
    """



    if blank(raw_search):
        raise ValueError("raw_search must be non-empty")
    spec = _build_spec(
        brand, generic, active_ingredient, sponsor, marketing_status, dosage_form,
        route, None, pharm_class, pharm_class_type, search_type,
        submission_date_from, submission_date_to, raw_search,
    )
    result = _drugsfda().retrieve(spec)




    window = (submission_date_from, submission_date_to) if raw_search is None else (None, None)
    out = _newest_first(result.records, *window)[:max_records]
    scope = " inside submission_date_from/to" if any(window) else ""
    narrow = "the raw_search string" if raw_search is not None else "the query (dates, filters)"
    truncated = result.total > len(out)
    response = {
        "search": spec.to_search(),
        "total": result.total,
        "n_returned": len(out),
        "truncated": truncated,
        "order": f"newest submission_status_date{scope} first",
    }
    if truncated:
        response["note"] = (
            f"Kept the {len(out)} of {result.total} matching applications with the "
            f"most recent submission activity{scope} (newest first) and dropped the "
            f"{result.total - len(out)} with older activity. Narrow {narrow} or raise "
            f"max_records (up to {result.total}) to see the rest."
        )
    response["last_updated"] = result.last_updated
    response["records"] = out
    return response


@mcp.tool(annotations=READ_ONLY)
def get_drug_application(application_number: str) -> dict:
    """Fetch one Drugs@FDA application by its number (e.g. "NDA020702", "ANDA076543", "BLA125514").

    Returns:
        {application_number, found, record} — `record` carries sponsor_name,
        products (brand name, active ingredients + strengths, dosage form,
        route, marketing status, TE code), the full regulatory submission
        history, and harmonized openfda fields (generic/brand names, UNII,
        RxCUI, NDCs, pharmacologic classes) when present; `record` is null and
        `found` false if the number does not exist.
    """
    rec = _drugsfda().get_application(application_number)
    return {
        "application_number": application_number,
        "found": rec is not None,
        "record": rec,
    }


@mcp.tool(annotations=READ_ONLY)
def count_drug_applications(
    count_field: str,
    brand: str | None = None,
    generic: str | None = None,
    active_ingredient: str | None = None,
    sponsor: str | None = None,
    marketing_status: str | None = None,
    dosage_form: str | None = None,
    route: str | None = None,
    pharm_class: str | None = None,
    pharm_class_type: str = "epc",
    search_type: str = "and",
    max_buckets: int = 1000,
) -> dict:
    """Aggregate Drugs@FDA applications: bucket counts over one field, optionally filtered.

    Args:
        count_field: One of the verified friendly names — "sponsor_name",
            "application_number", "dosage_form", "route", "marketing_status",
            "te_code", "pharm_class_epc", "pharm_class_moa", "pharm_class_cs",
            "pharm_class_pe" — or a raw openFDA field path (".exact" suffix
            required for analyzed fields).
        brand/generic/.../pharm_class: Optional filters (same semantics as
            search_drug_applications); no filters = aggregate the whole corpus.
        max_buckets: Maximum buckets (openFDA cap: 1000).

    Returns:
        {count_field, api_field, n_buckets, bucket_sum, buckets:
         [{term, count}, ...] in descending count order}.
    """
    filters_given = any([brand, generic, active_ingredient, sponsor, marketing_status,
                         dosage_form, route, pharm_class])
    spec = None
    if filters_given:
        spec = _build_spec(
            brand, generic, active_ingredient, sponsor, marketing_status,
            dosage_form, route, None, pharm_class, pharm_class_type, search_type,
            None, None, None,
        )
    result = _drugsfda().count_by(count_field, spec=spec, limit=max_buckets)
    return {
        "count_field": count_field,
        "api_field": COUNT_FIELDS.get(count_field, count_field),
        "n_buckets": len(result.buckets),
        "bucket_sum": result.bucket_sum,
        "buckets": result.buckets,
    }


@mcp.tool(annotations=READ_ONLY)
def get_drug_statistics() -> dict:
    """Corpus-level Drugs@FDA statistics in one call.

    Returns:
        {total_applications, last_updated, marketing_status: [{term, count}],
         dosage_form_top (top 25), dosage_form_distinct, route_top (top 25),
         route_distinct, sponsor_top (top 25 by application count)}.
    """
    return _drugsfda().statistics()


@mcp.tool(annotations=READ_ONLY)
def list_pharmacologic_classes(class_type: str = "epc", max_buckets: int = 1000) -> dict:
    """Enumerate pharmacologic classes present in Drugs@FDA, with application counts.

    Args:
        class_type: "epc" (Established Pharmacologic Class, default), "moa"
            (Mechanism of Action), "cs" (Chemical Structure), or "pe"
            (Physiologic Effect).
        max_buckets: Maximum classes returned (openFDA cap: 1000).

    Returns:
        {class_type, n_classes, classes: [{term, count}, ...] in descending
         count order}. Counts reflect only applications with a harmonized
         openfda block (older records lack one).
    """
    result = _drugsfda().pharmacologic_classes(class_type)
    buckets = result.buckets[:max_buckets]
    return {"class_type": class_type, "n_classes": len(buckets), "classes": buckets}


@mcp.tool(annotations=READ_ONLY)
def get_generic_equivalents(brand: str) -> dict:
    """Find applications sharing a brand-name drug's exact active-ingredient set.

    Resolves the brand to its reference application(s), extracts the exact
    active-ingredient name set(s), then returns every Drugs@FDA application
    (brand or generic, any sponsor) whose product ingredient set matches.

    Returns:
        {brand, reference_applications: [application_numbers],
         active_ingredient_sets: [[names]], equivalents: [full application
         records incl. TE codes and marketing status]}.
    """
    return _drugsfda().generic_equivalents(brand)





@mcp.tool(annotations=READ_ONLY)
def search_drug_labels(
    active_ingredient: str | None = None,
    generic_name: str | None = None,
    brand_name: str | None = None,
    route: str | None = None,
    product_type: str | None = None,
    exact: bool = False,
    raw_search: str | None = None,
    sections: list[str] | None = None,
    max_records: int = 50,
) -> dict:
    """Retrieve FDA drug product labels (SPL) by ingredient/name/route, with targeted section extraction.

    Default records carry identification (set_id, SPL version, effective date,
    brand/generic/substance names, manufacturer, route, product type,
    application numbers), boxed-warning presence, which warning-type sections
    exist, and the indications_and_usage text. Pass `sections` to instead get
    just those raw label sections per record (token-lean) — e.g.
    ["boxed_warning"], ["dosage_and_administration", "contraindications"],
    ["adverse_reactions"].

    Matching: analyzed fields are tokenized — brand_name "Tylenol" also matches
    "Tylenol Extra Strength"; active_ingredient "FENTANYL" also matches
    "FENTANYL CITRATE". Set exact=true for whole-string matching.

    Args:
        active_ingredient: Substance name (openfda.substance_name).
        generic_name / brand_name / route: openfda label fields.
        product_type: "HUMAN PRESCRIPTION DRUG" or "HUMAN OTC DRUG".
        exact: Query the non-analyzed .exact field variants.
        raw_search: Verbatim openFDA search= string (escape hatch; mutually
            exclusive with the mapped filters).
        sections: Raw openFDA label section names to extract instead of the
            default structured record.
        max_records: Cap on label records returned (retrieval itself is
            complete and count-verified; `truncated` flags the cap).

    Returns:
        {search, total (API count), n_returned, truncated, records: [...]}.
    """
    spec: dict = {}
    for key, val in (
        ("active_ingredient", active_ingredient),
        ("generic_name", generic_name),
        ("brand_name", brand_name),
        ("route", route),
        ("product_type", product_type),
    ):
        if val is not None:
            spec[key] = val
    if exact:
        spec["exact"] = True
    if blank(raw_search):
        raise ValueError("raw_search must be non-empty")
    if raw_search is not None:
        spec = {"search": raw_search}
    result = run_spec(spec, _labels(), sections=sections)
    out = result["records"][:max_records]
    return {
        "search": result["search"],
        "total": result["total"],
        "n_returned": len(out),
        "truncated": result["count"] > len(out),
        "records": out,
    }


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
