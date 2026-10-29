"""gwas-catalog: honest (count-verified or explicitly capped) retrieval from
the NHGRI-EBI GWAS Catalog REST API v2.

Paged list routes (/associations, /studies, /efo-traits) expose the API's own
``page.totalElements``; every walk here either completes and verifies the
retrieved row count against that total (mismatch raises ``CountMismatch``)
or stops at the caller's cap and reports ``truncated=True`` alongside
``api_total`` — silent truncation is impossible.

Association walks are server-sorted by p-value ascending, so a capped result
is the "most significant first" prefix of the full set.
"""
from __future__ import annotations

from typing import Any

from mcp_servers_common.pathseg import seg as _seg

from .client import GwasClient

PAGE_SIZE = 500
MAX_PAGES = 8









ALLOWED_FILTERS: dict[str, frozenset[str]] = {
    "/associations": frozenset({"rs_id", "mapped_gene", "efo_id", "efo_trait"}),
    "/studies": frozenset({"efo_id", "efo_trait", "pubmed_id"}),
    "/efo-traits": frozenset({"trait"}),
}





WHOLE_TABLE_GUARDED = frozenset({"/associations", "/studies"})


class CountMismatch(RuntimeError):
    """Retrieved row count disagrees with the API-reported total."""


class FilterIgnored(RuntimeError):
    """A filter was sent but the API returned ~the whole table — the v2 API
    silently ignores unrecognized params, so the filter was not honored."""


def _lean_trait_list(traits: list | None) -> list[dict[str, Any]]:
    return [{"efo_id": t.get("efo_id"), "efo_trait": t.get("efo_trait")}
            for t in (traits or [])]


def flatten_association(rec: dict) -> dict[str, Any]:
    """Lean association row: stats + traits + study/variant anchors."""
    return {
        "association_id": rec.get("association_id"),
        "p_value": rec.get("p_value"),
        "pvalue_mantissa": rec.get("pvalue_mantissa"),
        "pvalue_exponent": rec.get("pvalue_exponent"),
        "pvalue_description": rec.get("pvalue_description") or None,
        "or_value": rec.get("or_per_copy_num"),
        "beta": None if rec.get("beta") in (None, "-") else rec.get("beta"),
        "ci_lower": rec.get("ci_lower"),
        "ci_upper": rec.get("ci_upper"),
        "range": None if rec.get("range") in (None, "-") else rec.get("range"),
        "risk_frequency": rec.get("risk_frequency"),
        "snp_effect_alleles": rec.get("snp_effect_allele") or [],
        "rs_ids": [a.get("rs_id") for a in rec.get("snp_allele") or []],
        "locations": rec.get("locations") or [],
        "mapped_genes": rec.get("mapped_genes") or [],
        "efo_traits": _lean_trait_list(rec.get("efo_traits")),
        "bg_efo_traits": _lean_trait_list(rec.get("bg_efo_traits")),
        "reported_trait": rec.get("reported_trait") or [],
        "multi_snp_haplotype": rec.get("multi_snp_haplotype"),
        "snp_interaction": rec.get("snp_interaction"),
        "study_accession_id": rec.get("accession_id"),
        "pubmed_id": rec.get("pubmed_id"),
        "first_author": rec.get("first_author"),
    }


def flatten_study(rec: dict) -> dict[str, Any]:
    """Lean study row."""
    return {
        "accession_id": rec.get("accession_id"),
        "disease_trait": rec.get("disease_trait"),
        "efo_traits": _lean_trait_list(rec.get("efo_traits")),
        "bg_efo_traits": _lean_trait_list(rec.get("bg_efo_traits")),
        "pubmed_id": rec.get("pubmed_id"),
        "initial_sample_size": rec.get("initial_sample_size"),
        "replication_sample_size": rec.get("replication_sample_size"),
        "discovery_ancestry": rec.get("discovery_ancestry") or [],
        "replication_ancestry": rec.get("replication_ancestry") or [],
        "genotyping_technologies": rec.get("genotyping_technologies") or [],
        "platforms": rec.get("platforms"),
        "cohort": rec.get("cohort") or [],
        "full_summary_stats_available": rec.get("full_summary_stats_available"),
        "imputed": rec.get("imputed"),
        "gxe": rec.get("gxe"),
    }


def flatten_snp(rec: dict) -> dict[str, Any]:
    """Lean SNP record."""
    return {
        "rs_id": rec.get("rs_id"),
        "merged": rec.get("merged"),
        "functional_class": rec.get("functional_class"),
        "most_severe_consequence": rec.get("most_severe_consequence"),
        "alleles": rec.get("alleles"),
        "mapped_genes": rec.get("mapped_genes") or [],
        "locations": [{"chromosome": loc.get("chromosome_name"),
                       "position": loc.get("chromosome_position"),
                       "region": (loc.get("region") or {}).get("name")}
                      for loc in rec.get("locations") or []],
        "last_update_date": rec.get("last_update_date"),
    }


def flatten_efo_trait(rec: dict) -> dict[str, Any]:
    return {"efo_id": rec.get("efo_id"),
            "efo_trait": rec.get("efo_trait"),
            "uri": rec.get("uri")}


class GwasCatalog:
    """High-level GWAS Catalog v2 interface with honest page walks."""

    def __init__(self, client: GwasClient | None = None,
                 page_size: int = PAGE_SIZE):
        self.client = client or GwasClient()
        self.page_size = page_size
        self._route_totals: dict[str, int] = {}



    def _route_total(self, path: str) -> int:
        """Unfiltered table size for a route (1-row probe, cached per
        instance) — the reference for the whole-table tripwire. Returns 0
        (UNcached) when the probe body lacks a positive ``totalElements``, so
        a malformed/empty probe never poisons the tripwire: caching 0 would
        make ``api_total >= 0.9 * 0`` always true and raise FilterIgnored for
        every filtered query for the life of this lru_cache singleton.
        Callers skip the guard when this is <= 0."""
        if path not in self._route_totals:
            payload = self.client.get_json(path, params={"size": 1, "page": 0})
            total = (payload.get("page") or {}).get("totalElements", 0)
            if not isinstance(total, int) or total <= 0:
                return 0
            self._route_totals[path] = total
        return self._route_totals[path]

    def _walk(self, path: str, embed_key: str, params: dict[str, Any],
              max_records: int, sort: dict[str, str] | None = None
              ) -> tuple[list[dict], int, bool]:
        """Walk a paged v2 route. Returns (rows, api_total, truncated).

        Filter names must be on the live-verified ``ALLOWED_FILTERS`` list
        (the v2 API silently ignores unknown params). On identifier-filtered
        routes, a filtered total approaching the unfiltered table size
        raises ``FilterIgnored`` instead of returning the globally
        top-significant rows as false matches. When the walk completes (not
        capped), the row count is verified against the API's own total; a
        mismatch raises CountMismatch.
        """
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        allowed = ALLOWED_FILTERS.get(path, frozenset())
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(
                f"filter(s) {sorted(unknown)} are not on the live-verified "
                f"allowlist for {path} ({sorted(allowed)}); the v2 API "
                f"silently ignores unknown params")


        size = min(self.page_size, max_records)
        rows: list[dict] = []
        api_total = 0
        page = 0
        while True:
            q = dict(params)
            q.update({"size": size, "page": page})
            if sort:
                q.update(sort)
            payload = self.client.get_json(path, params=q)
            api_total = (payload.get("page") or {}).get("totalElements", 0)
            route_total = self._route_total(path)
            if page == 0 and params and path in WHOLE_TABLE_GUARDED \
                    and route_total > 0 and api_total >= 0.9 * route_total:
                raise FilterIgnored(
                    f"{path} {params}: the filtered total ({api_total}) is "
                    f"~the whole table ({route_total}) — the "
                    f"API ignored the filter; refusing to return unrelated "
                    f"rows as matches")
            batch = (payload.get("_embedded") or {}).get(embed_key, [])
            rows.extend(batch)
            wanted = min(api_total, max_records)
            if len(rows) >= wanted:
                break
            if not batch:


                raise CountMismatch(
                    f"{path} {params}: walked {len(rows)} rows but the API "
                    f"reports totalElements={api_total}")
            if page >= MAX_PAGES - 1:
                break
            page += 1
        kept = rows[:max_records]
        truncated = api_total > len(kept)
        if not truncated and len(kept) != api_total:
            raise CountMismatch(
                f"{path} {params}: walked {len(kept)} rows but the API "
                f"reports totalElements={api_total}")
        return kept, api_total, truncated



    def associations(self, filters: dict[str, Any], max_records: int
                     ) -> dict[str, Any]:
        """Association rows for exactly the given known-good v2 filters,
        server-sorted by p-value ascending."""
        rows, total, truncated = self._walk(
            "/associations", "associations", filters, max_records,
            sort={"sort": "p_value", "direction": "asc"})
        return {"api_total": total, "returned": len(rows),
                "truncated": truncated,
                "associations": [flatten_association(r) for r in rows]}



    def studies(self, filters: dict[str, Any], max_records: int
                ) -> dict[str, Any]:
        rows, total, truncated = self._walk(
            "/studies", "studies", filters, max_records)
        return {"api_total": total, "returned": len(rows),
                "truncated": truncated,
                "studies": [flatten_study(r) for r in rows]}

    def study(self, accession_id: str) -> dict[str, Any]:
        return flatten_study(
            self.client.get_json(f"/studies/{_seg(accession_id)}"))

    def snp(self, rs_id: str) -> dict[str, Any]:
        return flatten_snp(self.client.get_json(
            f"/single-nucleotide-polymorphisms/{_seg(rs_id)}"))

    def search_traits(self, query: str, max_records: int) -> dict[str, Any]:
        rows, total, truncated = self._walk(
            "/efo-traits", "efo_traits", {"trait": query}, max_records)
        out = [flatten_efo_trait(r) for r in rows]
        out.sort(key=lambda t: (t["efo_trait"] or "", t["efo_id"] or ""))
        return {"api_total": total, "returned": len(out),
                "truncated": truncated, "efo_traits": out}
