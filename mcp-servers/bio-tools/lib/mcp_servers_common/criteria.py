"""Empty-criterion hygiene for search tools (operon-authored NEW module —
register like gate.py/ua.py in VENDOR.json; a re-vendor's rmtree of
mcp_servers_common deletes it and must re-add).

The bug class (stress battery root frame df13ada4): an explicitly-provided,
blank string criterion reaching upstream as an empty filter matches
EVERYTHING — e.g. the hosted ChEMBL connector's compound_search(name="")
returned a ~28KB match-all page because Django-style `…icontains=""`
matches the whole table — or is dropped by a truthiness guard so the query
silently widens to an unfiltered listing/walk.

The rule these helpers implement: a provided-but-blank criterion never
silently widens a query. What happens instead follows each server's own
convention — tier-2 servers raise (matching their guarded siblings, e.g.
pubmed's "empty search term" / openalex_search_works' empty-query raise),
tier-1 drop-ins may return their zero-result shape instead (mcp_chembl).
Absent (None) optional criteria keep their existing, often documented,
browse semantics.
"""

from __future__ import annotations

__all__ = ["blank", "none_if_blank", "reject_blank"]


def blank(value: object) -> bool:
    """True for a provided-but-blank string criterion ("" or whitespace)."""
    return isinstance(value, str) and not value.strip()


def none_if_blank(value):
    """Treat a blank string criterion as not provided (documented-browse
    tools where the blank would otherwise mean something MORE than the
    documented omit shape)."""
    return None if blank(value) else value


def reject_blank(_mapping=None, **criteria) -> None:
    """Raise ValueError naming every blank criterion.

    Call with the tool's provided string criteria as keywords; None and
    non-blank values pass through silently. Deliberately strict: it raises
    on ANY provided-blank criterion even when a usable sibling criterion
    exists (the tier-1 drop-ins instead proceed on the usable one to stay
    closest to hosted-connector behavior). Free-form filter dicts
    (user-controlled keys, e.g. an `extra_filters` param) go in the
    positional `_mapping` — passing them as `**kwargs` would TypeError on
    a key that collides with a named criterion.
    """
    bad = sorted({k for src in (_mapping or {}, criteria)
                  for k, v in src.items() if blank(v)})
    if bad:
        raise ValueError(
            f"{', '.join(bad)} must be non-empty when provided — "
            "an empty search criterion is not a search (omit the parameter "
            "for the tool's documented default, or provide a value)")
