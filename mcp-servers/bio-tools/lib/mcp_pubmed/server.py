"""mcp-pubmed server — tool handlers + stdio entry point.

Tool names/schemas are served verbatim from ``schemas.json`` (captured from
the original hosted connector). Retrieval is delegated to the fleet
packages; ``marshal`` reshapes results into the original output formats.
"""

from __future__ import annotations

import os
from functools import lru_cache

from mcp_servers_common import Tier1Server, load_schemas, original_json
from mcp_servers_common.gate import apply_gate_tier1

from . import marshal





NCBI_EMAIL = os.environ.get("NCBI_EMAIL")
NCBI_API_KEY = os.environ.get("NCBI_API_KEY") or None



@lru_cache(maxsize=1)
def _fetcher():
    from pubmed_fetch import PubMedFetcher
    return PubMedFetcher(email=NCBI_EMAIL, tool="mcp-pubmed", api_key=NCBI_API_KEY)


@lru_cache(maxsize=1)
def _search():
    from pubmed_search import PubMedSearch
    return PubMedSearch(email=NCBI_EMAIL, tool="mcp-pubmed", api_key=NCBI_API_KEY)


@lru_cache(maxsize=1)
def _citmatch_search():





    from pubmed_search import PubMedSearch
    return PubMedSearch(
        email=NCBI_EMAIL, tool="mcp-pubmed", api_key=NCBI_API_KEY,
        timeout=25.0, max_retries=1,
    )


def get_article_metadata(args: dict) -> str:
    pmids = [str(p) for p in args["pmids"]]
    xml_by_pmid = _fetcher().fetch_xml(pmids, strict=False)
    return original_json(marshal.article_metadata_response(xml_by_pmid, pmids))


def get_full_text_article(args: dict) -> str:
    from europepmc_fulltext import fetch_articles





    ids = [str(i).strip() for i in args["pmc_ids"] if str(i).strip()]





    MAX_PMC_IDS = 20
    if len(ids) > MAX_PMC_IDS:
        raise ValueError(
            f"too many pmc_ids ({len(ids)}); max {MAX_PMC_IDS} per call "
            "(full-text retrieval is one request per article)")
    ids = [f"PMC{i}" if i.isdigit() else i for i in ids]
    records = fetch_articles(ids)
    return original_json(marshal.full_text_response(records))


@lru_cache(maxsize=1)
def _eutils():
    from ncbi_elink.client import EUtilsClient
    return EUtilsClient()


def find_related_articles(args: dict) -> str:




    link_type = args.get("link_type", "pubmed_pubmed")
    parts = link_type.split("_")
    db = parts[1] if len(parts) > 1 else "pubmed"
    ids = [str(p) for p in args["pmids"]]
    params = {"dbfrom": "pubmed", "db": db, "id": ids,
              "retmode": "json", "linkname": link_type}
    payload = _eutils().get("elink.fcgi", params).json()
    return original_json(
        marshal.related_articles_response(
            payload.get("linksets", []), args.get("max_results")))


def convert_article_ids(args: dict) -> str:
    ids = [str(i) for i in args["ids"]]
    id_type = args.get("id_type", "pmid")
    records = _search().convert_ids(ids, from_type=id_type)
    return original_json(marshal.convert_ids_response(records, ids, id_type))


def get_copyright_status(args: dict) -> str:
    pmids = [str(p) for p in args["pmids"]]
    results = _search().copyright_status(pmids)


    dois = {r["requested_id"]: r.get("doi")
            for r in _search().convert_ids(pmids, from_type="pmid")}
    return original_json(marshal.copyright_status_response(results, dois))


def search_articles(args: dict) -> str:
    query = args["query"]
    retstart = int(args.get("retstart", 0))
    max_results = int(args.get("max_results", 20))



    fleet = _search().search_page(
        query,
        retstart=retstart,
        retmax=max_results,
        datetype=args.get("datetype", "pdat") if (args.get("date_from") or args.get("date_to")) else None,
        mindate=args.get("date_from"),
        maxdate=args.get("date_to"),
        sort=args.get("sort"),
    )
    return original_json(
        marshal.search_articles_page_response(fleet, query, retstart, max_results))


def lookup_article_by_citation(args: dict) -> str:
    citations = list(args["citations"])
    results = _citmatch_search().citmatch(citations)
    return original_json(marshal.citation_lookup_response(results, citations))


HANDLERS = {
    "get_article_metadata": get_article_metadata,
    "get_full_text_article": get_full_text_article,
    "find_related_articles": find_related_articles,
    "convert_article_ids": convert_article_ids,
    "get_copyright_status": get_copyright_status,
    "search_articles": search_articles,
    "lookup_article_by_citation": lookup_article_by_citation,
}


def build_server() -> Tier1Server:
    return Tier1Server("pubmed-mcp-server", load_schemas(__package__), HANDLERS)


def main() -> None:



    t1 = build_server()
    apply_gate_tier1(t1)
    t1.run()


if __name__ == "__main__":
    main()
