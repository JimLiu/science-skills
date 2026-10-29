"""Declarative query specs for the openFDA Drugs@FDA endpoint.

A :class:`SearchSpec` is translated into the endpoint's exact ``search=``
field syntax (Elasticsearch-flavoured phrase queries). Field names were
verified live against ``api.fda.gov/drug/drugsfda.json`` on 2026-06-08:

- ``sponsor_name`` and ``application_number`` are keyword-mapped (no
  ``.exact`` sub-field exists for them; sorting/counting uses the raw name).
- product attributes live under ``products.*`` (``brand_name``,
  ``active_ingredients.name``, ``marketing_status``, ``dosage_form``,
  ``route``); ``.exact`` sub-fields exist for ``products.dosage_form``,
  ``products.route`` and the ``openfda.pharm_class_*`` fields.
- the harmonized ``openfda`` block (generic_name, pharm_class_*, unii, ...)
  is absent on many older records — queries on ``openfda.*`` silently skip
  those applications (documented in the README).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Optional

APP_NUMBER_RE = re.compile(r"^(NDA|ANDA|BLA)\d{6}$")

VALID_SEARCH_TYPES = ("and", "or")




DATE_CAP_FIELD = "submissions.submission_status_date"


def _phrase(field_name: str, value: str) -> str:
    """Render one exact-phrase clause, quoting the value."""
    cleaned = str(value).strip().replace('"', "")
    return f'{field_name}:"{cleaned}"'


@dataclass(frozen=True)
class SearchSpec:
    """Declarative Drugs@FDA application search.

    All text filters are exact-phrase matches (Elasticsearch phrase
    semantics on analyzed fields, exact term on keyword fields).
    ``search_type`` joins the provided clauses with AND (default) or OR.
    ``submission_date_from``/``submission_date_to`` constrain
    ``submissions.submission_status_date`` and are the recommended way to
    freeze a spec against upstream growth (always ANDed, even when
    ``search_type='or'``).
    """

    brand: Optional[str] = None
    generic: Optional[str] = None
    active_ingredient: Optional[str] = None
    sponsor: Optional[str] = None
    marketing_status: Optional[str] = None
    dosage_form: Optional[str] = None
    route: Optional[str] = None
    application_number: Optional[str] = None
    pharm_class: Optional[str] = None
    pharm_class_type: str = "epc"
    search_type: str = "and"
    submission_date_from: Optional[str] = None
    submission_date_to: Optional[str] = None
    raw: Optional[str] = None

    _FIELD_MAP = (
        ("brand", "products.brand_name"),
        ("generic", "openfda.generic_name"),
        ("active_ingredient", "products.active_ingredients.name"),
        ("sponsor", "sponsor_name"),
        ("marketing_status", "products.marketing_status"),
        ("dosage_form", "products.dosage_form"),
        ("route", "products.route"),
        ("application_number", "application_number"),
    )

    def __post_init__(self) -> None:
        if self.search_type not in VALID_SEARCH_TYPES:
            raise ValueError(f"search_type must be one of {VALID_SEARCH_TYPES}")
        if self.pharm_class_type not in ("epc", "moa", "cs", "pe"):
            raise ValueError("pharm_class_type must be epc|moa|cs|pe")
        if self.application_number is not None and not APP_NUMBER_RE.match(
            self.application_number
        ):
            raise ValueError(
                "application_number must look like NDA/ANDA/BLA + 6 digits, "
                f"got {self.application_number!r}"
            )

    def clauses(self) -> list[str]:
        out = []
        for attr, fld in self._FIELD_MAP:
            value = getattr(self, attr)
            if value is not None:
                out.append(_phrase(fld, value))
        if self.pharm_class is not None:
            out.append(
                _phrase(f"openfda.pharm_class_{self.pharm_class_type}", self.pharm_class)
            )
        return out

    def to_search(self) -> str:
        """Render the openFDA ``search=`` string."""
        if self.raw is not None:
            return self.raw
        clauses = self.clauses()
        if not clauses and not (self.submission_date_from or self.submission_date_to):
            raise ValueError("empty SearchSpec: provide at least one filter")
        joiner = " AND " if self.search_type == "and" else " OR "
        body = joiner.join(clauses)
        if self.search_type == "or" and len(clauses) > 1:
            body = f"({body})"
        if self.submission_date_from or self.submission_date_to:
            lo = _to_yyyymmdd(self.submission_date_from) or "19000101"
            hi = _to_yyyymmdd(self.submission_date_to) or "30000101"
            date_clause = f"{DATE_CAP_FIELD}:[{lo} TO {hi}]"
            body = f"{body} AND {date_clause}" if body else date_clause
        return body

    @classmethod
    def from_dict(cls, d: dict) -> "SearchSpec":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown SearchSpec keys: {sorted(unknown)}")
        return cls(**d)


def _to_yyyymmdd(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.replace("-", "")
    if not re.match(r"^\d{8}$", v):
        raise ValueError(f"date must be YYYY-MM-DD or YYYYMMDD, got {value!r}")
    return v
