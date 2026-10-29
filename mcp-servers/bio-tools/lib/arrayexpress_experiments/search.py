"""Declarative search over the ArrayExpress collection of BioStudies, with complete pagination.

The ArrayExpress collection endpoint is ``GET /api/v1/arrayexpress/search``. Key behaviours
(observed against the live API, 2026-05-30):

* ``pageSize`` is capped at 100 by the server (larger values are silently clamped).
* ``totalHits`` is only guaranteed exact when the result is sorted by a concrete field
  (e.g. ``sortBy=release_date``). Under the default relevance sort, large result sets report
  ``isTotalHitsExact: false`` and the number can be wildly wrong in either direction
  (e.g. ``query=cancer``: 1,290 approximate vs 15,505 exact).
* facet filters are plain query parameters named ``facet.<name>`` with lower-cased values
  (e.g. ``facet.organism=homo sapiens``, ``facet.study_type=chip-seq``).
* date ranges use the Lucene-style query syntax ``release_date:[YYYY-MM-DD TO YYYY-MM-DD]``.

``search_experiments`` therefore always sorts by ``release_date`` (descending by default),
walks every page, verifies the retrieved row count against ``totalHits``, and de-duplicates
on accession (defensively; no duplicates have been observed under field sorting).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import BioStudiesClient, BioStudiesError

PAGE_SIZE_MAX = 100


SEARCH_RECORD_FIELDS = [
    "accession",
    "title",
    "release_date",
    "files",
    "links",
    "is_public",
]


@dataclass
class SearchSpec:
    """Declarative search specification for ArrayExpress experiments.

    Attributes
    ----------
    query:
        Free-text query (BioStudies/Lucene syntax). Optional.
    organism:
        Organism facet value, e.g. ``"Homo sapiens"`` (case-insensitive; the API indexes
        facet values in lower case and this class lower-cases them for you).
    study_type:
        Experiment/study type facet value, e.g. ``"RNA-seq of coding RNA from single cells"``,
        ``"ChIP-seq"``, ``"transcription profiling by array"``.
    technology:
        Technology facet value: ``"array assay"`` or ``"sequencing assay"``.
    released_after / released_before:
        Inclusive ISO dates (``YYYY-MM-DD``); translated into a
        ``release_date:[after TO before]`` range clause (open ends use ``*``).
    extra_facets:
        Any additional ``facet.<name> -> value`` pairs, passed through verbatim.
    sort_order:
        ``"descending"`` (default) or ``"ascending"`` — applied to release_date.
    """

    query: str | None = None
    organism: str | None = None
    study_type: str | None = None
    technology: str | None = None
    released_after: str | None = None
    released_before: str | None = None
    extra_facets: dict[str, str] = field(default_factory=dict)
    sort_order: str = "descending"

    def to_params(self) -> dict[str, str]:
        """Translate the spec into BioStudies query parameters (excluding paging params)."""
        params: dict[str, str] = {}
        clauses: list[str] = []
        if self.query:
            clauses.append(self.query)
        if self.released_after or self.released_before:
            lo = self.released_after or "*"
            hi = self.released_before or "*"
            clauses.append(f"release_date:[{lo} TO {hi}]")
        if clauses:
            params["query"] = " AND ".join(f"({c})" if " AND " in c or " OR " in c else c
                                           for c in clauses)
        if self.organism:
            params["facet.organism"] = self.organism.lower()
        if self.study_type:
            params["facet.study_type"] = self.study_type.lower()
        if self.technology:
            params["facet.technology"] = self.technology.lower()
        for k, v in sorted(self.extra_facets.items()):
            key = k if k.startswith("facet.") else f"facet.{k}"
            params[key] = v.lower()

        params["sortBy"] = "release_date"
        params["sortOrder"] = self.sort_order
        return params


def _hit_to_record(hit: dict[str, Any]) -> dict[str, Any]:
    """Normalize one search hit into a stable, minimal record."""
    return {
        "accession": hit.get("accession"),
        "title": hit.get("title"),
        "release_date": hit.get("release_date"),
        "files": hit.get("files"),
        "links": hit.get("links"),
        "is_public": hit.get("isPublic"),
    }


def search_experiments(
    spec: SearchSpec,
    client: BioStudiesClient | None = None,
    page_size: int = PAGE_SIZE_MAX,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Run a declarative search and retrieve **every** matching experiment.

    Returns a dict::

        {
          "total_hits": int,            # the API's totalHits (exact, field-sorted)
          "is_total_exact": bool,       # the API's isTotalHitsExact flag
          "records": [ {accession, title, release_date, files, links, is_public}, ... ],
          "params": {...},              # the query parameters actually sent (paging excluded)
        }

    ``records`` are de-duplicated by accession and returned in a deterministic order:
    release_date in the requested direction, accession (ascending) as the tie-break — the
    server's own release_date sort is not stable for same-date ties. The function raises
    ``BioStudiesError`` if the number of unique retrieved accessions does not equal
    ``total_hits`` (unless ``max_records`` truncated the walk).
    """
    own_client = client is None
    client = client or BioStudiesClient()
    params = spec.to_params()
    page_size = min(page_size, PAGE_SIZE_MAX)











    page_size_schedule = [page_size,
                          max(2, page_size - 3),
                          max(2, page_size - 11)]
    last_error: BioStudiesError | None = None
    for sweep_attempt, page_size in enumerate(page_size_schedule):
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 1
        total_hits: int | None = None
        is_exact: bool | None = None
        truncated = False

        while True:
            page_params = dict(params, pageSize=page_size, page=page)
            data = client.get_json("arrayexpress/search", page_params)
            if total_hits is None:
                total_hits = int(data.get("totalHits", 0))
                is_exact = bool(data.get("isTotalHitsExact", False))
            hits = data.get("hits") or []
            for hit in hits:
                acc = hit.get("accession")
                if acc and acc not in seen:
                    seen.add(acc)
                    records.append(_hit_to_record(hit))
                    if max_records is not None and len(records) >= max_records:
                        truncated = True
                        break
            if truncated or not hits or len(records) >= total_hits:
                break
            page += 1

        if truncated or total_hits is None or len(records) == total_hits:
            last_error = None
            break
        last_error = BioStudiesError(
            f"Pagination mismatch: retrieved {len(records)} unique accessions "
            f"but the API reported totalHits={total_hits} (params={params}, "
            f"sweep_attempt={sweep_attempt + 1})"
        )

    if last_error is not None:
        raise last_error





    records.sort(key=lambda r: (r.get("accession") or ""))
    records.sort(key=lambda r: (r.get("release_date") or ""),
                 reverse=spec.sort_order != "ascending")

    return {
        "total_hits": total_hits or 0,
        "is_total_exact": is_exact,
        "records": records,
        "params": params,
        "truncated": truncated,
    }
