"""mcp-genes-ontologies server — FastMCP tools over the bio-tools fleet.

Retrieval is delegated entirely to the accuracy-gated fleet packages
(mygene-query, ols-ontologies, ols-terms, quickgo-annotations, reactome-map,
uniprot-fetch, kegg-fetch, kegg-link); this module is marshalling only.

KEGG REST is free for academic use and rate-sensitive: the fleet clients
pace every request (<3 req/s) and batch up to 10 entries per call — keep
KEGG queries small and infrequent.
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

from ols_ontologies import fetch_ontologies, list_catalogue
from ols_terms import RELATIONS, get_related_terms, lookup_term
from quickgo_annotations import AnnotationQuery, fetch_annotations, fetch_term_metadata
from reactome_map import compact_view, map_identifiers, stable_view
from kegg_link import find as kegg_find
from kegg_link import gene_ids_by_symbol
from kegg_link import link as kegg_link_op
from kegg_link import conv as kegg_conv_op



mcp = GuardedFastMCP("mcp-genes-ontologies")



@lru_cache(maxsize=1)
def _mygene():
    from mygene_query import MyGeneQueryClient
    return MyGeneQueryClient()


@lru_cache(maxsize=1)
def _ols_onto():
    from ols_ontologies import OLSClient
    return OLSClient()


@lru_cache(maxsize=1)
def _ols():
    from ols_terms import OLSClient
    return OLSClient()


@lru_cache(maxsize=1)
def _quickgo():
    from quickgo_annotations import QuickGOClient
    return QuickGOClient()


@lru_cache(maxsize=1)
def _uniprot():
    from uniprot_fetch import UniProtClient
    return UniProtClient()


@lru_cache(maxsize=1)
def _kegg_fetch():
    from kegg_fetch import KeggClient
    return KeggClient()


@lru_cache(maxsize=1)
def _kegg_link():
    from kegg_link import KeggClient
    return KeggClient()





@mcp.tool(annotations=READ_ONLY)
def query_genes(
    terms: list[str],
    scopes: str = "symbol",
    fields: str = "symbol,name,taxid,entrezgene,ensembl.gene",
    species: str = "human",
) -> dict:
    """Resolve gene identifiers/symbols via mygene.info (batched, up to 1000 terms/request).

    Use this to map gene symbols to Ensembl gene IDs, Entrez IDs, names, and any
    other mygene.info field — or the reverse (set `scopes` to the namespace of
    your input terms, e.g. "entrezgene", "ensembl.gene", "symbol,alias").

    Args:
        terms: Query terms (e.g. gene symbols ["TP53", "BRCA1"]). Terms containing
            commas are not supported.
        scopes: Comma-separated identifier namespaces to match terms against
            (e.g. "symbol", "entrezgene", "ensembl.gene", "symbol,alias").
        fields: Comma-separated mygene.info fields to return
            (e.g. "symbol,name,taxid,entrezgene,ensembl.gene"; "all" for everything).
        species: Species filter — common name ("human", "mouse") or NCBI taxid.

    Returns:
        {n_input, n_records, not_found: [terms with no match], records: [...]}.
        A term matching several genes yields several records (each carries its
        `query` term). Records are deterministically ordered (input order, then _id).
    """
    recs = _mygene().querymany(terms, scopes=scopes, fields=fields, species=species)
    found = [r for r in recs if not r.get("notfound")]
    missing = sorted({str(r.get("query")) for r in recs if r.get("notfound")})
    return {
        "n_input": len(terms),
        "n_records": len(found),
        "not_found": missing,
        "records": found,
    }





@mcp.tool(annotations=READ_ONLY)
def list_ontologies(ontology_ids: list[str] | None = None) -> dict:
    """List ontologies in the EBI Ontology Lookup Service (OLS4).

    With `ontology_ids` (e.g. ["efo", "cl", "chebi", "go", "mondo"]): fetch
    structured metadata records for just those ontologies; unknown IDs are
    reported in `not_found`. Without: the complete OLS4 catalogue (~250
    ontologies, paginated fully and count-verified).

    Returns:
        {records: [{ontology_id, title, version, status, num_terms, ...}],
         not_found: [...]} for an ID list, or
        {records: [...], total_elements, complete} for the full catalogue.
    """
    if ontology_ids:
        out = fetch_ontologies(ontology_ids, client=_ols_onto())
        return {"records": out["records"], "not_found": out["not_found"]}
    out = list_catalogue(client=_ols_onto())
    return {
        "records": out["records"],
        "total_elements": out["total_elements"],
        "complete": out["complete"],
    }


@mcp.tool(annotations=READ_ONLY)
def search_ontology_terms(
    query: str,
    ontologies: list[str] | None = None,
    exact: bool = False,
    include_obsolete: bool = False,
    max_results: int = 25,
) -> dict:
    """Search ontology terms by label/synonym across one or more OLS4 ontologies.

    Typical uses: find an EFO ID for a disease name (ontologies=["efo"]), Cell
    Ontology terms for a cell type (["cl"]), ChEBI terms for a chemical
    (["chebi"]), GO terms by name (["go"]) — or search all ontologies at once.

    Args:
        query: Search text (term label, synonym, or identifier).
        ontologies: Ontology IDs to restrict the search to (lowercase, e.g.
            ["efo"], ["cl", "uberon"]); None searches every ontology.
        exact: Require exact (whole-string) label/synonym match.
        include_obsolete: Also return obsolete terms (default False).
        max_results: Maximum terms returned (ranked by OLS relevance).

    Returns:
        {query, total_found, n_returned, truncated, terms: [{curie, iri, label,
         short_form, ontology, description, type, is_defining_ontology}]}.
    """


    if blank(query):
        raise ValueError("query must be non-empty")
    params: dict = {"q": query, "rows": max_results, "start": 0}
    if ontologies:
        params["ontology"] = ",".join(o.lower() for o in ontologies)
    if exact:
        params["exact"] = "true"
    if include_obsolete:
        params["obsoletes"] = "true"
    data = _ols().get_json("search", params=params)
    resp = data.get("response", {})
    docs = resp.get("docs", [])
    terms = [
        {
            "curie": d.get("obo_id") or d.get("short_form"),
            "iri": d.get("iri"),
            "label": d.get("label"),
            "short_form": d.get("short_form"),
            "ontology": d.get("ontology_name"),
            "description": d.get("description"),
            "type": d.get("type"),
            "is_defining_ontology": d.get("is_defining_ontology"),
        }
        for d in docs
    ]
    total = int(resp.get("numFound", len(terms)))
    return {
        "query": query,
        "ontologies": ontologies,
        "total_found": total,
        "n_returned": len(terms),
        "truncated": total > len(terms),
        "terms": terms,
    }


@mcp.tool(annotations=READ_ONLY)
def get_ontology_term(
    ontology: str,
    term_id: str,
    relation: str | None = None,
    include_parents: bool = True,
) -> dict:
    """Fetch one ontology term's details, or its complete related-term set.

    With `relation=None`: full term record (label, synonyms, description,
    obsolete flag, direct parents). With a relation: the COMPLETE, fully
    paginated set of related terms — e.g. relation="hierarchicalChildren" for
    direct children incl. part_of etc., "descendants"/"hierarchicalDescendants"
    for the whole subtree, "ancestors"/"hierarchicalAncestors", "parents",
    "children". Retrieval is count-verified against the API's own total.

    Args:
        ontology: OLS4 ontology ID (lowercase, e.g. "efo", "go", "cl", "chebi").
        term_id: Term CURIE ("EFO:0000305", "GO:0006281") or full IRI.
        relation: None, or one of: parents, children, ancestors, descendants,
            hierarchicalParents, hierarchicalChildren, hierarchicalAncestors,
            hierarchicalDescendants.
        include_parents: Include direct parent refs on the term record
            (only used when relation is None).

    Returns:
        relation=None: {curie, iri, label, ontology, short_form, synonyms,
        description, is_obsolete, has_children, parents}.
        Otherwise: {root, relation, total_elements, term_count, terms: [...]}.
    """
    if relation is None:
        return lookup_term(_ols(), ontology, term_id, include_parents=include_parents).to_dict()
    if relation not in RELATIONS:
        raise ValueError(f"relation must be None or one of {RELATIONS}, got {relation!r}")
    return get_related_terms(_ols(), ontology, term_id, relation).to_dict()





@mcp.tool(annotations=READ_ONLY)
def get_go_annotations(
    uniprot_accession: str,
    aspect: str | None = None,
    evidence: str | None = None,
    taxon_id: int | None = None,
    include_term_names: bool = True,
    max_records: int = 500,
) -> dict:
    """Retrieve GO annotations for a UniProt gene product from QuickGO (complete, count-verified).

    Args:
        uniprot_accession: UniProt accession, e.g. "P04637" (prefix optional).
        aspect: None (all) or one of "biological_process", "molecular_function",
            "cellular_component".
        evidence: None (all), a preset ("experimental_manual" = manually-assigned
            experimental evidence, "automatic_iea" = electronic/IEA), or an
            explicit ECO code like "ECO:0000314". Three-letter GO evidence codes
            (IDA, IEA, ...) are NOT accepted — the QuickGO API silently ignores
            its goEvidence parameter, so filtering must use ECO codes.
        taxon_id: Optional NCBI taxon restriction (exact), e.g. 9606.
        include_term_names: Hydrate each returned record with the GO term name,
            aspect and obsolete flag (one batched ontology lookup).
        max_records: Cap on annotation records returned in `records` (the full
            set is still retrieved and summarized; `truncated` flags the cap).

    Returns:
        {gene_product, total_annotations (API's own count), n_records, complete,
         truncated, distinct_go_ids (across ALL annotations, not just the
         returned page), records: [{go_id, go_aspect, qualifier, go_evidence,
         eco_id, reference, assigned_by, date, ...}]}.
    """



    if blank(uniprot_accession):
        raise ValueError("uniprot_accession must be non-empty")
    query = AnnotationQuery(
        gene_product_id=uniprot_accession,
        aspect=aspect,
        evidence=evidence,
        taxon_id=taxon_id,
    )
    res = fetch_annotations(_quickgo(), query)
    records = res["records"]
    out_records = records[:max_records]
    if include_term_names and out_records:
        meta = fetch_term_metadata(_quickgo(), sorted({r["go_id"] for r in out_records}))
        for rec in out_records:
            m = meta.get(rec["go_id"])
            rec["go_name"] = m["name"] if m else None
            rec["go_is_obsolete"] = m["is_obsolete"] if m else None
    return {
        "gene_product": query.accession,
        "aspect": aspect,
        "evidence": evidence,
        "taxon_id": taxon_id,
        "total_annotations": res["total_items"],
        "complete": res["complete"],
        "n_records": len(out_records),
        "truncated": len(records) > len(out_records),
        "distinct_go_ids": sorted({r["go_id"] for r in records}),
        "records": out_records,
    }





@mcp.tool(annotations=READ_ONLY)
def get_uniprot_entries(
    accessions: list[str],
    format: str = "fasta",
    fields: list[str] | None = None,
) -> dict:
    """Fetch UniProtKB records for a list of accessions (batched OR-queries, not per-accession).

    Three modes:
    - `fields` given: token-lean tabular retrieval of just those UniProt fields
      (e.g. ["accession", "id", "protein_name", "gene_names", "organism_name",
      "length", "sequence"]); `format` is ignored.
    - format="fasta": per-accession FASTA sequences.
    - format="txt": per-accession full UniProt flat-file text (complete
      annotation; can be very large — prefer `fields` when you need specifics).

    Args:
        accessions: UniProt accessions, e.g. ["P04637", "P38398"].
        format: "fasta" or "txt" (ignored when `fields` is given).
        fields: Optional UniProt REST field names for tabular mode.

    Returns:
        fields mode: {accessions, fields, n_records, records: [{<column>: value}]}.
        fasta/txt mode: {accessions, format, n_found, missing, records:
        {accession: text}} — `missing` lists accessions UniProt returned no
        record for (merged/deleted accessions land there).
    """
    accessions = [str(a) for a in accessions]
    if fields:
        tsv = _uniprot().fetch_fields(accessions, fields, fmt="tsv")
        lines = [ln for ln in tsv.splitlines() if ln.strip()]
        records: list[dict] = []
        header: list[str] | None = None
        for ln in lines:
            cols = ln.split("\t")
            if header is None:
                header = cols
                continue
            if cols == header:
                continue
            records.append(dict(zip(header, cols)))
        return {
            "accessions": accessions,
            "fields": fields,
            "n_records": len(records),
            "records": records,
        }
    if format not in ("fasta", "txt"):
        raise ValueError(f"format must be 'fasta' or 'txt', got {format!r}")
    if format == "fasta":
        recs, missing = _uniprot().fetch_fasta(accessions)
    else:
        recs, missing = _uniprot().fetch_flatfile(accessions)
    return {
        "accessions": accessions,
        "format": format,
        "n_found": len(recs),
        "missing": missing,
        "records": recs,
    }





@mcp.tool(annotations=READ_ONLY)
def map_reactome_pathways(
    identifiers: list[str],
    id_type: str = "symbol",
    species: str = "Homo sapiens",
    resource: str = "TOTAL",
    include_disease: bool = True,
    compact: bool = True,
) -> dict:
    """Map gene symbols or UniProt accessions to Reactome pathways (AnalysisService token workflow).

    Args:
        identifiers: Gene symbols (id_type="symbol") or UniProt accessions
            (id_type="uniprot"). No duplicates.
        id_type: "symbol" or "uniprot".
        species: Species filter on returned pathways (default "Homo sapiens").
        resource: AnalysisService molecule-resource view ("TOTAL" default;
            "UNIPROT" restricts to protein-level mappings — the like-for-like
            view when comparing symbol vs accession submissions).
        include_disease: Include disease pathways (service default True).
        compact: True → per-identifier low-level pathways only ({stId, name,
            species}), plus the Reactome release version. False → the full
            deterministic result: per-identifier complete pathway sets with
            entity/reaction statistics (p-values, FDR, found/total counts) and
            the batch summary incl. identifiers_not_found.

    Returns:
        compact: {tool, reactome_version, id_type, species, n_input,
        genes: {identifier: {found, n_lowlevel_pathways, pathways}}}.
        full: adds per-pathway statistics and batch_summary.
    """
    result = map_identifiers(
        identifiers,
        id_type,
        species=species,
        resource=resource,
        include_disease=include_disease,
    )
    return compact_view(result) if compact else stable_view(result)





@mcp.tool(annotations=READ_ONLY)
def get_kegg_entries(ids: list[str], include_raw: bool = False) -> dict:
    """Fetch KEGG entries by ID (genes, pathways, compounds, ...) — batched /get, 10 per request.

    Args:
        ids: KEGG entry IDs, e.g. ["hsa:7157"] (gene), ["hsa04110"] (pathway),
            ["C00031"] or ["cpd:C00031"] (compound). No duplicates. KEGG is
            rate-limited (academic REST service) — keep lists small.
        include_raw: Also return the raw KEGG flat-file text per entry.

    Returns:
        {n_entries, entries: [{requested_id, entry_id, record, raw?}]} where
        `record` is the structured parse: {entry, entry_id, entry_type, name,
        symbol, definition, organism, formula, pathway: [{id, name}],
        orthology: [{id, name}]}. An ID KEGG returns nothing for raises an
        error naming the missing IDs.
    """
    entries = _kegg_fetch().get_entries([str(i) for i in ids])
    out = []
    for e in entries:
        item = {"requested_id": e.requested_id, "entry_id": e.entry_id, "record": e.record}
        if include_raw:
            item["raw"] = e.raw
        out.append(item)
    return {"n_entries": len(out), "entries": out}


@mcp.tool(annotations=READ_ONLY)
def search_kegg(
    query: str,
    database: str = "hsa",
    exact_gene_symbol: bool = False,
    max_hits: int = 200,
) -> dict:
    """Search a KEGG database by keyword (/find), with optional exact gene-symbol resolution.

    KEGG's /find matches the query as a SUBSTRING anywhere in the entry line —
    searching "TP53" in hsa returns TP53BP2, TP53I3, even alias-only matches,
    before TP53 itself. To resolve a gene symbol to its KEGG gene ID(s), set
    exact_gene_symbol=True: hits are filtered to rows whose symbol list contains
    the query exactly (case-insensitive); ambiguous symbols return ALL matching
    genes, zero matches is a normal outcome (not an error).

    Args:
        query: Search text (gene symbol, compound name, keyword).
        database: KEGG database — an organism code ("hsa", "mmu") for genes, or
            "compound", "pathway", "drug", ...
        exact_gene_symbol: Apply exact symbol-list filtering (gene databases).
        max_hits: Cap on raw keyword hits returned (ignored in exact mode).

    Returns:
        exact mode: {symbol, organism, n_matches, matches: [{entry_id, symbols,
        description}]}.
        keyword mode: {query, database, total_hits, n_returned, truncated,
        hits: [{entry_id, description}]}.
    """



    if blank(query):
        raise ValueError("query must be non-empty")
    if exact_gene_symbol:
        return gene_ids_by_symbol(_kegg_link(), query, organism=database)
    hits = kegg_find(_kegg_link(), database, query)
    return {
        "query": query,
        "database": database,
        "total_hits": len(hits),
        "n_returned": min(len(hits), max_hits),
        "truncated": len(hits) > max_hits,
        "hits": hits[:max_hits],
    }


@mcp.tool(annotations=READ_ONLY)
def link_kegg_ids(ids: list[str], target_db: str, operation: str = "link") -> dict:
    """Cross-reference KEGG IDs against another database (/link) or convert to/from outside IDs (/conv).

    Examples: link ["hsa:7157"] to "pathway" (gene → pathways); conv
    ["ncbi-geneid:7157"] to "hsa" (NCBI Gene → KEGG gene) or ["hsa:7157"] to
    "ncbi-geneid"/"uniprot" (KEGG → outside IDs). Batched 10 IDs per request.

    Args:
        ids: Source KEGG/outside IDs (prefixed, e.g. "hsa:7157", "cpd:C00031",
            "ncbi-geneid:7157").
        target_db: Target database ("pathway", "ko", "enzyme", "hsa",
            "ncbi-geneid", "uniprot", ...).
        operation: "link" (cross-reference within KEGG) or "conv" (convert
            between KEGG and outside identifier namespaces).

    Returns:
        {operation, target_db, query_ids, n_records, per_id_targets:
        {input_id: [target_ids]}, missing_ids (inputs with zero hits — reported
        explicitly, never silently dropped), records: [{source_id, source_db,
        target_id, target_db, ...}]}.
    """
    if operation not in ("link", "conv"):
        raise ValueError(f"operation must be 'link' or 'conv', got {operation!r}")
    fn = kegg_link_op if operation == "link" else kegg_conv_op
    res = fn(_kegg_link(), [str(i) for i in ids], target_db)
    return {
        "operation": res.operation,
        "target_db": res.target_db,
        "query_ids": res.query_ids,
        "n_records": len(res.records),
        "per_id_targets": res.per_id_targets(),
        "missing_ids": res.missing_ids,
        "records": [
            {k: r[k] for k in ("source_id", "source_db", "target_id", "target_db", "operation")}
            for r in res.records
        ],
    }


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
