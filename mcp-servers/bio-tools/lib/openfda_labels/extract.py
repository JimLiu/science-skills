"""Structured extraction from raw openFDA label documents.

``extract_record``  -> the tool's default structured record (identification,
                       classification, boxed-warning presence, which warning-type
                       sections are present, and the indications_and_usage text).
``extract_sections`` -> targeted section extraction: a minimal record carrying
                        only the requested label section(s), for token-lean output.
"""

from __future__ import annotations


WARNING_SECTION_FIELDS = [
    "boxed_warning",
    "warnings",
    "warnings_and_cautions",
    "contraindications",
    "precautions",
    "general_precautions",
    "adverse_reactions",
    "drug_interactions",
    "do_not_use",
    "stop_use",
    "ask_doctor",
    "ask_doctor_or_pharmacist",
    "when_using",
]


def _openfda_list(raw: dict, key: str) -> list[str]:
    """A sorted, de-duplicated copy of an ``openfda.<key>`` array (deterministic)."""
    values = raw.get("openfda", {}).get(key, [])
    return sorted(set(values))


def _section_text(raw: dict, key: str) -> str | None:
    """Join a label section's paragraph array into one string, or None if absent."""
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def extract_record(raw: dict) -> dict:
    """Map a raw openFDA label document to the tool's structured record."""
    return {
        "set_id": raw.get("set_id"),
        "spl_id": raw.get("id"),
        "spl_version": raw.get("version"),
        "effective_time": raw.get("effective_time"),
        "brand_name": _openfda_list(raw, "brand_name"),
        "generic_name": _openfda_list(raw, "generic_name"),
        "substance_name": _openfda_list(raw, "substance_name"),
        "manufacturer_name": _openfda_list(raw, "manufacturer_name"),
        "route": _openfda_list(raw, "route"),
        "product_type": _openfda_list(raw, "product_type"),
        "application_number": _openfda_list(raw, "application_number"),
        "has_boxed_warning": "boxed_warning" in raw,
        "warning_sections_present": [k for k in WARNING_SECTION_FIELDS if k in raw],
        "indications_and_usage": _section_text(raw, "indications_and_usage"),
    }


def extract_sections(raw: dict, sections: list[str]) -> dict:
    """Targeted section extraction: identification fields + only the requested sections.

    ``sections`` are raw openFDA label field names, e.g. ``["indications_and_usage"]``
    or ``["boxed_warning", "dosage_and_administration"]``.  Absent sections are None.
    """
    record = {
        "set_id": raw.get("set_id"),
        "brand_name": _openfda_list(raw, "brand_name"),
        "generic_name": _openfda_list(raw, "generic_name"),
    }
    for key in sections:
        record[key] = _section_text(raw, key)
    return record
