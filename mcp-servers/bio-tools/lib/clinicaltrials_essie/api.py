"""Public surface: search_essie / by_investigator / by_sponsor_name /
by_eligibility / recruiting_near, plus the battery-spec runner.

Every function builds an Essie expression (essie.py), sends it server-side via
``filter.advanced`` (optionally combined with ``query.cond`` / ``filter.overallStatus``),
walks the complete pageToken pagination, and returns a deterministic result:

    {
      "spec_id":          str | None,
      "essie_expression": str,            # exact filter.advanced sent
      "params":           {...},          # exact base params sent (minus paging)
      "api_total_count":  int,            # totalCount from page 0
      "n_studies":        int,            # == api_total_count (no local post-filtering)
      "nct_ids":          [sorted...],
      "studies":          [trimmed records, sorted by NCT ID, deduped],
      "provenance":       [per-request log],
    }
"""

from __future__ import annotations

from typing import Any, Sequence

from .client import CTGovClient, DEFAULT_FIELDS
from .essie import (
    and_join, area_phrase, area_range, area_term, or_join, search_location,
)

__all__ = [
    "search_essie", "by_investigator", "by_sponsor_name", "by_eligibility",
    "recruiting_near", "build_spec_query", "run_spec",
]




def _module(study: dict, name: str) -> dict:
    return (study.get("protocolSection") or {}).get(name) or {}


def trim_study(study: dict) -> dict:
    """Project a raw API study record onto the tool's stable, flat schema."""
    ident = _module(study, "identificationModule")
    status = _module(study, "statusModule")
    design = _module(study, "designModule")
    sponsor = _module(study, "sponsorCollaboratorsModule")
    contacts = _module(study, "contactsLocationsModule")
    elig = _module(study, "eligibilityModule")
    conds = _module(study, "conditionsModule")
    locations = contacts.get("locations") or []
    return {
        "nct_id": ident.get("nctId"),
        "brief_title": ident.get("briefTitle"),
        "overall_status": status.get("overallStatus"),
        "study_first_post_date": (status.get("studyFirstPostDateStruct") or {}).get("date"),
        "phases": design.get("phases") or [],
        "study_type": design.get("studyType"),
        "conditions": conds.get("conditions") or [],
        "lead_sponsor_name": (sponsor.get("leadSponsor") or {}).get("name"),
        "lead_sponsor_class": (sponsor.get("leadSponsor") or {}).get("class"),
        "overall_officials": [
            {"name": o.get("name"), "affiliation": o.get("affiliation"),
             "role": o.get("role")}
            for o in (contacts.get("overallOfficials") or [])
        ],
        "responsible_party_investigator": (
            _module(study, "sponsorCollaboratorsModule").get("responsibleParty") or {}
        ).get("investigatorFullName"),
        "minimum_age": elig.get("minimumAge"),
        "maximum_age": elig.get("maximumAge"),
        "sex": elig.get("sex"),
        "healthy_volunteers": elig.get("healthyVolunteers"),
        "location_summary": sorted({
            (loc.get("city") or "", loc.get("state") or "", loc.get("country") or "")
            for loc in locations
        }),
        "n_locations": len(locations),
    }





def _run(base_params: dict[str, str],
         essie_expression: str,
         spec_id: str | None = None,
         fields: str = DEFAULT_FIELDS,
         client: CTGovClient | None = None) -> dict:
    own = client is None
    c = client or CTGovClient()
    try:
        studies, total, provenance = c.paginate_studies(base_params, fields=fields)
        by_id: dict[str, dict] = {}
        for s in studies:
            t = trim_study(s)
            if t["nct_id"]:
                by_id[t["nct_id"]] = t
        nct_ids = sorted(by_id)
        return {
            "spec_id": spec_id,
            "essie_expression": essie_expression,
            "params": dict(base_params),
            "api_total_count": total,
            "n_studies": len(nct_ids),
            "nct_ids": nct_ids,
            "studies": [by_id[n] for n in nct_ids],
            "provenance": provenance,
        }
    finally:
        if own:
            c.close()


def search_essie(advanced_query: str,
                 fields: str = DEFAULT_FIELDS,
                 client: CTGovClient | None = None,
                 spec_id: str | None = None,
                 extra_params: dict[str, str] | None = None) -> dict:
    """Raw Essie passthrough: run any AREA[...] expression via filter.advanced,
    fully paginated and totalCount-verified."""
    params = {"filter.advanced": advanced_query}
    if extra_params:
        params.update(extra_params)
    return _run(params, advanced_query, spec_id=spec_id, fields=fields, client=client)


def by_investigator(name: str,
                    role: str = "any",
                    first_posted_max: str | None = None,
                    client: CTGovClient | None = None,
                    spec_id: str | None = None) -> dict:
    """Studies naming `name` as overall official and/or responsible-party investigator.

    role: 'official' -> AREA[OverallOfficialName] only;
          'responsible_party' -> AREA[ResponsiblePartyInvestigatorFullName] only;
          'any' -> OR of both. The name is quoted (phrase semantics): unquoted
          tokens would match across DIFFERENT officials of the same study.
    """
    if role not in ("any", "official", "responsible_party"):
        raise ValueError(f"bad role: {role!r}")
    parts = []
    if role in ("any", "official"):
        parts.append(area_phrase("OverallOfficialName", name))
    if role in ("any", "responsible_party"):
        parts.append(area_phrase("ResponsiblePartyInvestigatorFullName", name))
    expr = or_join(*parts)
    if first_posted_max:
        expr = and_join(expr, area_range("StudyFirstPostDate", None, first_posted_max))
    return search_essie(expr, client=client, spec_id=spec_id)


def by_sponsor_name(name: str,
                    lead_only: bool = True,
                    first_posted_max: str | None = None,
                    client: CTGovClient | None = None,
                    spec_id: str | None = None) -> dict:
    """Studies whose lead sponsor (or, with lead_only=False, any sponsor/collaborator)
    name matches the quoted phrase."""
    if lead_only:
        base = area_phrase("LeadSponsorName", name)
    else:
        base = or_join(area_phrase("LeadSponsorName", name),
                       area_phrase("CollaboratorName", name))
    if first_posted_max:
        base = and_join(base, area_range("StudyFirstPostDate", None, first_posted_max))
    return search_essie(base, client=client, spec_id=spec_id)


def by_eligibility(condition: str | None = None,
                   criteria_keywords: Sequence[str] = (),
                   min_age_at_least: str | None = None,
                   max_age_at_most: str | None = None,
                   sex: str | None = None,
                   healthy_volunteers: bool | None = None,
                   status: Sequence[str] = (),
                   first_posted_max: str | None = None,
                   client: CTGovClient | None = None,
                   spec_id: str | None = None) -> dict:
    """Eligibility-dimension search.

    - criteria_keywords: quoted phrases matched in AREA[EligibilityCriteria]
    - min_age_at_least:  AREA[MinimumAge]RANGE[<value>, MAX]   (e.g. "18 years")
    - max_age_at_most:   AREA[MaximumAge]RANGE[MIN, <value>]   (e.g. "65 years")
    - sex:               'FEMALE' | 'MALE' | 'ALL'
    - healthy_volunteers: True -> AREA[HealthyVolunteers]y, False -> n
    - status:            list of overallStatus enums, sent as filter.overallStatus
    - condition:         sent as query.cond (Essie ConditionSearch area)
    """
    parts: list[str] = []
    for kw in criteria_keywords:
        parts.append(area_phrase("EligibilityCriteria", kw))
    if min_age_at_least:
        parts.append(area_range("MinimumAge", min_age_at_least, None))
    if max_age_at_most:
        parts.append(area_range("MaximumAge", None, max_age_at_most))
    if sex:
        if sex not in ("FEMALE", "MALE", "ALL"):
            raise ValueError(f"bad sex: {sex!r}")
        parts.append(area_term("Sex", sex))
    if healthy_volunteers is not None:
        parts.append(area_term("HealthyVolunteers", "y" if healthy_volunteers else "n"))
    if first_posted_max:
        parts.append(area_range("StudyFirstPostDate", None, first_posted_max))
    if not parts and not condition:
        raise ValueError("by_eligibility needs at least one constraint")
    expr = and_join(*parts) if parts else ""
    params: dict[str, str] = {}
    if expr:
        params["filter.advanced"] = expr
    if condition:
        params["query.cond"] = condition
    if status:
        params["filter.overallStatus"] = "|".join(status)
    return _run(params, expr, spec_id=spec_id, client=client)


def recruiting_near(condition: str | None = None,
                    city: str | None = None,
                    state: str | None = None,
                    country: str | None = None,
                    recruiting_only: bool = False,
                    same_site: bool = True,
                    extra_advanced: str | None = None,
                    first_posted_max: str | None = None,
                    client: CTGovClient | None = None,
                    spec_id: str | None = None) -> dict:
    """City-level location search.

    same_site=True wraps the location constraints in SEARCH[Location](...) so
    city/state/country (and LocationStatus when recruiting_only) must hold at
    the SAME study site — without it a study recruiting in Paris, Texas and
    completed in Paris, France would match city+country incorrectly.
    recruiting_only adds AREA[LocationStatus]RECRUITING inside the site group
    (note: site-level status; the battery freezes specs with date cutoffs
    instead because recruiting sets drift).
    """
    loc_parts: list[str] = []
    if city:
        loc_parts.append(area_phrase("LocationCity", city))
    if state:
        loc_parts.append(area_phrase("LocationState", state))
    if country:
        loc_parts.append(area_phrase("LocationCountry", country))
    if recruiting_only:
        loc_parts.append(area_term("LocationStatus", "RECRUITING"))
    if not loc_parts:
        raise ValueError("recruiting_near needs at least one location constraint")
    loc_expr = search_location(*loc_parts) if same_site else and_join(*loc_parts)
    parts = [loc_expr]
    if extra_advanced:
        parts.append(extra_advanced)
    if first_posted_max:
        parts.append(area_range("StudyFirstPostDate", None, first_posted_max))
    expr = and_join(*parts)
    params: dict[str, str] = {"filter.advanced": expr}
    if condition:
        params["query.cond"] = condition
    return _run(params, expr, spec_id=spec_id, client=client)





def build_spec_query(spec: dict) -> tuple[str, dict[str, Any]]:
    """Resolve a battery spec dict to (method_name, kwargs)."""
    method = spec["method"]
    kwargs = dict(spec.get("args", {}))
    return method, kwargs


def run_spec(spec: dict, client: CTGovClient | None = None) -> dict:
    """Run one battery spec through its public surface function."""
    method, kwargs = build_spec_query(spec)
    fn = {
        "search_essie": search_essie,
        "by_investigator": by_investigator,
        "by_sponsor_name": by_sponsor_name,
        "by_eligibility": by_eligibility,
        "recruiting_near": recruiting_near,
    }[method]
    return fn(client=client, spec_id=spec["spec_id"], **kwargs)
