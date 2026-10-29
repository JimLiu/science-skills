"""FastMCP server exposing the protein-annotation fleet tools.

Backing fleet packages (retrieval, pacing, retries, determinism all live
there — this layer is marshalling only):

* ``interpro-domains``      — protein -> complete InterPro domain architecture
* ``interpro-entry-search`` — entry-centric InterPro/Pfam search + clans
* ``protein-atlas``         — Human Protein Atlas per-gene records + bulk search
* ``string-network``        — STRING id mapping, interaction networks, homology
"""
from __future__ import annotations

import interpro_domains
import interpro_entry_search
from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank, none_if_blank, reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)
from protein_atlas import ProteinAtlas
from string_network import (
    StringClient,
    build_network,
    get_best_similarity_hits,
    get_similarity_scores,
    map_identifiers,
)



mcp = GuardedFastMCP("mcp-protein-annotation")



_entry_client: interpro_entry_search.InterProEntryClient | None = None
_atlas: ProteinAtlas | None = None
_string_client: StringClient | None = None


def _entries() -> interpro_entry_search.InterProEntryClient:
    global _entry_client
    if _entry_client is None:
        _entry_client = interpro_entry_search.InterProEntryClient()
    return _entry_client


def _hpa() -> ProteinAtlas:
    global _atlas
    if _atlas is None:
        _atlas = ProteinAtlas()
    return _atlas


def _string() -> StringClient:
    global _string_client
    if _string_client is None:
        _string_client = StringClient()
    return _string_client






@mcp.tool(annotations=READ_ONLY)
def get_domain_architecture(accessions: list[str]) -> dict:
    """Complete InterPro domain architecture for one or more UniProt proteins.

    For each UniProt accession (e.g. "P04637"), returns every matching
    InterPro entry — accession, name, type (family/domain/repeat/site),
    member-database signatures (Pfam, PANTHER, SMART, PROSITE, CDD, ...),
    and the entry's fragment coordinates on the protein. Pagination is
    followed to completion and verified against the API's count, so large
    multi-domain proteins are never silently truncated.

    Use this for the protein->domains direction (includes all Pfam domain
    annotations for a protein). For the entry->proteins direction use
    search_interpro_entries / get_pfam_family_proteins.

    Returns {"summaries": {accession: {protein, protein_length, entry_count,
    entries: [...]}, ...}, "stats": {http_requests, bytes_downloaded}}.
    Absent location keys mean model=null, score=null, representative=false,
    fragment dc_status="CONTINUOUS".
    """
    return interpro_domains.fetch_domain_architecture(accessions)






@mcp.tool(annotations=READ_ONLY)
def search_interpro_entries(
    query: str | None = None,
    entry_type: str | None = None,
    source_db: str = "interpro",
    go_term: str | None = None,
) -> dict:
    """Keyword search over InterPro or member-database entries (complete walk).

    Args:
        query: free-text keyword (e.g. "kinase"). Optional if go_term is given.
        entry_type: filter by entry type — "family", "domain", "repeat",
            "homologous_superfamily", "conserved_site", "active_site",
            "binding_site", "ptm".
        source_db: "interpro" (default) or a member database such as "pfam",
            "smart", "prosite", "panther", "cdd" — searching "pfam" is the
            Pfam family search.
        go_term: filter by GO identifier (e.g. "GO:0004672").

    Cursor pagination is walked to completion and the accumulated row count
    is verified against the API's count field. Returns {"count": n,
    "results": [{accession, name, type, source_database, ...}, ...]} with
    rows sorted by accession.
    """





    reject_blank(query=query, entry_type=entry_type, source_db=source_db,
                 go_term=go_term)
    if query is None and go_term is None:
        raise ValueError("provide 'query' or 'go_term'")
    return interpro_entry_search.search_entries(
        q=query, entry_type=entry_type, source_db=source_db, go_term=go_term,
        client=_entries(),
    )


@mcp.tool(annotations=READ_ONLY)
def get_interpro_entry(accession: str) -> dict:
    """Detail record for an InterPro entry (IPRxxxxxx) or Pfam family (PFxxxxx).

    The route is chosen by accession prefix, so this serves both InterPro
    entry detail and Pfam family detail. Returns a deterministic summary:
    accession, name, type, description, GO terms (sorted), member-database
    signatures, integrated/set relationships (e.g. a Pfam family's clan in
    "set_info"), and counters.
    """
    return interpro_entry_search.get_entry(accession, client=_entries())




@mcp.tool(annotations=READ_ONLY, blank_as_absent={"query"})
def search_pfam_clans(query: str | None = None) -> dict:
    """Keyword search over Pfam clans (InterPro 'sets', accessions CLxxxx).

    Returns {"count": n, "results": [{accession, name, ...}, ...]}; an empty
    upstream result (HTTP 204) yields count=0 with an empty list.
    """




    return interpro_entry_search.search_clans(q=none_if_blank(query),
                                              client=_entries())


@mcp.tool(annotations=READ_ONLY)
def get_pfam_clan(clan_accession: str) -> dict:
    """Pfam clan detail including the complete sorted member-family list.

    Args:
        clan_accession: clan accession, e.g. "CL0016" (kinase clan).

    Returns clan metadata plus "members" (every PF family in the clan) and
    "member_count".
    """
    return interpro_entry_search.clan_members(clan_accession, client=_entries())


@mcp.tool(annotations=READ_ONLY)
def get_pfam_family_proteins(
    pfam_accession: str,
    reviewed_only: bool = False,
    tax_id: int | None = None,
    count_only: bool = False,
) -> dict:
    """Member proteins of a Pfam family (complete walk or count).

    Args:
        pfam_accession: Pfam family accession, e.g. "PF00069".
        reviewed_only: restrict to reviewed (Swiss-Prot) proteins.
        tax_id: restrict to an NCBI taxon, e.g. 9606 for human.
        count_only: return only the match count — REQUIRED for very large
            families (e.g. unfiltered PF00069 has hundreds of thousands of
            members); the full walk is paced at <=2 req/s.

    Returns {"count": n, "results": [...]} (results empty when count_only).
    """
    return interpro_entry_search.entry_proteins(
        pfam_accession, reviewed_only=reviewed_only, tax_id=tax_id,
        count_only=count_only, client=_entries(),
    )


@mcp.tool(annotations=READ_ONLY)
def get_pfam_family_proteomes(
    pfam_accession: str,
    count_only: bool = True,
) -> dict:
    """Proteomes containing members of a Pfam family.

    Args:
        pfam_accession: Pfam family accession, e.g. "PF00069".
        count_only: default True — the upstream proteome route's cursor
            pagination is defective for deep walks (fleet-documented), so the
            count is the reliable product. Set False only for small families.

    Returns {"count": n, "results": [...]}.
    """
    return interpro_entry_search.entry_proteomes(
        pfam_accession, count_only=count_only, client=_entries(),
    )






@mcp.tool(annotations=READ_ONLY)
def get_protein_atlas_gene(gene: str, full: bool = False) -> dict:
    """Human Protein Atlas per-gene record (HPA release 25.1, pinned host).

    Args:
        gene: Ensembl gene ID ("ENSG00000141510") or gene symbol ("TP53").
            Symbols are resolved by exact match on Gene then Gene synonym;
            ambiguous or unknown symbols raise an error listing candidates.
        full: False (default) returns a grouped summary with sections
            identity, tissue_expression, single_cell_expression,
            blood_expression, brain_expression, cancer_expression,
            subcellular, antibody, pathology (incl. per-cancer prognostics).
            True returns HPA's complete raw ~119-key record.

    Covers tissue/subcellular/pathology/blood/brain expression and antibody
    information for human genes.
    """
    return _hpa().get_gene(gene, full=full)


@mcp.tool(annotations=READ_ONLY)
def search_protein_atlas(
    query: str,
    columns: str = "g,gs,eg,gd,up,chr,chrp,scl",
) -> list[dict]:
    """Column-selected bulk search over the Human Protein Atlas.

    Args:
        query: free-text search (gene symbol, description keyword, ...).
        columns: comma-separated HPA search_download column codes. Common
            codes: g=Gene, gs=Gene synonym, eg=Ensembl, gd=Gene description,
            up=Uniprot, chr=Chromosome, chrp=Position, scl=Subcellular
            location, scml=Subcellular main location, ab=Antibody,
            pc=Protein class, pe=Evidence, di=Disease involvement.

    Returns a list of row dicts keyed by the human-readable field names of
    the per-gene record.
    """



    if blank(query):
        raise ValueError("query must be non-empty")
    return _hpa().search(query, columns=columns)






@mcp.tool(annotations=READ_ONLY)
def map_string_ids(symbols: list[str], species: int = 9606) -> dict:
    """Map gene symbols/aliases to STRING protein identifiers (v12.0 pinned).

    Every input symbol is either mapped (best match, with preferred name and
    annotation) or listed in "unmapped" — the two always partition the input,
    so silent drops are impossible.

    Args:
        symbols: gene symbols or aliases, e.g. ["TP53", "PD-1"].
        species: NCBI taxonomy ID (9606 = human).

    Returns {"string_version", "species", "mapped": [{query, string_id,
    preferred_name, annotation, ...}], "unmapped": [symbol, ...]}.
    """
    client = _string()
    version = client.get_version()
    mapped, unmapped = map_identifiers(client, symbols, species)
    return {"string_version": version, "species": species,
            "mapped": mapped, "unmapped": unmapped}


@mcp.tool(annotations=READ_ONLY)
def get_string_network(
    symbols: list[str],
    species: int = 9606,
    required_score: int = 700,
) -> dict:
    """STRING protein-protein interaction network for a gene list (v12.0).

    Maps symbols to STRING IDs first (unmapped symbols are reported
    explicitly), then retrieves the interaction network at the stated
    confidence threshold.

    Args:
        symbols: gene symbols, e.g. ["TP53", "BRCA1", "EGFR"].
        species: NCBI taxonomy ID (9606 = human).
        required_score: minimum combined interaction score 0-1000
            (400 = medium confidence, 700 = high, 900 = highest).

    Returns structured JSON: "nodes" (per-input mapping + degree; isolated
    nodes visible), "unmapped", deterministically ordered "edges"
    ({a, b, score, evidence-by-channel}), "summary" (node/edge counts,
    score stats), and a "provenance" request log.
    """
    return build_network(symbols, species=species,
                         required_score=required_score, client=_string())


@mcp.tool(annotations=READ_ONLY)
def get_string_similarity_scores(symbols: list[str], species: int = 9606) -> dict:
    """Smith-Waterman protein similarity bitscores among a gene set (STRING).

    Maps symbols to STRING IDs, then fetches STRING's all-vs-all homology
    bitscores within the set. "pairs" holds one record per reported
    unordered pair including self-scores; pairs absent from STRING's
    similarity data are not listed (sparse semantics — absence means no
    recorded similarity, not zero).

    Returns {"species", "mapped", "unmapped", "pairs": [{id_a, id_b,
    name_a, name_b, bitscore}, ...]}.
    """
    return get_similarity_scores(symbols, species=species, client=_string())


@mcp.tool(annotations=READ_ONLY)
def get_string_best_similarity_hits(
    symbols: list[str],
    species: int = 9606,
    target_species: int | None = None,
) -> dict:
    """Best homology hit per input protein in a target species (STRING).

    Args:
        symbols: gene symbols in the source species.
        species: source NCBI taxonomy ID (9606 = human).
        target_species: target NCBI taxonomy ID; None asks STRING for the
            best hit across all species in its homology data.

    Returns mapping info plus one best-hit record per query protein,
    sorted by query STRING ID.
    """
    return get_best_similarity_hits(symbols, species=species,
                                    species_b=target_species, client=_string())



def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
