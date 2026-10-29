"""OpenAlexWorks — high-level retrieval over the OpenAlex REST API.

All listings are honest: ``api_total`` always carries OpenAlex's own
``meta.count`` and ``records_truncated`` flags any capped walk — silent
truncation is impossible. Page walks are bounded by the caller's cap
(pages of up to 200, paced by the client).
"""
from __future__ import annotations

from .client import NotFound, OpenAlexClient
from .records import (lean_author, lean_source, lean_work,
                      normalize_author_id, normalize_source_id,
                      normalize_work_id)

PAGE_SIZE = 200
MAX_RECORDS_CEILING = 500
BATCH_FILTER_SIZE = 50

_SORT_MAP = {
    "relevance": "relevance_score:desc",
    "cited_by_count": "cited_by_count:desc",
    "publication_date": "publication_date:desc",
}


class OpenAlexWorks:
    def __init__(self, client: OpenAlexClient | None = None):
        self.client = client or OpenAlexClient()



    def _list(self, path: str, params: dict, max_records: int) -> dict:
        """Bounded page walk; returns {api_total, results(raw), truncated}.

        Invariant: ``per-page`` is held constant for the whole walk —
        OpenAlex computes a page's window as ``(page-1) * per_page``, so
        varying it mid-walk misaligns pages (re-fetches + silent gaps).
        The final page may overshoot the cap; we slice afterwards.
        """
        cap = max(1, min(int(max_records), MAX_RECORDS_CEILING))
        per_page = min(PAGE_SIZE, cap)
        results: list[dict] = []
        api_total = 0
        page = 1
        while len(results) < cap:
            q = dict(params)
            q["per-page"] = per_page
            q["page"] = page
            payload = self.client.get(path, q)
            api_total = (payload.get("meta") or {}).get("count", 0)
            rows = payload.get("results") or []
            results.extend(rows)
            if not rows or len(results) >= api_total:
                break
            page += 1
        results = results[:cap]
        return {"api_total": api_total, "results": results,
                "truncated": api_total > len(results)}

    @staticmethod
    def _sort_param(sort: str, has_search: bool) -> str | None:
        if sort not in _SORT_MAP:
            raise ValueError(
                f"unknown sort {sort!r} — one of {sorted(_SORT_MAP)}")
        if sort == "relevance":


            return _SORT_MAP[sort] if has_search else None
        return _SORT_MAP[sort]

    def _resolve_doi_work(self, doi_alias: str) -> tuple[dict, list[dict]]:
        """Resolve ``doi:...`` via the FILTER route; return (raw work,
        claimants).

        The alias route (``/works/doi:...``) arbitrarily picks ONE claimant
        when upstream has wrongly tagged several works with the same DOI —
        observed live: ``10.1056/NEJMoa2001017`` alias-resolved to a
        0-citation garbage record while the filter route also returned the
        real 30k-citation paper. The filter route exposes every claimant;
        we pick the most-cited deterministically (merged-dupe garbage
        records are near-zero) and surface the full claimant list.
        """
        doi = doi_alias[4:]
        payload = self.client.get("/works", {
            "filter": f"doi:{doi}", "per-page": PAGE_SIZE,
            "sort": "cited_by_count:desc"})
        results = payload.get("results") or []
        if not results:
            raise NotFound(f"no OpenAlex work has doi:{doi}")
        results.sort(key=lambda w: (-(w.get("cited_by_count") or 0),
                                    w.get("id") or ""))
        claimants = [{
            "openalex_id": (w.get("id") or "").rsplit("/", 1)[-1] or None,
            "title": w.get("title") or w.get("display_name"),
            "publication_year": w.get("publication_year"),
            "cited_by_count": w.get("cited_by_count"),
        } for w in results]
        return results[0], claimants

    @staticmethod
    def _ambiguity_note(doi_alias: str, claimants: list[dict]) -> str:
        return (f"{len(claimants)} OpenAlex works claim {doi_alias}; "
                f"selected the most-cited ({claimants[0]['openalex_id']}) — "
                f"see doi_claimants for the alternatives")



    def search_works(self, query: str | None = None,
                     year_from: int | None = None,
                     year_to: int | None = None,
                     work_type: str | None = None,
                     open_access_only: bool = False,
                     venue: str | None = None,
                     sort: str = "relevance",
                     max_records: int = 50,
                     include_abstracts: bool = False) -> dict:
        filters: list[str] = []
        if year_from is not None and year_to is not None:
            filters.append(f"publication_year:{int(year_from)}-{int(year_to)}")
        elif year_from is not None:
            filters.append(f"publication_year:>{int(year_from) - 1}")
        elif year_to is not None:
            filters.append(f"publication_year:<{int(year_to) + 1}")
        if work_type:
            filters.append(f"type:{work_type}")
        if open_access_only:
            filters.append("open_access.is_oa:true")
        venue_resolved: dict | None = None
        if venue:
            try:
                sid = normalize_source_id(venue)
            except ValueError:


                hits = self.search_sources(venue, max_records=1)
                if not hits["records"]:
                    raise ValueError(
                        f"no OpenAlex source matches venue {venue!r} — "
                        f"pass an S-id or ISSN (see openalex_venue_info)")
                top = hits["records"][0]
                sid = top["source_id"]
                venue_resolved = {
                    "input": venue, "source_id": sid,
                    "display_name": top["display_name"],
                    "candidates_total": hits["api_total"]}
            if sid.startswith("issn:"):
                filters.append(f"primary_location.source.issn:{sid[5:]}")
            else:
                filters.append(f"primary_location.source.id:{sid}")
        if not query and not filters:
            raise ValueError("pass a query and/or at least one filter")
        params: dict = {}
        if query:
            params["search"] = query
        if filters:
            params["filter"] = ",".join(filters)
        sort_param = self._sort_param(sort, has_search=bool(query))
        if sort_param:
            params["sort"] = sort_param
        out = self._list("/works", params, max_records)
        records = [lean_work(w, with_abstract=include_abstracts)
                   for w in out["results"]]
        result = {"query": query, "filters": filters, "sort": sort,
                  "api_total": out["api_total"],
                  "n_records_returned": len(records),
                  "records_truncated": out["truncated"], "records": records}
        if venue_resolved:
            result["venue_resolved"] = venue_resolved
        return result

    def get_work(self, work_id: str) -> dict:
        wid = normalize_work_id(work_id)
        claimants: list[dict] = []
        if wid.startswith("doi:"):
            raw, claimants = self._resolve_doi_work(wid)
        else:
            raw = self.client.get(f"/works/{wid}")
        rec = lean_work(raw, with_abstract=True)
        rec["referenced_works"] = [r.rsplit("/", 1)[-1]
                                   for r in raw.get("referenced_works") or []]
        rec["counts_by_year"] = raw.get("counts_by_year")
        if len(claimants) > 1:
            rec["doi_claimants"] = claimants
            rec["doi_resolution_note"] = self._ambiguity_note(wid, claimants)
        return rec

    def citations(self, work_id: str, sort: str = "cited_by_count",
                  max_records: int = 50,
                  include_abstracts: bool = False) -> dict:
        wid = normalize_work_id(work_id)
        claimants: list[dict] = []
        if wid.startswith("doi:"):



            doi_alias = wid
            raw, claimants = self._resolve_doi_work(wid)
            wid = raw["id"].rsplit("/", 1)[-1]
        params: dict = {"filter": f"cites:{wid}"}
        sort_param = self._sort_param(sort, has_search=False)
        if sort_param:
            params["sort"] = sort_param
        out = self._list("/works", params, max_records)
        records = [lean_work(w, with_abstract=include_abstracts)
                   for w in out["results"]]
        result = {"work_id": wid, "api_total": out["api_total"],
                  "n_records_returned": len(records),
                  "records_truncated": out["truncated"], "records": records}
        if len(claimants) > 1:
            result["doi_claimants"] = claimants
            result["doi_resolution_note"] = self._ambiguity_note(
                doi_alias, claimants)
        return result

    def references(self, work_id: str, max_records: int = 100) -> dict:
        wid = normalize_work_id(work_id)
        claimants: list[dict] = []
        if wid.startswith("doi:"):
            raw, claimants = self._resolve_doi_work(wid)
        else:
            raw = self.client.get(
                f"/works/{wid}", {"select": "id,referenced_works"})
        src_id = raw["id"].rsplit("/", 1)[-1]
        ref_ids = [r.rsplit("/", 1)[-1]
                   for r in raw.get("referenced_works") or []]
        cap = max(1, min(int(max_records), MAX_RECORDS_CEILING))
        to_hydrate = ref_ids[:cap]
        records: list[dict] = []
        for i in range(0, len(to_hydrate), BATCH_FILTER_SIZE):
            batch = to_hydrate[i:i + BATCH_FILTER_SIZE]
            payload = self.client.get("/works", {
                "filter": "openalex:" + "|".join(batch),
                "per-page": len(batch)})
            records.extend(lean_work(w, with_abstract=False)
                           for w in payload.get("results") or [])
        got = {r["openalex_id"] for r in records}
        not_hydrated = [i for i in to_hydrate if i not in got]
        order = {w: i for i, w in enumerate(to_hydrate)}
        records.sort(key=lambda r: order.get(r["openalex_id"], len(order)))
        result = {"work_id": src_id, "n_references": len(ref_ids),
                  "n_records_returned": len(records),
                  "records_truncated": len(ref_ids) > len(to_hydrate),
                  "references_not_hydrated": not_hydrated,
                  "reference_ids": ref_ids, "records": records}
        if len(claimants) > 1:
            result["doi_claimants"] = claimants
            result["doi_resolution_note"] = self._ambiguity_note(
                wid, claimants)
        return result



    def search_authors(self, query: str, max_records: int = 25) -> dict:
        out = self._list("/authors", {"search": query}, max_records)
        records = [lean_author(a) for a in out["results"]]
        return {"query": query, "api_total": out["api_total"],
                "n_records_returned": len(records),
                "records_truncated": out["truncated"], "records": records}

    def get_author(self, author_id: str, works_sample: int = 10) -> dict:
        aid = normalize_author_id(author_id)
        raw = self.client.get(f"/authors/{aid}")
        rec = lean_author(raw)
        rec["counts_by_year"] = raw.get("counts_by_year")
        n = max(0, min(int(works_sample), PAGE_SIZE))
        if n:
            short = rec["author_id"]
            payload = self.client.get("/works", {
                "filter": f"author.id:{short}",
                "sort": "cited_by_count:desc", "per-page": n})
            rec["top_works_total"] = (payload.get("meta") or {}).get("count")
            rec["top_works"] = [lean_work(w, with_abstract=False)
                                for w in payload.get("results") or []]
        return rec



    def search_sources(self, query: str, max_records: int = 10) -> dict:
        out = self._list("/sources", {"search": query}, max_records)
        records = [lean_source(s) for s in out["results"]]
        return {"query": query, "api_total": out["api_total"],
                "n_records_returned": len(records),
                "records_truncated": out["truncated"], "records": records}

    def get_source(self, source_id: str) -> dict:
        sid = normalize_source_id(source_id)
        raw = self.client.get(f"/sources/{sid}")
        rec = lean_source(raw)
        rec["counts_by_year"] = raw.get("counts_by_year")
        return rec
