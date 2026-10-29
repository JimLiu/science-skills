"""Declarative query spec -> openFDA search string.

A spec is a plain dict with any of the following keys (all values are strings):

    active_ingredient   -> openfda.substance_name
    generic_name        -> openfda.generic_name
    brand_name          -> openfda.brand_name
    route               -> openfda.route
    product_type        -> openfda.product_type   (marketing status:
                           "HUMAN PRESCRIPTION DRUG" or "HUMAN OTC DRUG")
    search              -> raw openFDA search string (escape hatch; mutually
                           exclusive with the mapped fields)
    exact               -> bool; if true, the ``.exact`` (non-analyzed) variant of
                           each mapped field is queried instead of the analyzed one

Mapped fields are quoted as phrases and joined with " AND " in a fixed key order,
so the same spec always produces byte-identical search strings.

Note on matching semantics: the analyzed fields are tokenized by openFDA, so
``brand_name: "Tylenol"`` matches every product whose brand name contains the
token "Tylenol" (e.g. "Tylenol Extra Strength"), and
``active_ingredient: "FENTANYL"`` also matches "FENTANYL CITRATE".  Use
``exact: true`` for whole-string matching against the ``.exact`` fields.
"""

from __future__ import annotations

FIELD_MAP = {
    "active_ingredient": "openfda.substance_name",
    "generic_name": "openfda.generic_name",
    "brand_name": "openfda.brand_name",
    "route": "openfda.route",
    "product_type": "openfda.product_type",
}


FIELD_ORDER = ["active_ingredient", "generic_name", "brand_name", "route", "product_type"]

_META_KEYS = {"id", "exact", "search", "frozen_total", "frozen_search"}


class SpecError(ValueError):
    pass


def validate_spec(spec: dict) -> None:
    """Raise SpecError if the spec is malformed."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be a dict")
    unknown = set(spec) - set(FIELD_MAP) - _META_KEYS
    if unknown:
        raise SpecError(f"unknown spec keys: {sorted(unknown)}")
    has_mapped = any(k in spec for k in FIELD_MAP)
    has_raw = "search" in spec
    if has_mapped and has_raw:
        raise SpecError("'search' is mutually exclusive with mapped fields")
    if not has_mapped and not has_raw:
        raise SpecError("spec must contain at least one query field "
                        f"({sorted(FIELD_MAP)} or 'search')")
    for k in FIELD_MAP:
        if k in spec and (not isinstance(spec[k], str) or not spec[k].strip()):
            raise SpecError(f"spec field {k!r} must be a non-empty string")
    if has_raw and (not isinstance(spec["search"], str) or not spec["search"].strip()):
        raise SpecError("'search' must be a non-empty string")


def build_search(spec: dict) -> str:
    """Build the openFDA ``search=`` string for a declarative spec."""
    validate_spec(spec)
    if "search" in spec:
        return spec["search"]
    suffix = ".exact" if spec.get("exact") else ""
    clauses = []
    for key in FIELD_ORDER:
        if key in spec:
            value = spec[key].replace('"', '\\"')
            clauses.append(f'{FIELD_MAP[key]}{suffix}:"{value}"')
    return " AND ".join(clauses)
