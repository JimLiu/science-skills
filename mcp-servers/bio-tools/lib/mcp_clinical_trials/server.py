"""mcp-clinical-trials server — tool handlers + stdio entry point.

Tool names/schemas are served verbatim from ``schemas.json`` (captured from
the original hosted connector). Retrieval goes through the fleet's paced and
retrying ClinicalTrials.gov v2 client (clinicaltrials-essie), with query
translation reusing the fleet builders (clinicaltrials-fetch FilterSpec for
search_trials' fielded filters, clinicaltrials-essie Essie expression
helpers for sponsor/eligibility/investigator dimensions). ``marshal``
reshapes raw study JSON into the original output formats.

Unlike the fleet's public surface functions (which walk the FULL pageToken
pagination), the original connector is page-oriented: page_size/page_token
are forwarded server-side and next_page_token is returned, so no extra
pages are ever fetched.
"""

from __future__ import annotations

from functools import lru_cache

import requests

from clinicaltrials_essie import (
    CTGovClient, and_join, area_phrase, area_range, area_term, or_join,
)
from clinicaltrials_fetch import FilterSpec, build_query_params
from mcp_servers_common import Tier1Server, load_schemas
from mcp_servers_common.criteria import none_if_blank, reject_blank
from mcp_servers_common.gate import apply_gate_tier1

from . import marshal
from .marshal import compact_json


SEARCH_FIELDS = "|".join([
    "NCTId", "OfficialTitle", "BriefTitle", "OverallStatus", "Phase",
    "StudyType", "Condition", "InterventionName", "LeadSponsorName",
    "EnrollmentCount", "StartDate", "PrimaryCompletionDate", "LocationCity",
])
INVESTIGATOR_FIELDS = "|".join([
    "NCTId", "BriefTitle", "Condition",
    "protocolSection.contactsLocationsModule.locations",
])
ENDPOINT_FIELDS = "NCTId|protocolSection.outcomesModule"
DETAILS_FIELDS = "protocolSection|hasResults"



@lru_cache(maxsize=1)
def _client() -> CTGovClient:
    return CTGovClient()




def _as_list(value) -> list[str]:
    """Normalize a string-or-array-or-null schema value to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _enum_list(value) -> list[str]:
    """String-or-array enum value(s), upper-cased (API enums are UPPER)."""
    return [v.strip().upper() for v in _as_list(value) if v.strip()]


def _page_size(args: dict, default: int) -> int:
    try:
        size = int(args.get("page_size") or default)
    except (TypeError, ValueError):
        size = default
    return max(1, min(size, 1000))


def normalize_nct(nct_id: str) -> str:
    """'NCT' + 8 digits; prepend NCT if the user passed just the number
    (documented behavior of the original tool). Case-insensitive."""
    nct = str(nct_id).strip().upper()
    if nct and not nct.startswith("NCT"):
        nct = "NCT" + nct
    return nct


def _fetch_page(params: dict[str, str], page_size: int,
                page_token: str | None, count_total: bool,
                fields: str) -> dict:
    """One server-side page of /studies (the original is page-oriented;
    fleet-style full pagination is deliberately NOT done here)."""
    p = dict(params)
    p["pageSize"] = str(page_size)
    p["fields"] = fields
    if count_total:
        p["countTotal"] = "true"
    if page_token:
        p["pageToken"] = page_token
    data, _meta = _client().get_json("/studies", p)
    return data




def _blanks_as_absent(args: dict) -> dict:
    """Copy of args with blank/whitespace-only string values removed —
    whitespace is truthy, so without this a criterion like condition=" "
    rides into the Essie query as a vacuous filter (and, alone, widens the
    call to a status-only browse). Same rule as mcp_servers_common.criteria."""
    return {k: v for k, v in args.items()
            if not (isinstance(v, str) and not v.strip())}


def search_trials_params(args: dict) -> dict[str, str]:
    """search_trials → clinicaltrials-fetch FilterSpec for the fielded
    filters, plus query.locn / query.spons / merged advanced_query."""



    study_type = none_if_blank(args.get("study_type"))
    fielded = dict(
        condition=none_if_blank(args.get("condition")),
        intervention=none_if_blank(args.get("intervention")),
        overall_status=tuple(_enum_list(args.get("status"))),
        phase=tuple(_enum_list(args.get("phase"))),
        study_type=study_type.strip().upper() if study_type else None,
    )
    if any(v for v in fielded.values()):
        spec = FilterSpec(spec_id="mcp-clinical-trials", **fielded)
        spec.validate()
        params = build_query_params(spec)
    else:
        params = {}
    if none_if_blank(args.get("location")):
        params["query.locn"] = args["location"]
    if none_if_blank(args.get("sponsor")):
        params["query.spons"] = args["sponsor"]
    if none_if_blank(args.get("advanced_query")):
        params["filter.advanced"] = and_join(
            params.get("filter.advanced"), args["advanced_query"])




    if not params:
        raise ValueError(
            "search_trials needs at least one of: condition, intervention, "
            "status, phase, study_type, location, sponsor, advanced_query "
            "(page_token continuations must repeat the original criteria)")
    return params


def sponsor_params(args: dict) -> dict[str, str]:
    """search_by_sponsor → Essie LeadSponsorName phrase (partial match)."""
    args = _blanks_as_absent(args)


    if not args.get("sponsor_name"):
        raise ValueError("sponsor_name must be non-empty")
    parts = [area_phrase("LeadSponsorName", args["sponsor_name"])]
    phases = _enum_list(args.get("phase"))
    if phases:
        parts.append(or_join(*[area_term("Phase", p) for p in phases]))
    params = {"filter.advanced": and_join(*parts)}
    if args.get("condition"):
        params["query.cond"] = args["condition"]
    status = _enum_list(args.get("status"))
    if status:
        params["filter.overallStatus"] = "|".join(status)
    return params


def eligibility_params(args: dict) -> dict[str, str]:
    """search_by_eligibility → Essie eligibility dimensions, patient-matching
    semantics per the tool's own documentation:

    - min_age is the PATIENT's age: trials whose MinimumAge <= it
      (RANGE[MIN, min_age]); max_age: trials whose MaximumAge >= it.
    - sex=MALE/FEMALE matches trials accepting that sex (Sex=X OR Sex=ALL);
      sex=ALL matches all-comers trials.
    - status defaults to RECRUITING (documented default).
    """
    args = _blanks_as_absent(args)
    parts: list[str] = []
    if args.get("eligibility_keywords"):
        parts.append(area_phrase("EligibilityCriteria", args["eligibility_keywords"]))
    if args.get("min_age"):
        parts.append(area_range("MinimumAge", None, args["min_age"]))
    if args.get("max_age"):
        parts.append(area_range("MaximumAge", args["max_age"], None))
    sex = args.get("sex")
    if sex:
        sex = str(sex).upper()
        if sex in ("MALE", "FEMALE"):
            parts.append(or_join(area_term("Sex", sex), area_term("Sex", "ALL")))
        else:
            parts.append(area_term("Sex", "ALL"))
    params: dict[str, str] = {}
    if parts:
        params["filter.advanced"] = and_join(*parts)
    if args.get("condition"):
        params["query.cond"] = args["condition"]
    status = _enum_list(args.get("status")) or ["RECRUITING"]
    params["filter.overallStatus"] = "|".join(status)
    if not parts and not args.get("condition"):
        raise ValueError(
            "search_by_eligibility needs at least one of: condition, "
            "eligibility_keywords, min_age, max_age, sex")
    return params


def investigator_params(args: dict) -> dict[str, str]:
    """search_investigators → Essie OverallOfficialName /
    ResponsiblePartyInvestigatorFullName phrase search; institution filters
    on LocationFacility (and takes precedence over location, as documented)."""
    args = _blanks_as_absent(args)
    parts: list[str] = []
    if args.get("investigator_name"):
        name = args["investigator_name"]
        parts.append(or_join(
            area_phrase("OverallOfficialName", name),
            area_phrase("ResponsiblePartyInvestigatorFullName", name)))
    params: dict[str, str] = {}
    if args.get("institution"):
        parts.append(area_phrase("LocationFacility", args["institution"]))
    elif args.get("location"):
        params["query.locn"] = args["location"]
    if args.get("condition"):
        params["query.cond"] = args["condition"]
    status = _enum_list(args.get("status"))
    if status:
        params["filter.overallStatus"] = "|".join(status)
    if parts:
        params["filter.advanced"] = and_join(*parts)
    if not params:
        raise ValueError(
            "search_investigators needs at least one of: investigator_name, "
            "institution, location, condition, status")
    return params


def endpoints_params(args: dict) -> dict[str, str]:
    """analyze_endpoints aggregate mode → condition + phase + StartDate."""
    params: dict[str, str] = {"query.cond": args["condition"]}
    phases = _enum_list(args.get("phase"))
    parts: list[str] = []
    if phases:
        parts.append(or_join(*[area_term("Phase", p) for p in phases]))
    if args.get("start_date_after"):
        parts.append(area_range("StartDate", args["start_date_after"], None))
    if parts:
        params["filter.advanced"] = and_join(*parts)
    return params




def get_trial_details(args: dict) -> str:




    reject_blank(nct_id=args["nct_id"])
    nct = normalize_nct(args["nct_id"])
    try:
        study, _meta = _client().get_study(nct, fields=DETAILS_FIELDS)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (400, 404):
            return compact_json(marshal.trial_not_found_response(
                nct, f"Trial {nct} not found"))
        raise
    return compact_json(marshal.trial_details_response(study))


def analyze_endpoints(args: dict) -> str:
    args = _blanks_as_absent(args)
    nct_id = args.get("nct_id")
    condition = args.get("condition")
    if not nct_id and not condition:
        raise ValueError("analyze_endpoints needs nct_id or condition")
    if nct_id:
        nct = normalize_nct(nct_id)
        study, _meta = _client().get_study(nct, fields=ENDPOINT_FIELDS)
        return compact_json(marshal.endpoints_response([study], nct, None))
    page = _fetch_page(endpoints_params(args), _page_size(args, 50),
                       None, False, ENDPOINT_FIELDS)
    return compact_json(marshal.endpoints_response(
        page.get("studies", []), None, condition))


def search_trials(args: dict) -> str:
    count_total = bool(args.get("count_total"))
    page = _fetch_page(search_trials_params(args), _page_size(args, 10),
                       args.get("page_token"), count_total, SEARCH_FIELDS)
    return compact_json(marshal.search_response(page, count_total))


def search_by_sponsor(args: dict) -> str:
    count_total = bool(args.get("count_total"))
    page = _fetch_page(sponsor_params(args), _page_size(args, 10),
                       args.get("page_token"), count_total, SEARCH_FIELDS)
    return compact_json(marshal.search_response(page, count_total))


def search_by_eligibility(args: dict) -> str:
    page = _fetch_page(eligibility_params(args), _page_size(args, 10),
                       args.get("page_token"), False, SEARCH_FIELDS)
    return compact_json(marshal.search_response(page, False))


def search_investigators(args: dict) -> str:
    page = _fetch_page(investigator_params(args), _page_size(args, 20),
                       None, False, INVESTIGATOR_FIELDS)
    return compact_json(marshal.investigators_response(page.get("studies", [])))


HANDLERS = {
    "analyze_endpoints": analyze_endpoints,
    "get_trial_details": get_trial_details,
    "search_by_eligibility": search_by_eligibility,
    "search_by_sponsor": search_by_sponsor,
    "search_investigators": search_investigators,
    "search_trials": search_trials,
}


def build_server() -> Tier1Server:
    return Tier1Server("clinical-trials-mcp-server", load_schemas(__package__), HANDLERS)


def main() -> None:



    t1 = build_server()
    apply_gate_tier1(t1)
    t1.run()


if __name__ == "__main__":
    main()
