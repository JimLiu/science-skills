"""Marshal raw ClinicalTrials.gov v2 study records into the ORIGINAL
Clinical Trials connector's output formats (see
mcp-servers/_snapshots/original_outputs/mcp-clinical-trials/).

Retrieval lives in the fleet packages (clinicaltrials-fetch,
clinicaltrials-essie — shared paced/retrying CTGovClient + Essie builders);
this module only reshapes raw study JSON. All mappings below were verified
field-by-field against the captured originals and the live v2 API:

- search item ``title``   = officialTitle (briefTitle fallback)
- search item ``phase``   = designModule.phases, or null when absent
  (observational studies) — NOT [] (the capture distinguishes them)
- endpoints               = protocolSection.outcomesModule (protocol
  endpoints; NCT04280705's capture matches 4 primary / 39 secondary protocol
  outcomes, not the 43 posted-results outcome measures)
- investigators           = locations[].contacts[] only (the capture for
  NCT06560905 contains exactly the two location contacts and neither the
  central contact nor the overall official as separate entries;
  ``affiliation`` is the location's facility name, ``location`` its city)
- healthy_volunteers      = "Yes"/"No" string from the API boolean
"""

from __future__ import annotations

from collections import Counter
from typing import Any


import json


def compact_json(obj: object) -> str:
    """Serialize like the original Clinical Trials connector (compact,
    ensure_ascii=False) — verified byte-identical against every capture."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)




def _module(study: dict, name: str) -> dict:
    return (study.get("protocolSection") or {}).get(name) or {}


def _struct_date(module: dict, key: str) -> str | None:
    return (module.get(key) or {}).get("date")




def trial_summary(study: dict) -> dict[str, Any]:
    """One search-result item in the original connector's shape."""
    ident = _module(study, "identificationModule")
    status = _module(study, "statusModule")
    design = _module(study, "designModule")
    sponsor = _module(study, "sponsorCollaboratorsModule")
    conds = _module(study, "conditionsModule")
    arms = _module(study, "armsInterventionsModule")
    locations = _module(study, "contactsLocationsModule").get("locations") or []
    return {
        "nct_id": ident.get("nctId"),
        "title": ident.get("officialTitle") or ident.get("briefTitle"),
        "status": status.get("overallStatus"),
        "phase": design.get("phases") or None,
        "conditions": conds.get("conditions") or [],
        "interventions": [i.get("name") for i in (arms.get("interventions") or [])
                          if i.get("name")],
        "sponsor": (sponsor.get("leadSponsor") or {}).get("name"),
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "start_date": _struct_date(status, "startDateStruct"),
        "primary_completion_date": _struct_date(status, "primaryCompletionDateStruct"),
        "locations_count": len(locations),
        "study_type": design.get("studyType"),
    }


def search_response(page: dict, count_total: bool) -> dict[str, Any]:
    """Reshape one raw /studies page into the original search output."""
    items = [trial_summary(s) for s in page.get("studies", [])]
    return {
        "count": len(items),
        "total": page.get("totalCount") if count_total else None,
        "next_page_token": page.get("nextPageToken"),
        "items": items,
    }




def _outcomes(outcomes_module: dict, key: str, type_label: str) -> list[dict] | None:
    raw = outcomes_module.get(key)
    if not raw:
        return None
    return [{
        "measure": o.get("measure"),
        "time_frame": o.get("timeFrame"),
        "description": o.get("description"),
        "type": type_label,
    } for o in raw]


def _location(loc: dict) -> dict[str, Any]:
    return {
        "facility": loc.get("facility"),
        "city": loc.get("city"),
        "state": loc.get("state"),
        "country": loc.get("country"),
        "zip": loc.get("zip"),
        "status": loc.get("status"),
        "contacts": loc.get("contacts"),
    }


def trial_details_response(study: dict) -> dict[str, Any]:
    ident = _module(study, "identificationModule")
    status = _module(study, "statusModule")
    design = _module(study, "designModule")
    sponsor = _module(study, "sponsorCollaboratorsModule")
    desc = _module(study, "descriptionModule")
    elig = _module(study, "eligibilityModule")
    outcomes = _module(study, "outcomesModule")
    conds = _module(study, "conditionsModule")
    arms = _module(study, "armsInterventionsModule")
    locations = _module(study, "contactsLocationsModule").get("locations")
    nct_id = ident.get("nctId")
    collaborators = [c.get("name") for c in (sponsor.get("collaborators") or [])
                     if c.get("name")] or None
    hv = elig.get("healthyVolunteers")
    trial = {
        "nct_id": nct_id,
        "title": ident.get("officialTitle") or ident.get("briefTitle"),
        "brief_title": ident.get("briefTitle"),
        "acronym": ident.get("acronym"),
        "status": status.get("overallStatus"),
        "phase": design.get("phases") or None,
        "study_type": design.get("studyType"),
        "conditions": conds.get("conditions") or [],
        "interventions": [i.get("name") for i in (arms.get("interventions") or [])
                          if i.get("name")],
        "sponsor": (sponsor.get("leadSponsor") or {}).get("name"),
        "collaborators": collaborators,
        "enrollment": (design.get("enrollmentInfo") or {}).get("count"),
        "start_date": _struct_date(status, "startDateStruct"),
        "primary_completion_date": _struct_date(status, "primaryCompletionDateStruct"),
        "completion_date": _struct_date(status, "completionDateStruct"),
        "brief_summary": desc.get("briefSummary"),
        "detailed_description": desc.get("detailedDescription"),
        "eligibility_criteria": elig.get("eligibilityCriteria"),
        "minimum_age": elig.get("minimumAge"),
        "maximum_age": elig.get("maximumAge"),
        "sex": elig.get("sex"),
        "healthy_volunteers": None if hv is None else ("Yes" if hv else "No"),
        "primary_outcomes": _outcomes(outcomes, "primaryOutcomes", "PRIMARY"),
        "secondary_outcomes": _outcomes(outcomes, "secondaryOutcomes", "SECONDARY"),
        "other_outcomes": _outcomes(outcomes, "otherOutcomes", "OTHER"),
        "locations": [_location(l) for l in locations] if locations else None,
        "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else None,
        "has_results": study.get("hasResults"),
    }
    return {"found": True, "trial": trial}


def trial_not_found_response(nct_id: str, error: str) -> dict[str, Any]:
    return {"found": False, "nct_id": nct_id, "error": error}




def endpoints_response(studies: list[dict],
                       nct_id: str | None,
                       condition: str | None) -> dict[str, Any]:
    """Aggregate protocol endpoints across the analyzed studies.

    ``common_measures`` = the 20 most common endpoint measure names across
    all analyzed trials (ties keep first-seen order — for a single trial this
    reproduces the original's primary-then-secondary listing).
    """
    primary: list[dict] = []
    secondary: list[dict] = []
    other: list[dict] = []
    counts: Counter[str] = Counter()
    for study in studies:
        outcomes = _module(study, "outcomesModule")
        for dest, key, label in ((primary, "primaryOutcomes", "PRIMARY"),
                                 (secondary, "secondaryOutcomes", "SECONDARY"),
                                 (other, "otherOutcomes", "OTHER")):
            for o in outcomes.get(key) or []:
                dest.append({
                    "measure": o.get("measure"),
                    "time_frame": o.get("timeFrame"),
                    "description": o.get("description"),
                    "type": label,
                })
                if o.get("measure"):
                    counts[o["measure"]] += 1
    return {
        "trials_analyzed": len(studies),
        "nct_id": nct_id,
        "condition": condition,
        "primary_endpoints": primary,
        "secondary_endpoints": secondary,
        "other_endpoints": other,
        "common_measures": [m for m, _ in counts.most_common(20)],
    }




def investigators_response(studies: list[dict]) -> dict[str, Any]:
    """Collect investigators/contacts from the analyzed trials' site
    contact lists (locations[].contacts[]), deduplicated per trial."""
    investigators: list[dict] = []
    seen: set[tuple] = set()
    for study in studies:
        ident = _module(study, "identificationModule")
        conds = _module(study, "conditionsModule").get("conditions") or []
        nct_id = ident.get("nctId")
        title = ident.get("briefTitle")
        condition = conds[0] if conds else None
        for loc in _module(study, "contactsLocationsModule").get("locations") or []:
            for contact in loc.get("contacts") or []:
                key = (contact.get("name"), contact.get("role"), nct_id)
                if key in seen:
                    continue
                seen.add(key)
                investigators.append({
                    "name": contact.get("name"),
                    "role": contact.get("role"),
                    "affiliation": loc.get("facility"),
                    "facility": loc.get("facility"),
                    "location": loc.get("city"),
                    "nct_id": nct_id,
                    "study_title": title,
                    "condition": condition,
                })
    return {
        "count": len(investigators),
        "trials_analyzed": len(studies),
        "investigators": investigators,
    }
