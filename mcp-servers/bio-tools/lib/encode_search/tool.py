"""High-level surface mirroring the six mcp-tooluniverse-encode methods."""
from __future__ import annotations

from typing import Any

from mcp_servers_common.pathseg import seg

from .client import EncodeClient
from .records import experiment_record, file_record, biosample_record, record_for


DEFAULT_PORTAL_PAGE = 25
PAGE_SIZE = 100

ORGANISM_FIELD = "replicates.library.biosample.donor.organism.scientific_name"


class EncodeSearch:
    def __init__(self, client: EncodeClient | None = None, page_size: int = PAGE_SIZE):
        self.client = client or EncodeClient()
        self.page_size = page_size




    def _search_all(self, type_name: str, filters: dict[str, Any],
                    fields: list[str], date_cutoff: str | None = None,
                    date_field: str = "date_released") -> dict:
        """Walk /report/ with sort=accession until `total` rows are collected.

        /search/ ignores `from` (verified live), so paging uses /report/.
        Zero-hit searches surface as HTTP 404 with total=0 -- treated as empty.
        Raises RuntimeError if the walk does not converge to the reported total.
        """
        params: dict[str, Any] = {"type": type_name, "format": "json",
                                  "sort": "accession", "limit": str(self.page_size)}
        for k, v in filters.items():
            if v is not None:
                params[k] = v
        if date_cutoff:
            params["advancedQuery"] = f"{date_field}:[* TO {date_cutoff}]"
        params["field"] = fields

        rows: list[dict] = []
        seen: set[str] = set()
        total: int | None = None
        offset = 0
        while True:
            page_params = dict(params)
            page_params["from"] = str(offset)
            doc = self.client.get_json("/report/", params=page_params,
                                       allow_empty_404=True)
            total = doc.get("total", 0)
            graph = doc.get("@graph", [])
            for row in graph:
                acc = row.get("accession") or row.get("@id")
                if acc not in seen:
                    seen.add(acc)
                    rows.append(row)
            offset += len(graph)
            if not graph or len(rows) >= (total or 0):
                break
        if total is None:
            total = 0
        if len(rows) != total:
            raise RuntimeError(
                f"pagination incomplete for {type_name} {filters}: "
                f"collected {len(rows)} of total {total}")
        return {"total": total, "rows": rows}





    EXPERIMENT_FIELDS = ["accession", "assay_title", "assay_term_name",
                         "target.label", "biosample_ontology.term_name",
                         "status", "date_released", "lab.title"]

    def search_experiments(self, assay_title: str | None = None,
                           target: str | None = None,
                           organism: str | None = None,
                           status: str = "released",
                           date_released_before: str | None = None,
                           extra_filters: dict | None = None) -> dict:
        filters: dict[str, Any] = {"status": status}
        if assay_title:
            filters["assay_title"] = assay_title
        if target:
            filters["target.label"] = target
        if organism:
            filters[ORGANISM_FIELD] = organism
        filters.update(extra_filters or {})
        out = self._search_all("Experiment", filters, self.EXPERIMENT_FIELDS,
                               date_cutoff=date_released_before,
                               date_field="date_released")
        out["accessions"] = sorted(r["accession"] for r in out["rows"])
        return out

    BIOSAMPLE_FIELDS = ["accession", "biosample_ontology.term_name",
                        "biosample_ontology.classification",
                        "organism.scientific_name", "status", "lab.title",
                        "summary", "date_created"]

    def search_biosamples(self, term_name: str | None = None,
                          classification: str | None = None,
                          organism: str | None = None,
                          status: str = "released",
                          date_created_before: str | None = None,
                          extra_filters: dict | None = None) -> dict:
        filters: dict[str, Any] = {"status": status}
        if term_name:
            filters["biosample_ontology.term_name"] = term_name
        if classification:
            filters["biosample_ontology.classification"] = classification
        if organism:
            filters["organism.scientific_name"] = organism
        filters.update(extra_filters or {})
        out = self._search_all("Biosample", filters, self.BIOSAMPLE_FIELDS,
                               date_cutoff=date_created_before,
                               date_field="date_created")
        out["accessions"] = sorted(r["accession"] for r in out["rows"])
        return out

    FILE_FIELDS = ["accession", "file_format", "output_type", "assay_term_name",
                   "assembly", "dataset", "status", "file_size", "date_created"]

    def list_files(self, file_format: str | None = None,
                   assay_term_name: str | None = None,
                   biosample_term_name: str | None = None,
                   status: str = "released",
                   date_created_before: str | None = None,
                   extra_filters: dict | None = None) -> dict:
        filters: dict[str, Any] = {"status": status}
        if file_format:
            filters["file_format"] = file_format
        if assay_term_name:
            filters["assay_term_name"] = assay_term_name
        if biosample_term_name:
            filters["biosample_ontology.term_name"] = biosample_term_name
        filters.update(extra_filters or {})
        out = self._search_all("File", filters, self.FILE_FIELDS,
                               date_cutoff=date_created_before,
                               date_field="date_created")
        out["accessions"] = sorted(r["accession"] for r in out["rows"])
        return out




    def _get_object(self, accession: str) -> dict:
        return self.client.get_json(
            f"/{seg(accession)}/",
            params={"format": "json"})

    def get_experiment(self, accession: str) -> dict:
        return experiment_record(self._get_object(accession))

    def get_file(self, accession: str) -> dict:
        return file_record(self._get_object(accession))

    def get_biosample(self, accession: str) -> dict:
        return biosample_record(self._get_object(accession))

    def get_any(self, accession: str) -> dict:
        return record_for(self._get_object(accession))




    def search_total(self, type_name: str, filters: dict[str, Any],
                     date_cutoff: str | None = None,
                     date_field: str = "date_released") -> int:
        """The API's own `total` from /search/ with limit=0 -- no rows fetched."""
        params: dict[str, Any] = {"type": type_name, "format": "json", "limit": "0"}
        params.update({k: v for k, v in filters.items() if v is not None})
        if date_cutoff:
            params["advancedQuery"] = f"{date_field}:[* TO {date_cutoff}]"
        doc = self.client.get_json("/search/", params=params, allow_empty_404=True)
        return doc.get("total", 0)

    def facet_count(self, type_name: str, filters: dict[str, Any],
                    facet_field: str, facet_key: str,
                    date_cutoff: str | None = None,
                    date_field: str = "date_released") -> int | None:
        """doc_count of `facet_key` in `facet_field`'s facet for a search that
        does NOT filter on that field -- an independent count formulation."""
        params: dict[str, Any] = {"type": type_name, "format": "json", "limit": "0"}
        params.update({k: v for k, v in filters.items() if v is not None})
        if date_cutoff:
            params["advancedQuery"] = f"{date_field}:[* TO {date_cutoff}]"
        doc = self.client.get_json("/search/", params=params, allow_empty_404=True)
        for facet in doc.get("facets", []):
            if facet.get("field") == facet_field:
                for term in facet.get("terms", []):
                    if term.get("key") == facet_key:
                        return term.get("doc_count")
                return 0
        return None

    def search_all_limit_all(self, type_name: str, filters: dict[str, Any],
                             fields: list[str],
                             date_cutoff: str | None = None,
                             date_field: str = "date_released") -> list[str]:
        """Accession list via /search/?limit=all -- the second full-retrieval
        formulation, independent of the /report/ from-walk."""
        params: dict[str, Any] = {"type": type_name, "format": "json",
                                  "limit": "all", "field": fields}
        params.update({k: v for k, v in filters.items() if v is not None})
        if date_cutoff:
            params["advancedQuery"] = f"{date_field}:[* TO {date_cutoff}]"
        doc = self.client.get_json("/search/", params=params, allow_empty_404=True)
        return sorted(r.get("accession") for r in doc.get("@graph", []))
