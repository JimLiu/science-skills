"""High-level CIViC retrieval: 12 methods mirroring the tooluniverse/civic MCP surface.

Search methods walk Relay-style cursor pagination (``pageInfo.hasNextPage`` /
``endCursor``) to completion and verify the retrieved row count against the
connection's ``totalCount`` — the naive MCP-shaped pattern stops at the first
page (the server caps ``first`` at 100 regardless of the requested value) and
silently drops everything after it.
"""
from __future__ import annotations

from .client import CivicClient, CivicApiError, GraphQLError
from .records import (ASSERTION_FIELDS, DISEASE_FIELDS, EVIDENCE_FIELDS,
                      GENE_FIELDS, MOLECULAR_PROFILE_FIELDS, THERAPY_FIELDS,
                      VARIANT_CORE_FIELDS, normalize)



MAX_PAGE_SIZE = 100


class PaginationError(CivicApiError):
    """Retrieved row count does not equal the connection's totalCount."""


class CivicEvidence:
    """CIViC GraphQL retrieval with complete, count-verified pagination.

    Parameters
    ----------
    client : CivicClient, optional
        Inject a configured client (throttling, retries). A default polite
        client (0.5 s minimum between requests) is created if omitted.
    page_size : int
        Nodes per page for cursor walks (server caps at 100).
    """

    def __init__(self, client: CivicClient | None = None,
                 page_size: int = MAX_PAGE_SIZE):
        self.client = client or CivicClient()
        self.page_size = min(page_size, MAX_PAGE_SIZE)


    def _paged(self, field: str, arg_decls: str, arg_refs: str,
               variables: dict, node_fields: str) -> dict:
        """Walk one Relay connection to completion; verify against totalCount."""
        query = f"""
        query Paged($first: Int, $after: String{arg_decls}) {{
          conn: {field}(first: $first, after: $after{arg_refs}) {{
            totalCount
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ {node_fields} }}
          }}
        }}"""
        nodes: list = []
        after = None
        total = None
        pages = 0
        while True:
            d = self.client.execute(query, {**variables, "first": self.page_size,
                                            "after": after})
            conn = d["conn"]
            total = conn["totalCount"]
            nodes.extend(conn["nodes"])
            pages += 1
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
        if len(nodes) != total:
            raise PaginationError(
                f"{field}: retrieved {len(nodes)} nodes but totalCount={total}")
        return {"total_count": total, "pages_fetched": pages,
                "records": [normalize(n) for n in nodes]}

    def _single(self, field: str, entity_id: int, node_fields: str,
                id_key: str = "id") -> dict:
        query = f"""
        query Single($id: Int!) {{
          node: {field}({id_key}: $id) {{ {node_fields} }}
        }}"""
        d = self.client.execute(query, {"id": entity_id})
        rec = d.get("node")
        return {"query": {"mode": field, "id": entity_id},
                "found": rec is not None,
                "record": normalize(rec) if rec is not None else None}


    def search_genes(self, entrez_symbol: str) -> dict:
        """Genes matching an exact Entrez symbol (CIViC genes(entrezSymbols:)).

        Note: ``browseFeatures`` offers substring matching but its totalCount
        reports the UNFILTERED feature count, so it cannot be count-verified;
        this tool uses the exact-symbol route, which can.
        """
        out = self._paged("genes", ", $sym: [String!]", ", entrezSymbols: $sym",
                          {"sym": [entrez_symbol]}, GENE_FIELDS)
        out["query"] = {"mode": "search_genes", "entrez_symbol": entrez_symbol}
        return out

    def gene_variants(self, gene_id: int) -> dict:
        """All variants of one CIViC gene id, fully paginated."""
        out = self._paged("variants", ", $gid: Int", ", geneId: $gid",
                          {"gid": gene_id}, VARIANT_CORE_FIELDS)
        out["query"] = {"mode": "gene_variants", "gene_id": gene_id}
        out["records"].sort(key=lambda r: r["id"])
        return out


    def get_variant(self, variant_id: int) -> dict:
        return self._single("variant", variant_id, VARIANT_CORE_FIELDS)

    def search_variants(self, name: str, gene_id: int | None = None) -> dict:
        """Variants by name substring, optionally scoped to a gene."""
        decls, refs, var = ", $name: String", ", name: $name", {"name": name}
        if gene_id is not None:
            decls += ", $gid: Int"
            refs += ", geneId: $gid"
            var["gid"] = gene_id
        out = self._paged("variants", decls, refs, var, VARIANT_CORE_FIELDS)
        out["query"] = {"mode": "search_variants", "name": name, "gene_id": gene_id}
        out["records"].sort(key=lambda r: r["id"])
        return out


    def get_evidence_item(self, evidence_id: int) -> dict:
        return self._single("evidenceItem", evidence_id, EVIDENCE_FIELDS)

    EVIDENCE_FILTERS = {

        "disease_name": ("diseaseName", "String"),
        "therapy_name": ("therapyName", "String"),
        "evidence_level": ("evidenceLevel", "EvidenceLevel"),
        "evidence_type": ("evidenceType", "EvidenceType"),
        "evidence_direction": ("evidenceDirection", "EvidenceDirection"),
        "significance": ("significance", "EvidenceSignificance"),
        "variant_origin": ("variantOrigin", "VariantOrigin"),
        "evidence_rating": ("evidenceRating", "Int"),
        "status": ("status", "EvidenceStatusFilter"),
        "molecular_profile_name": ("molecularProfileName", "String"),
        "molecular_profile_id": ("molecularProfileId", "Int"),
        "variant_id": ("variantId", "Int"),
        "disease_id": ("diseaseId", "Int"),
        "therapy_id": ("therapyId", "Int"),
        "phenotype_id": ("phenotypeId", "Int"),
        "source_id": ("sourceId", "Int"),
        "assertion_id": ("assertionId", "Int"),
    }

    def search_evidence(self, **filters) -> dict:
        """Evidence items matching the given filters, fully paginated.

        Supported filters: see ``CivicEvidence.EVIDENCE_FILTERS`` (enum values
        are passed through verbatim, e.g. evidence_level="A",
        evidence_type="PREDICTIVE", status="ACCEPTED").
        Results are sorted by ascending evidence id (sortBy id ASC) so output
        order is deterministic.
        """
        return self._filtered_search("evidenceItems", filters,
                                     self.EVIDENCE_FILTERS, EVIDENCE_FIELDS,
                                     sort_decl=", $sb: EvidenceSort",
                                     sort_ref=", sortBy: $sb",
                                     sort_val={"column": "ID", "direction": "ASC"},
                                     mode="search_evidence")

    ASSERTION_FILTERS = {
        "disease_name": ("diseaseName", "String"),
        "therapy_name": ("therapyName", "String"),
        "assertion_type": ("assertionType", "EvidenceType"),
        "assertion_direction": ("assertionDirection", "EvidenceDirection"),
        "significance": ("significance", "AssertionSignificance"),
        "amp_level": ("ampLevel", "AmpLevel"),
        "status": ("status", "EvidenceStatusFilter"),
        "molecular_profile_name": ("molecularProfileName", "String"),
        "molecular_profile_id": ("molecularProfileId", "Int"),
        "variant_id": ("variantId", "Int"),
        "variant_name": ("variantName", "String"),
        "disease_id": ("diseaseId", "Int"),
        "therapy_id": ("therapyId", "Int"),
        "phenotype_id": ("phenotypeId", "Int"),
        "evidence_id": ("evidenceId", "Int"),
        "summary": ("summary", "String"),
    }


    def get_assertion(self, assertion_id: int) -> dict:
        return self._single("assertion", assertion_id, ASSERTION_FIELDS)

    def search_assertions(self, **filters) -> dict:
        """Assertions matching the given filters, fully paginated (sorted by id)."""
        return self._filtered_search("assertions", filters,
                                     self.ASSERTION_FILTERS, ASSERTION_FIELDS,
                                     sort_decl=", $sb: AssertionSort",
                                     sort_ref=", sortBy: $sb",
                                     sort_val={"column": "ID", "direction": "ASC"},
                                     mode="search_assertions")

    def _filtered_search(self, field, filters, allowed, node_fields,
                         sort_decl="", sort_ref="", sort_val=None, mode="") -> dict:
        decls, refs, var = [], [], {}
        for key, value in filters.items():
            if key not in allowed:
                raise ValueError(f"unsupported filter {key!r}; "
                                 f"allowed: {sorted(allowed)}")
            gql_arg, gql_type = allowed[key]
            vname = f"f_{gql_arg}"
            decls.append(f", ${vname}: {gql_type}")
            refs.append(f", {gql_arg}: ${vname}")
            var[vname] = value
        if sort_val is not None:
            decls.append(sort_decl)
            refs.append(sort_ref)
            var["sb"] = sort_val
        out = self._paged(field, "".join(decls), "".join(refs), var, node_fields)
        out["query"] = {"mode": mode, "filters": dict(filters)}
        return out


    def get_molecular_profile(self, mp_id: int) -> dict:
        return self._single("molecularProfile", mp_id, MOLECULAR_PROFILE_FIELDS)

    def search_molecular_profiles(self, name: str) -> dict:
        """Molecular profiles by name substring, fully paginated (sorted by id)."""
        out = self._paged("molecularProfiles", ", $name: String", ", name: $name",
                          {"name": name}, MOLECULAR_PROFILE_FIELDS)
        out["query"] = {"mode": "search_molecular_profiles", "name": name}
        out["records"].sort(key=lambda r: r["id"])
        return out


    def search_diseases(self, name: str) -> dict:
        """Diseases by name substring, fully paginated (sorted by id)."""
        out = self._paged("diseases", ", $name: String", ", name: $name",
                          {"name": name}, DISEASE_FIELDS)
        out["query"] = {"mode": "search_diseases", "name": name}
        out["records"].sort(key=lambda r: r["id"])
        return out

    def search_therapies(self, name: str) -> dict:
        """Therapies by name substring, fully paginated (sorted by id)."""
        out = self._paged("therapies", ", $name: String", ", name: $name",
                          {"name": name}, THERAPY_FIELDS)
        out["query"] = {"mode": "search_therapies", "name": name}
        out["records"].sort(key=lambda r: r["id"])
        return out


    def run_battery(self, battery: dict) -> dict:
        """Run the pinned battery spec (bench/battery.json) -> combined output."""
        out: dict = {"items": []}
        for item in battery["items"]:
            kind = item["kind"]
            spec = item["spec"]
            if kind == "search_genes":
                res = self.search_genes(spec["entrez_symbol"])
            elif kind == "gene_variants":
                res = self.gene_variants(spec["gene_id"])
            elif kind == "get_variant":
                res = self.get_variant(spec["variant_id"])
            elif kind == "search_variants":
                res = self.search_variants(spec["name"], spec.get("gene_id"))
            elif kind == "get_evidence_item":
                res = self.get_evidence_item(spec["evidence_id"])
            elif kind == "search_evidence":
                res = self.search_evidence(**spec["filters"])
            elif kind == "get_assertion":
                res = self.get_assertion(spec["assertion_id"])
            elif kind == "search_assertions":
                res = self.search_assertions(**spec["filters"])
            elif kind == "get_molecular_profile":
                res = self.get_molecular_profile(spec["mp_id"])
            elif kind == "search_molecular_profiles":
                res = self.search_molecular_profiles(spec["name"])
            elif kind == "search_diseases":
                res = self.search_diseases(spec["name"])
            elif kind == "search_therapies":
                res = self.search_therapies(spec["name"])
            else:
                raise ValueError(f"unknown battery item kind: {kind}")
            out["items"].append({"id": item["id"], "kind": kind, "result": res})
        return out
