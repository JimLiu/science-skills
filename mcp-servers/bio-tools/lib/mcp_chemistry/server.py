"""mcp-chemistry server — small-molecule chemistry retrieval.

Clean tier-2 schemas over four accuracy-gated fleet packages:

* ``pubchem-compounds``    — PubChem PUG REST: identifier resolution,
  computed properties, synonyms, bioassay summaries, 2D similarity
  (synchronous ``fastsimilarity_2d`` — no ListKey polling), GHS safety
  (PUG-View). NCBI etiquette: tool/email on every request, <= 2 req/s.
* ``chebi-ontology``       — ChEBI public backend API (the keyless JSON API
  behind the 2024+ ChEBI website): entity records with ontology relations,
  roles and xrefs; full-text search with api_total.
* ``rhea-reactions``       — Rhea via its public SPARQL endpoint
  (www.rhea-db.org REST sits behind a Cloudflare JS challenge; SPARQL is
  SIB's documented programmatic interface). Every capped search runs a
  companion COUNT query, so totals are always honest.
* ``bindingdb-affinities`` — BindingDB REST: measured Ki/Kd/IC50/EC50 rows
  by UniProt target or by compound similarity.

This layer is marshalling only: retrieval, pacing, retries and totals live
in the fleet packages. Listings are either complete or carry
``truncated`` + the upstream total — silent truncation is impossible.
"""

from __future__ import annotations

import re
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

mcp = FastMCP("mcp-chemistry")



@lru_cache(maxsize=1)
def _pubchem():
    from pubchem_compounds import PubChemCompounds
    return PubChemCompounds()


@lru_cache(maxsize=1)
def _chebi():
    from chebi_ontology import ChebiOntology
    return ChebiOntology()


@lru_cache(maxsize=1)
def _rhea():
    from rhea_reactions import RheaReactions
    return RheaReactions()


@lru_cache(maxsize=1)
def _bindingdb():
    from bindingdb_affinities import BindingDbAffinities
    return BindingDbAffinities()


def _cap(rows: list, cap: int) -> tuple[list, bool]:


    cap = max(0, cap)
    return (rows[:cap], True) if len(rows) > cap else (rows, False)





_CHEBI_QUERY_RE = re.compile(r"^(?:CHEBI:)?\d+$", re.IGNORECASE)
_EC_QUERY_RE = re.compile(r"^\d+\.\d+\.\d+\.n?\d+$")




_PARTIAL_EC_QUERY_RE = re.compile(r"^\d+\.(?:\d+|-)(?:\.(?:\d+|-)){0,2}$")




@mcp.tool(annotations=READ_ONLY)
def pubchem_search_compounds(query: str, namespace: str = "name",
                             max_cids: int = 25,
                             with_properties: bool = True) -> dict:
    """Resolve a chemical identifier to PubChem CIDs, optionally with core
    computed properties for the top hits.

    Args:
        query: the identifier — a chemical name (``aspirin``,
            ``acetylsalicylic acid``), SMILES (``CC(=O)OC1=CC=CC=C1C(=O)O``)
            or InChIKey (``BSYNRYMUTXBXSQ-UHFFFAOYSA-N``), matching
            ``namespace``. Names match PubChem's full synonym index (brand
            names, CAS numbers like ``50-78-2`` work too).
        namespace: one of ``name`` (default), ``smiles``, ``inchikey``,
            ``cid``.
        max_cids: cap on returned CIDs (1-100, default 25). ``n_cids_total``
            always carries the full match count and ``truncated`` flags a
            capped list.
        with_properties: also fetch properties (formula, weight, SMILES,
            InChIKey, IUPAC name, XLogP, TPSA, H-bond counts...) for the
            returned CIDs in one extra request (default True).

    Returns ``{query, namespace, n_cids_total, truncated, cids,
    properties}``; ``cids`` is [] when nothing matches (not an error).
    ``properties`` rows use PubChem's 2025 field names: ``SMILES`` is the
    full isomeric SMILES, ``ConnectivitySMILES`` the stereo-stripped one.
    """
    if not (1 <= max_cids <= 100):
        raise ValueError("max_cids must be in [1, 100]")
    cids = _pubchem().search_cids(query, namespace=namespace)
    capped, truncated = _cap(cids, max_cids)
    props = _pubchem().properties(capped) if (with_properties and capped) else []
    return {"query": query, "namespace": namespace,
            "n_cids_total": len(cids), "truncated": truncated,
            "cids": capped, "properties": props}


@mcp.tool(annotations=READ_ONLY)
def pubchem_get_compounds(cids: list[int], include_synonyms: bool = False,
                          max_synonyms: int = 30) -> dict:
    """Full computed property records for a batch of PubChem CIDs, with
    optional capped synonym lists.

    Args:
        cids: PubChem compound IDs (1-50 per call), e.g. ``[2244, 2519]``.
        include_synonyms: also fetch synonyms in one extra request
            (default False — synonym lists are long).
        max_synonyms: per-CID cap on returned synonyms (default 30).
            ``n_synonyms_total`` per record keeps the true count.

    Returns ``{n_requested, duplicates, records, not_found}``; ``duplicates``
    lists CIDs passed more than once (collapsed to one record each,
    first-occurrence order — arXiv ``get_papers`` precedent). Each record
    carries CID, MolecularFormula, MolecularWeight, SMILES (isomeric),
    ConnectivitySMILES, InChI, InChIKey, IUPACName, XLogP, ExactMass, TPSA,
    Charge, HBondDonorCount, HBondAcceptorCount, RotatableBondCount,
    HeavyAtomCount — plus ``synonyms``/``n_synonyms_total``/
    ``synonyms_truncated`` when requested. CIDs the API doesn't know are
    listed in ``not_found``.
    """
    if not cids:
        raise ValueError("cids must be non-empty")
    if len(cids) > 50:
        raise ValueError("at most 50 CIDs per call")
    cids = [int(c) for c in cids]


    unique = list(dict.fromkeys(cids))
    duplicates = list(dict.fromkeys(c for c in cids if cids.count(c) > 1))
    records = _pubchem().properties(unique)
    by_cid = {int(r["CID"]): dict(r) for r in records}
    if include_synonyms:
        syn_by_cid = _pubchem().synonyms(unique)
        for cid, rec in by_cid.items():
            syns = syn_by_cid.get(cid, [])
            capped, truncated = _cap(syns, max_synonyms)
            rec["synonyms"] = capped
            rec["n_synonyms_total"] = len(syns)
            rec["synonyms_truncated"] = truncated
    ordered = [by_cid[c] for c in unique if c in by_cid]
    not_found = [c for c in unique if c not in by_cid]
    return {"n_requested": len(cids), "duplicates": duplicates,
            "records": ordered, "not_found": not_found}


@mcp.tool(annotations=READ_ONLY)
def pubchem_similarity_search(smiles: str, threshold: int = 90,
                              max_records: int = 50,
                              with_properties: bool = False) -> dict:
    """2D Tanimoto similarity search over all of PubChem for a query SMILES
    (synchronous ``fastsimilarity_2d`` route — no job polling).

    Args:
        smiles: query structure as SMILES, e.g. ``CC(=O)OC1=CC=CC=C1C(=O)O``.
        threshold: minimum Tanimoto similarity in percent (1-100, default
            90). Lower values match more, and 2D similarity is permissive —
            below ~85 expect very loose analogs.
        max_records: upstream cap on hits (1-200, default 50). The API
            does not report the uncapped total, so ``may_be_truncated`` is
            true exactly when the cap was filled.
        with_properties: also fetch core properties for the first 10 hits.

    Returns ``{smiles, threshold, n_cids, may_be_truncated, cids,
    properties}`` — CIDs in upstream (relevance) order; the query compound
    itself is usually the first hit.
    """
    if not (1 <= max_records <= 200):
        raise ValueError("max_records must be in [1, 200]")
    cids = _pubchem().similarity_cids(smiles, threshold=threshold,
                                      max_records=max_records)
    props = _pubchem().properties(cids[:10]) if (with_properties and cids) else []
    return {"smiles": smiles, "threshold": threshold, "n_cids": len(cids),
            "may_be_truncated": len(cids) >= max_records, "cids": cids,
            "properties": props}


@mcp.tool(annotations=READ_ONLY)
def pubchem_get_bioassay_summary(cid: int, active_only: bool = False,
                                 max_rows: int = 100) -> dict:
    """Bioassay activity summary for one PubChem compound — which assays
    tested it, against which targets, with what outcome and potency.

    Args:
        cid: PubChem CID, e.g. ``2244`` (aspirin).
        active_only: keep only rows with Activity Outcome == ``Active``
            (default False). Filtering happens BEFORE the cap, so
            ``n_rows_total`` is the true count of (filtered) rows.
        max_rows: cap on returned rows (1-1000, default 100). Heavily
            assayed compounds have tens of thousands of rows;
            ``truncated`` + ``n_rows_total`` make the cap explicit.

    Returns ``{cid, active_only, n_rows_total, truncated, rows}``; each row
    maps the upstream columns: AID, SID, CID, Activity Outcome
    (Active/Inactive/Unspecified/Inconclusive), Target Accession (protein),
    Target GeneID, Activity Value [uM], Activity Name (IC50/Ki/...), Assay
    Name, Assay Type, PubMed ID. ``rows`` is [] for compounds with no assay
    data (not an error).
    """
    if not (1 <= max_rows <= 1000):
        raise ValueError("max_rows must be in [1, 1000]")
    rows = _pubchem().assay_summary(int(cid))
    if active_only:
        rows = [r for r in rows if r.get("Activity Outcome") == "Active"]
    capped, truncated = _cap(rows, max_rows)
    return {"cid": int(cid), "active_only": active_only,
            "n_rows_total": len(rows), "truncated": truncated,
            "rows": capped}


@mcp.tool(annotations=READ_ONLY)
def pubchem_get_safety(cid: int) -> dict:
    """GHS safety classification for one PubChem compound (PUG-View
    ``GHS Classification`` heading), aggregated across reporting sources.

    Args:
        cid: PubChem CID, e.g. ``702`` (ethanol).

    Returns ``{cid, found, ghs}``. ``ghs`` is null when PubChem has no GHS
    section for the compound; otherwise ``{cid, record_title, signals
    (e.g. ["Danger"]), pictograms (e.g. ["Flammable", "Irritant"]),
    hazard_statements (H-codes with occurrence percentages, e.g. "H302
    (95.6%): Harmful if swallowed [...]"), precautionary_statement_codes,
    notes, n_source_references}``. Aggregated = union across all SDS
    sources PubChem indexes; percentages in the statement text show how
    many sources report each hazard.
    """
    ghs = _pubchem().ghs_classification(int(cid))
    return {"cid": int(cid), "found": ghs is not None, "ghs": ghs}




@mcp.tool(annotations=READ_ONLY)
def chebi_search(term: str, max_results: int = 20, page: int = 1) -> dict:
    """Full-text search over ChEBI entities (names, synonyms, formulae,
    InChIKeys).

    Args:
        term: search text, e.g. ``caffeine``, ``C8H10N4O2`` or an InChIKey.
        max_results: page size (1-100, default 20).
        page: 1-based page number for walking further hits.

    Returns ``{term, page, size, api_total, number_pages, results}`` —
    ``api_total`` is ChEBI's own total hit count (results beyond this page
    exist iff ``api_total > page*size``). Each result: chebi_accession,
    name, definition, stars (3 = manually curated), formula, charge, mass,
    monoisotopic_mass, smiles, inchikey, relevance score.
    """
    return _chebi().search(term, size=max_results, page=page)


@mcp.tool(annotations=READ_ONLY)
def chebi_get_entity(chebi_id: str, max_synonyms: int = 30,
                     max_xrefs: int = 50) -> dict:
    """Full ChEBI entity record: names, structure, chemical data, roles and
    cross-references.

    Args:
        chebi_id: ChEBI identifier — ``CHEBI:27732`` or bare ``27732``.
            Secondary (merged) IDs resolve to the primary record.
        max_synonyms: cap on returned synonyms (default 30);
            ``n_synonyms_total`` keeps the true count.
        max_xrefs: cap on returned cross-references (default 50; citations
            and registry numbers can run to hundreds); ``n_xrefs_total``
            keeps the true count.

    Returns ``{chebi_accession, name, definition, stars, formula, charge,
    mass, monoisotopic_mass, smiles, inchi, inchikey, iupac_names,
    synonyms, n_synonyms_total, synonyms_truncated, secondary_ids, xrefs
    (type/accession/source/url), n_xrefs_total, xrefs_truncated, roles
    (ChEBI role classification, e.g. "central nervous system stimulant"),
    modified_on, is_released}``. Ontology parents/children are NOT here —
    use ``chebi_get_ontology``. Unknown IDs raise a not-found error.
    """
    rec = _chebi().get_compound(chebi_id)
    synonyms = rec.pop("synonyms")
    xrefs = rec.pop("xrefs")
    rec.pop("outgoing_relations")
    rec.pop("incoming_relations")
    rec["synonyms"], rec["synonyms_truncated"] = _cap(synonyms, max_synonyms)
    rec["n_synonyms_total"] = len(synonyms)
    rec["xrefs"], rec["xrefs_truncated"] = _cap(xrefs, max_xrefs)
    rec["n_xrefs_total"] = len(xrefs)
    return rec


@mcp.tool(annotations=READ_ONLY)
def chebi_get_ontology(chebi_id: str, relation_type: str | None = None,
                       max_relations: int = 100) -> dict:
    """Ontology relations of a ChEBI entity — what it IS (outgoing ``is a``
    parents, conjugate acids/bases, functional parents...) and what points
    AT it (incoming relations, i.e. its children/derivatives).

    Args:
        chebi_id: ChEBI identifier — ``CHEBI:27732`` or bare ``27732``.
        relation_type: optional exact filter, e.g. ``is a``, ``has part``,
            ``has role``, ``is conjugate acid of``, ``is conjugate base of``,
            ``has functional parent``, ``is tautomer of``,
            ``is enantiomer of``. Default: all types.
        max_relations: per-direction cap (default 100; hub entities like
            water have thousands of incoming ``has part`` relations).
            ``n_*_total`` keep true counts; ``*_truncated`` flag caps.

    Returns ``{chebi_accession, name, relation_type_filter,
    outgoing_relations, n_outgoing_total, outgoing_truncated,
    incoming_relations, n_incoming_total, incoming_truncated}``. Each
    relation: ``{relation_type, init_chebi_id, init_name, final_chebi_id,
    final_name}`` read as "init --relation--> final"; for outgoing rows
    init is this entity, for incoming rows final is this entity.
    """
    rec = _chebi().get_compound(chebi_id)
    outgoing = rec["outgoing_relations"]
    incoming = rec["incoming_relations"]
    if relation_type:
        outgoing = [r for r in outgoing if r["relation_type"] == relation_type]
        incoming = [r for r in incoming if r["relation_type"] == relation_type]
    out_capped, out_trunc = _cap(outgoing, max_relations)
    in_capped, in_trunc = _cap(incoming, max_relations)
    return {"chebi_accession": rec["chebi_accession"], "name": rec["name"],
            "relation_type_filter": relation_type,
            "outgoing_relations": out_capped,
            "n_outgoing_total": len(outgoing),
            "outgoing_truncated": out_trunc,
            "incoming_relations": in_capped,
            "n_incoming_total": len(incoming),
            "incoming_truncated": in_trunc}




@mcp.tool(annotations=READ_ONLY)
def rhea_search_reactions(query: str, limit: int = 50) -> dict:
    """Search Rhea master reactions by equation text, participant ChEBI ID,
    or EC number (query type auto-detected).

    Args:
        query: one of — a ChEBI ID (``CHEBI:27732`` / ``27732``) to find
            reactions with that participant (small molecules, reactive
            parts of generic compounds, and polymer underlying ChEBIs all
            match); a full EC number (``2.1.1.160``, partial ECs like
            ``2.1.1.-`` are rejected) for enzyme-linked reactions; anything
            else is a case-insensitive substring match on the chemical
            equation text (e.g. ``caffeine``, ``S-adenosyl-L-methionine``).
        limit: row cap (1-500, default 50). ``api_total`` (a companion
            COUNT query) always carries the true match count and
            ``truncated`` flags a capped list.

    Returns ``{query, query_type (chebi|ec|text), api_total, n_returned,
    truncated, reactions}``; each reaction: ``{rhea_id (master accession,
    e.g. "RHEA:10280"), equation, status (Approved/Preliminary/Obsolete)}``,
    ordered by rhea_id. Feed rhea_id into ``rhea_get_reaction`` for
    participants/EC/citations.
    """
    q = query.strip()
    if not q:
        raise ValueError("query must be non-empty")
    if _CHEBI_QUERY_RE.match(q):
        result = _rhea().search_by_chebi(q, limit=limit)
        query_type = "chebi"
    elif _EC_QUERY_RE.match(q):
        result = _rhea().search_by_ec(q, limit=limit)
        query_type = "ec"
    elif _PARTIAL_EC_QUERY_RE.match(q):




        result = _rhea().search_by_ec(q, limit=limit)
        query_type = "ec"
    else:
        result = _rhea().search_by_text(q, limit=limit)
        query_type = "text"
    return {"query": q, "query_type": query_type, **result}


@mcp.tool(annotations=READ_ONLY)
def rhea_get_reaction(rhea_id: str) -> dict:
    """Full record for one Rhea reaction: equation, participants with ChEBI
    IDs and stoichiometry, EC links, direction family and literature.

    Args:
        rhea_id: Rhea accession — ``RHEA:10280`` or bare ``10280``. Master
            (undirected) IDs carry the full participant breakdown; the
            related directional IDs are listed in the output. Unknown IDs
            raise a not-found error.

    Returns ``{rhea_id, equation, status, is_transport,
    is_chemically_balanced, ec_numbers (e.g. ["2.1.1.160"]), pubmed_ids,
    directional_reactions (the two left-to-right / right-to-left RHEA IDs),
    bidirectional_reaction, left_side, right_side}``. Each side lists
    participants ``{compound_accession (e.g. "CHEBI:27732", or
    "POLYMER:..."/"GENERIC:..." for non-small-molecule participants), name,
    coefficient ("1", "2", ... or symbolic "N"/"2n" for polymeric
    stoichiometry)}``. Sides follow the equation's left/right as written;
    master reactions are undirected — check ``directional_reactions`` for
    the physiological direction entries.
    """
    return _rhea().get_reaction(rhea_id)




@mcp.tool(annotations=READ_ONLY)
def bindingdb_ligands_by_target(uniprot: str, affinity_cutoff_nm: int = 10000,
                                max_rows: int = 100) -> dict:
    """Measured binding affinities (Ki/Kd/IC50/EC50) of all BindingDB
    ligands against one protein target, by UniProt accession.

    Args:
        uniprot: UniProt accession of the target, e.g. ``P00533`` (EGFR).
        affinity_cutoff_nm: only measurements with value <= this many nM
            (default 10000 = 10 uM). Tighten (e.g. 100) to get only potent
            binders — hot targets have tens of thousands of rows at 10 uM.
        max_rows: cap on returned rows (1-1000, default 100). The full
            match set is downloaded and counted, so ``n_rows_total`` is the
            true count and ``truncated`` flags the cap. Rows are sorted by
            (affinity_type, numeric affinity ascending) — most potent first
            within each measurement type.

    Returns ``{uniprot, affinity_cutoff_nm, n_rows_total, truncated,
    rows}``; each row: ``{target_name, monomer_id (BindingDB ligand ID),
    smiles, affinity_type (Ki/Kd/IC50/EC50), affinity (STRING, may carry
    ``>``/``<`` qualifiers, in nM), pmid, doi}``. No hits returns
    n_rows_total=0 (not an error).
    """
    if not (1 <= max_rows <= 1000):
        raise ValueError("max_rows must be in [1, 1000]")
    rows = _bindingdb().ligands_by_uniprot(uniprot,
                                           cutoff_nm=affinity_cutoff_nm)
    capped, truncated = _cap(rows, max_rows)
    return {"uniprot": uniprot.strip().upper(),
            "affinity_cutoff_nm": int(affinity_cutoff_nm),
            "n_rows_total": len(rows), "truncated": truncated,
            "rows": capped}


@mcp.tool(annotations=READ_ONLY)
def bindingdb_targets_by_compound(smiles: str, similarity: float = 0.85,
                                  max_rows: int = 100) -> dict:
    """Protein targets with measured affinities for compounds 2D-similar to
    a query SMILES — "what does this molecule (or its close analogs) bind?".

    Args:
        smiles: query structure as SMILES, e.g.
            ``CC(=O)OC1=CC=CC=C1C(=O)O`` (aspirin).
        similarity: minimum 2D Tanimoto similarity (0.5-1.0, default 0.85;
            1.0 = this exact structure only).
        max_rows: cap on returned rows (1-1000, default 100). The full row
            set is downloaded, so ``n_rows_total`` is the true row count and
            ``truncated`` flags the cap. ``api_hit_count`` is the upstream's
            own matching-compound count — rows are per-measurement (several
            Ki/Kd/IC50 rows per compound), so it is not row-for-row
            comparable with ``n_rows_total``. Rows sorted by (target_name,
            affinity_type, numeric affinity).

    Returns ``{smiles, similarity, api_hit_count, n_rows_total, truncated,
    rows}``; each row: ``{monomer_id, smiles (the matched analog),
    ligand_name, target_name, species, affinity_type, affinity (STRING,
    may carry ``>``/``<`` qualifiers, in nM), tanimoto}``. No hits returns
    n_rows_total=0 (not an error).
    """
    if not (1 <= max_rows <= 1000):
        raise ValueError("max_rows must be in [1, 1000]")
    result = _bindingdb().targets_by_compound(smiles, similarity=similarity)
    rows = result["rows"]
    capped, truncated = _cap(rows, max_rows)
    return {"smiles": smiles, "similarity": similarity,
            "api_hit_count": result["hit"], "n_rows_total": len(rows),
            "truncated": truncated, "rows": capped}


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
