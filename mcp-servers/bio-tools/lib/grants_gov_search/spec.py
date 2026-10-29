"""Query specification for the Grants.gov search2 API."""
from __future__ import annotations

from dataclasses import dataclass, field

VALID_STATUSES = ("forecasted", "posted", "closed", "archived")
ALL_STATUSES = "forecasted|posted|closed|archived"
DEFAULT_STATUSES = "forecasted|posted"


def _pipe_join(value) -> str:
    """Accept a str ('a|b'), list/tuple, or None; return the pipe-joined string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return "|".join(str(v) for v in value)


@dataclass(frozen=True)
class GrantsSearchSpec:
    """One frozen search against POST https://api.grants.gov/v1/api/search2.

    All filter dimensions of the upstream API are exposed. ``opp_statuses``
    defaults to the API's own default (forecasted|posted); batteries pin
    ``archived`` for stability. ``sort_by`` defaults to ``oppNum|asc`` so that
    pagination order is deterministic (NB: an *invalid* sortBy value is
    silently accepted by the API and yields empty pages — the client's
    completeness check catches this).
    """

    keyword: str | None = None
    opp_num: str | None = None
    aln: str | None = None
    agencies: str | tuple | list | None = None
    opp_statuses: str = DEFAULT_STATUSES
    eligibilities: str | tuple | list | None = None
    funding_categories: str | tuple | list | None = None
    funding_instruments: str | tuple | list | None = None
    sort_by: str = "oppNum|asc"

    def __post_init__(self):
        if not any([self.keyword, self.opp_num, self.aln, _pipe_join(self.agencies),
                    _pipe_join(self.eligibilities), _pipe_join(self.funding_categories),
                    _pipe_join(self.funding_instruments)]):
            raise ValueError("spec needs at least one search criterion")
        for s in self.opp_statuses.split("|"):
            if s not in VALID_STATUSES:
                raise ValueError(f"invalid oppStatus {s!r}; valid: {VALID_STATUSES}")

    def to_payload(self, rows: int, start_record_num: int = 0) -> dict:
        """Build the JSON body for one POST."""
        payload = {
            "rows": rows,
            "startRecordNum": start_record_num,
            "oppStatuses": self.opp_statuses,
            "sortBy": self.sort_by,
        }
        if self.keyword:
            payload["keyword"] = self.keyword
        if self.opp_num:
            payload["oppNum"] = self.opp_num
        if self.aln:
            payload["cfda"] = self.aln
        for key, val in (("agencies", self.agencies),
                         ("eligibilities", self.eligibilities),
                         ("fundingCategories", self.funding_categories),
                         ("fundingInstruments", self.funding_instruments)):
            joined = _pipe_join(val)
            if joined:
                payload[key] = joined
        return payload

    @classmethod
    def from_dict(cls, d: dict) -> "GrantsSearchSpec":
        return cls(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items()})
