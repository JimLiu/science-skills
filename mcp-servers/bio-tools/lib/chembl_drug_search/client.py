"""HTTP client for ChEMBL compound search and indication-based drug search.

Routes used (base ``https://www.ebi.ac.uk/chembl/api/data``):

* ``/molecule.json`` -- exact name lookup (``pref_name__iexact``, synonym
  fallback) and batched ``molecule_chembl_id__in`` record fetches;
* ``/molecule/search.json`` -- full-text name search (the route the naive
  MCP-shaped pattern uses; exposed for completeness, with its first-hit
  pitfalls documented);
* ``/similarity/{smiles}/{threshold}.json`` -- Tanimoto similarity search;
* ``/substructure/{smiles}.json`` -- substructure search;
* ``/drug_indication.json`` -- indication rows (EFO / MeSH term match);
* ``/drug_warning.json`` -- warning records (withdrawal, black box, ...).

Conventions shared with the sibling tools chembl-bioactivity / chembl-targets:
``limit=1000`` paging verified against ``page_meta.total_count``;
deterministic output ordering (client-side -- the similarity route rejects
``order_by`` with HTTP 400); bounded retries with backoff honouring
Retry-After; a politeness delay (default 0.21 s) keeping a single client
under the shared EBI 5 req/s budget; request/byte instrumentation.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Iterable

import requests

from .records import sort_by_key, sort_similarity_records, summarize_warnings
from mcp_servers_common.ratelimit import retry_after_seconds

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/chembl/api/data"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_MIN_INTERVAL_S = 0.21
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RETRIES = 4
DEFAULT_BATCH_SIZE = 50
USER_AGENT = "chembl-drug-search/0.1.0 (bio-tools MCP-rebuild wave)"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


DRUG_FIELDS = (
    "molecule_chembl_id,pref_name,max_phase,first_approval,"
    "withdrawn_flag,black_box_warning,molecule_type,molecule_hierarchy"
)


class ChemblDrugSearchError(RuntimeError):
    """Base error for chembl-drug-search."""


class MoleculeNotFoundError(ChemblDrugSearchError):
    """A requested molecule ChEMBL ID does not exist upstream."""


class PaginationError(ChemblDrugSearchError):
    """A paged retrieval did not return page_meta.total_count records."""


class ChemblDrugSearchClient:
    """ChEMBL compound / drug search client with verified pagination."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_s: float = 1.0,
        page_size: int = DEFAULT_PAGE_SIZE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept", "application/json")
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.page_size = page_size
        self.batch_size = batch_size
        self.timeout_s = timeout_s
        self.request_count = 0
        self.bytes_downloaded = 0
        self._last_request_t = 0.0



    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
        if wait > 0:
            time.sleep(wait)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``base_url + path``; throttled, retried, instrumented."""
        url = f"{self.base_url}{path}"
        last_error = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_t = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == self.max_retries:
                    break
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (2**attempt))
                continue
            self.request_count += 1
            self.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                raise MoleculeNotFoundError(f"HTTP 404 for {resp.url}")
            if resp.status_code in RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}"
                if attempt == self.max_retries:
                    break
                retry_after = resp.headers.get("Retry-After", "")
                delay = retry_after_seconds(retry_after, self.backoff_s * (2**attempt))
                if attempt < self.max_retries:
                    time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise ChemblDrugSearchError(
                    f"HTTP {resp.status_code} for {resp.url}: {resp.text[:300]}"
                )
            return resp.json()
        raise ChemblDrugSearchError(
            f"request failed after {self.max_retries + 1} attempts: {url} ({last_error})"
        )

    def paginate(
        self,
        path: str,
        items_key: str,
        params: dict[str, Any] | None = None,
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Retrieve every record behind a paged route; verify against total_count.

        ``path`` is the full resource path (e.g. ``/molecule.json`` or
        ``/similarity/<smiles>/<threshold>.json``).  Raises PaginationError if
        the retrieved record count differs from ``page_meta.total_count``
        (unless ``max_records`` truncated the walk on purpose).
        """
        query = dict(params or {})
        query["limit"] = page_size or self.page_size
        query["offset"] = 0
        records: list[dict[str, Any]] = []
        total: int | None = None
        while True:
            page = self.get_json(path, params=query)
            meta = page.get("page_meta") or {}
            total = meta.get("total_count")
            chunk = page.get(items_key) or []
            records.extend(chunk)
            if max_records is not None and len(records) >= max_records:
                return records[:max_records], total
            if not meta.get("next") or not chunk:
                break
            query["offset"] = int(query["offset"]) + len(chunk)
        if max_records is None and total is not None and len(records) != total:
            raise PaginationError(
                f"{path}: retrieved {len(records)} records but page_meta.total_count={total}"
            )
        return records, total



    def search_by_name(
        self,
        name: str,
        include_synonyms: bool = True,
        fields: str | None = None,
    ) -> list[dict[str, Any]]:
        """Exact (case-insensitive) compound lookup by preferred name.

        Tries ``pref_name__iexact`` first; when that returns nothing and
        ``include_synonyms`` is true, falls back to
        ``molecule_synonyms__molecule_synonym__iexact``.  Returns full (or
        field-selected) molecule records sorted by molecule_chembl_id.
        Exactness avoids the classic first-hit trap of the full-text route
        (e.g. salt/anhydrous forms outranking the parent drug).
        """
        params: dict[str, Any] = {"pref_name__iexact": name}
        if fields:
            params["only"] = fields
        records, _ = self.paginate("/molecule.json", "molecules", params)
        if not records and include_synonyms:
            params = {"molecule_synonyms__molecule_synonym__iexact": name}
            if fields:
                params["only"] = fields
            records, _ = self.paginate("/molecule.json", "molecules", params)
        return sort_by_key(records, "molecule_chembl_id")

    def search_fulltext(
        self, query: str, max_records: int | None = None, fields: str | None = None
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Full-text molecule search (``/molecule/search``), relevance-ordered.

        This is the route MCP-shaped patterns use.  Order is the server's
        relevance ranking (NOT canonicalized -- document any reliance on it);
        provided for discovery, not for exact resolution.
        """
        params: dict[str, Any] = {"q": query}
        if fields:
            params["only"] = fields
        return self.paginate(
            "/molecule/search.json", "molecules", params, max_records=max_records
        )

    def similarity_search(
        self,
        smiles: str,
        threshold: int,
        fields: str | None = None,
        max_records: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Tanimoto similarity search, paginated, deterministically ordered.

        ``threshold`` is the percent similarity cutoff (40-100 upstream).
        Records are sorted by (-similarity, molecule_chembl_id) client-side:
        the route rejects ``order_by`` (HTTP 400) and its native order is not
        contractual. With ``max_records`` the walk is bounded and the sort is
        scoped to the records walked — callers must disclose that.
        """
        if not 40 <= int(threshold) <= 100:
            raise ValueError("ChEMBL similarity threshold must be in [40, 100]")
        enc = urllib.parse.quote(smiles, safe="")
        params: dict[str, Any] = {}
        if fields:
            params["only"] = fields
        records, total = self.paginate(
            f"/similarity/{enc}/{int(threshold)}.json", "molecules", params,
            max_records=max_records,
        )
        return sort_similarity_records(records), total

    def substructure_search(
        self,
        smiles: str,
        fields: str | None = None,
        page_size: int | None = None,
        max_records: int | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Substructure search, fully paginated, sorted by molecule_chembl_id.

        Generic scaffolds can match tens of thousands of molecules (e.g. bare
        benzimidazole -> 30,000 at ChEMBL_37); use ``max_records`` to bound
        the walk -- ``total`` always reports the upstream total_count.
        """
        enc = urllib.parse.quote(smiles, safe="")
        params: dict[str, Any] = {}
        if fields:
            params["only"] = fields
        records, total = self.paginate(
            f"/substructure/{enc}.json",
            "molecules",
            params,
            page_size=page_size,
            max_records=max_records,
        )
        if max_records is None:
            records = sort_by_key(records, "molecule_chembl_id")
        return records, total

    def get_molecules(
        self,
        molecule_chembl_ids: Iterable[str],
        fields: str | None = None,
        missing_ok: bool = False,
    ) -> list[dict[str, Any]]:
        """Molecule records for explicit IDs (batched ``__in``), input order.

        Missing IDs raise MoleculeNotFoundError unless ``missing_ok`` (then
        they are simply absent from the result).
        """
        ids = list(molecule_chembl_ids)
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), self.batch_size):
            chunk = ids[start : start + self.batch_size]
            params: dict[str, Any] = {"molecule_chembl_id__in": ",".join(chunk)}
            if fields:
                params["only"] = fields
            records, _ = self.paginate("/molecule.json", "molecules", params)
            for rec in records:
                found[rec["molecule_chembl_id"]] = rec
        missing = [i for i in ids if i not in found]
        if missing and not missing_ok:
            raise MoleculeNotFoundError(f"molecule ChEMBL IDs not found: {missing}")
        return [found[i] for i in ids if i in found]



    def indication_rows(
        self,
        indication: str,
        only_approved: bool = False,
        match_field: str = "efo",
        fields: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Raw ``/drug_indication`` rows for a term (paginated + verified).

        ``match_field``: ``efo`` -> ``efo_term__icontains``;
        ``mesh`` -> ``mesh_heading__icontains`` (independent formulation,
        used by the accuracy gate).  ``only_approved`` adds
        ``max_phase_for_ind=4``.  Rows sorted by drugind_id.
        """
        field = {"efo": "efo_term__icontains", "mesh": "mesh_heading__icontains"}.get(match_field)
        if field is None:
            raise ValueError("match_field must be 'efo' or 'mesh'")
        params: dict[str, Any] = {field: indication}
        if only_approved:
            params["max_phase_for_ind"] = 4
        if fields:
            params["only"] = fields
        rows, total = self.paginate("/drug_indication.json", "drug_indications", params)
        return sort_by_key(rows, "drugind_id"), total

    def drug_warnings(
        self, molecule_chembl_ids: Iterable[str] | str, by_parent: bool = True
    ) -> list[dict[str, Any]]:
        """Warning records for one or many molecules (batched, sorted by warning_id).

        ``by_parent=True`` matches on ``parent_molecule_chembl_id`` so salt
        forms inherit their parent's warnings.
        """
        ids = [molecule_chembl_ids] if isinstance(molecule_chembl_ids, str) else list(molecule_chembl_ids)
        key = "parent_molecule_chembl_id__in" if by_parent else "molecule_chembl_id__in"
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), self.batch_size):
            chunk = ids[start : start + self.batch_size]
            rows, _ = self.paginate("/drug_warning.json", "drug_warnings", {key: ",".join(chunk)})
            out.extend(rows)
        return sort_by_key(out, "warning_id")

    def search_drugs_by_indication(
        self,
        indication: str,
        only_approved: bool = False,
        match_field: str = "efo",
        max_drugs: int | None = None,
    ) -> dict[str, Any]:
        """Indication-based drug search with approval/withdrawal flags.

        Joins ``/drug_indication`` rows -> distinct parent molecules ->
        molecule records (max_phase, first_approval, withdrawn_flag,
        black_box_warning) -> ``/drug_warning`` summaries.  Returns::

            {"indication_query": ..., "total_indication_rows": int,
             "total_parents": int,
             "indications": [rows sorted by drugind_id],
             "drugs": [joined records, best_phase_for_ind desc then id]}

        ``max_drugs`` bounds the molecule/warning join to the first N parents
        (best phase first) — ``total_parents`` always reflects the full distinct-parent
        count. Operon vendored-copy addition:
        without it a broad indication joins every matching parent (unbounded
        N+1 over throttled EBI calls) before callers can page.

        Every drug record carries ``indication_rows`` (its drugind_ids),
        ``best_phase_for_ind``, the molecule flag fields, and a de-duplicated
        ``warning_summary`` (empty list when the drug has no warnings).
        """
        rows, total = self.indication_rows(indication, only_approved, match_field)
        phase_by_parent: dict[str, float] = {}
        for r in rows:
            p = r.get("parent_molecule_chembl_id")
            if not p:
                continue
            ph = r.get("max_phase_for_ind")
            ph = float(ph) if ph is not None else -1.0
            if ph > phase_by_parent.get(p, -2.0):
                phase_by_parent[p] = ph




        all_parents = sorted(phase_by_parent,
                             key=lambda p: (-phase_by_parent[p], p))
        parents = all_parents if max_drugs is None else all_parents[:max_drugs]
        molecules = {
            m["molecule_chembl_id"]: m




            for m in (self.get_molecules(parents, fields=DRUG_FIELDS,
                                         missing_ok=True) if parents else [])
        }
        warnings_by_parent: dict[str, list[dict[str, Any]]] = {}
        for w in (self.drug_warnings(parents, by_parent=True) if parents else []):
            warnings_by_parent.setdefault(w.get("parent_molecule_chembl_id"), []).append(w)

        drugs: list[dict[str, Any]] = []
        for parent in parents:
            mol = molecules.get(parent, {})
            my_rows = [r for r in rows if r.get("parent_molecule_chembl_id") == parent]
            phases = [
                float(r["max_phase_for_ind"])
                for r in my_rows
                if r.get("max_phase_for_ind") is not None
            ]
            drugs.append(
                {
                    "parent_molecule_chembl_id": parent,
                    "pref_name": mol.get("pref_name"),
                    "max_phase": mol.get("max_phase"),
                    "first_approval": mol.get("first_approval"),
                    "withdrawn_flag": mol.get("withdrawn_flag"),
                    "black_box_warning": mol.get("black_box_warning"),
                    "molecule_type": mol.get("molecule_type"),
                    "indication_rows": [r["drugind_id"] for r in my_rows],
                    "best_phase_for_ind": max(phases) if phases else None,
                    "efo_terms": sorted({r.get("efo_term") for r in my_rows if r.get("efo_term")}),
                    "warning_summary": summarize_warnings(warnings_by_parent.get(parent, [])),
                }
            )
        return {
            "indication_query": {
                "term": indication,
                "match_field": match_field,
                "only_approved": only_approved,
            },
            "total_indication_rows": total,
            "total_parents": len(all_parents),
            "indications": rows,
            "drugs": drugs,
        }
