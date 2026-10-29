"""Declarative filter specification for ClinicalTrials.gov v2 retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


PHASES = frozenset({"NA", "EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"})
STATUSES = frozenset({
    "ACTIVE_NOT_RECRUITING", "COMPLETED", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING",
    "RECRUITING", "SUSPENDED", "TERMINATED", "WITHDRAWN", "AVAILABLE", "NO_LONGER_AVAILABLE",
    "TEMPORARILY_NOT_AVAILABLE", "APPROVED_FOR_MARKETING", "WITHHELD", "UNKNOWN",
})
STUDY_TYPES = frozenset({"INTERVENTIONAL", "OBSERVATIONAL", "EXPANDED_ACCESS"})
SPONSOR_CLASSES = frozenset({
    "NIH", "FED", "OTHER_GOV", "INDIV", "INDUSTRY", "NETWORK", "AMBIG", "OTHER", "UNKNOWN",
})

_DATE_RE_OK = ("%Y-%m-%d",)


def _check_date(value: str, where: str) -> None:
    import datetime as _dt
    try:
        _dt.datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where}: expected 'YYYY-MM-DD' date, got {value!r}") from exc


@dataclass(frozen=True)
class DateRange:
    """Inclusive date range; either bound may be omitted (None = open)."""
    start: str | None = None
    end: str | None = None

    def validate(self, where: str) -> None:
        if self.start is None and self.end is None:
            raise ValueError(f"{where}: empty date range")
        if self.start is not None:
            _check_date(self.start, where + ".start")
        if self.end is not None:
            _check_date(self.end, where + ".end")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError(f"{where}: start {self.start} > end {self.end}")


@dataclass(frozen=True)
class IntRange:
    """Inclusive integer range; either bound may be omitted (None = open)."""
    min: int | None = None
    max: int | None = None

    def validate(self, where: str) -> None:
        if self.min is None and self.max is None:
            raise ValueError(f"{where}: empty integer range")
        for name, v in (("min", self.min), ("max", self.max)):
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise ValueError(f"{where}.{name}: expected non-negative int, got {v!r}")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"{where}: min {self.min} > max {self.max}")


@dataclass(frozen=True)
class FilterSpec:
    """One declarative retrieval request.

    All filter dimensions are optional; at least one must be set.
    Every dimension in this dataclass is translated to a *server-side* API
    constraint by query.build_query_params (no local post-filtering is needed
    for any of these dimensions).
    """
    spec_id: str
    description: str = ""
    condition: str | None = None
    intervention: str | None = None
    overall_status: tuple[str, ...] = ()
    phase: tuple[str, ...] = ()
    study_type: str | None = None
    enrollment: IntRange | None = None
    primary_completion_date: DateRange | None = None
    first_posted_date: DateRange | None = None
    location_country: str | None = None
    lead_sponsor_class: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FilterSpec":
        d = dict(d)
        f = dict(d.pop("filters", {}))
        spec_id = d.pop("spec_id", d.pop("id", None))
        if not spec_id:
            raise ValueError("spec needs a 'spec_id' (or 'id') key")
        description = d.pop("description", "")

        def rng_date(key: str) -> DateRange | None:
            v = f.pop(key, None)
            if v is None:
                return None
            return DateRange(start=v.get("start"), end=v.get("end"))

        def rng_int(key: str) -> IntRange | None:
            v = f.pop(key, None)
            if v is None:
                return None
            return IntRange(min=v.get("min"), max=v.get("max"))

        spec = cls(
            spec_id=str(spec_id),
            description=str(description),
            condition=f.pop("condition", None),
            intervention=f.pop("intervention", None),
            overall_status=tuple(f.pop("overall_status", []) or []),
            phase=tuple(f.pop("phase", []) or []),
            study_type=f.pop("study_type", None),
            enrollment=rng_int("enrollment"),
            primary_completion_date=rng_date("primary_completion_date"),
            first_posted_date=rng_date("first_posted_date"),
            location_country=f.pop("location_country", None),
            lead_sponsor_class=f.pop("lead_sponsor_class", None),
        )
        if f:
            raise ValueError(f"spec {spec_id}: unknown filter keys {sorted(f)}")
        spec.validate()
        return spec

    def validate(self) -> None:
        sid = self.spec_id
        dims = (self.condition, self.intervention, self.overall_status, self.phase,
                self.study_type, self.enrollment, self.primary_completion_date,
                self.first_posted_date, self.location_country, self.lead_sponsor_class)
        if not any(bool(x) for x in dims):
            raise ValueError(f"spec {sid}: at least one filter dimension must be set")
        for s in self.overall_status:
            if s not in STATUSES:
                raise ValueError(f"spec {sid}: unknown overall_status {s!r}")
        for p in self.phase:
            if p not in PHASES:
                raise ValueError(f"spec {sid}: unknown phase {p!r}")
        if self.study_type is not None and self.study_type not in STUDY_TYPES:
            raise ValueError(f"spec {sid}: unknown study_type {self.study_type!r}")
        if self.lead_sponsor_class is not None and self.lead_sponsor_class not in SPONSOR_CLASSES:
            raise ValueError(f"spec {sid}: unknown lead_sponsor_class {self.lead_sponsor_class!r}")
        if self.enrollment is not None:
            self.enrollment.validate(f"spec {sid}: enrollment")
        if self.primary_completion_date is not None:
            self.primary_completion_date.validate(f"spec {sid}: primary_completion_date")
        if self.first_posted_date is not None:
            self.first_posted_date.validate(f"spec {sid}: first_posted_date")

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable representation (battery.json 'filters' layout)."""
        filters: dict[str, Any] = {}
        if self.condition is not None:
            filters["condition"] = self.condition
        if self.intervention is not None:
            filters["intervention"] = self.intervention
        if self.overall_status:
            filters["overall_status"] = list(self.overall_status)
        if self.phase:
            filters["phase"] = list(self.phase)
        if self.study_type is not None:
            filters["study_type"] = self.study_type
        if self.enrollment is not None:
            filters["enrollment"] = {k: v for k, v in asdict(self.enrollment).items() if v is not None}
        if self.primary_completion_date is not None:
            filters["primary_completion_date"] = {
                k: v for k, v in asdict(self.primary_completion_date).items() if v is not None}
        if self.first_posted_date is not None:
            filters["first_posted_date"] = {
                k: v for k, v in asdict(self.first_posted_date).items() if v is not None}
        if self.location_country is not None:
            filters["location_country"] = self.location_country
        if self.lead_sponsor_class is not None:
            filters["lead_sponsor_class"] = self.lead_sponsor_class
        return {"spec_id": self.spec_id, "description": self.description, "filters": filters}
