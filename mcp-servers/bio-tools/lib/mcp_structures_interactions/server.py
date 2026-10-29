"""mcp-structures-interactions server — structural biology & molecular interactions.

Clean tier-2 schemas over three accuracy-gated fleet packages:

* ``emdb-meta``             — EMDB cryo-EM map *metadata* (entries, Solr search,
                              publication/map/sample/imaging sections, validation metrics)
* ``complexportal-fetch``   — EBI Complex Portal curated complexes (CPX records,
                              participant searches, total-verified pagination)
* ``intact-interactions``   — IntAct binary interaction evidence (complete
                              MI-score-filtered sweeps, interactor details, networks)
* ``pdb-structures``        — RCSB PDB entry/entity/ligand *metadata* (attribute
                              search, capped+flagged against the API total)
* ``alphafold-structures``  — AlphaFold DB prediction metadata (pLDDT summaries,
                              model URLs — payloads never downloaded)

This layer is marshalling only: retrieval, pacing (<=2 req/s per host),
retries and count verification live in the fleet packages.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank, reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)


from emdb_meta import (
    fetch_section_records,
    fetch_validation_records,
    run_search_spec,
)
from emdb_meta.records import fetch_entry_records
from complexportal_fetch import fetch_complexes, search_by_participant
from intact_interactions import (
    build_network,
    fetch_interactions,
    get_interaction_details,
    get_interactor,
)
from pdb_structures import (
    MAX_IDS_PER_CALL as PDB_MAX_IDS,
    fetch_entity_records,
    fetch_entry_records as pdb_fetch_entry_records,
    fetch_ligand_records,
    search_structures,
)
from alphafold_structures import fetch_coverage_records, fetch_prediction_record



mcp = GuardedFastMCP("mcp-structures-interactions")



@lru_cache(maxsize=1)
def _emdb_client():
    from emdb_meta import EMDBClient
    return EMDBClient()


@lru_cache(maxsize=1)
def _cpx_client():
    from complexportal_fetch import ComplexPortalClient
    return ComplexPortalClient()


@lru_cache(maxsize=1)
def _intact_client():
    from intact_interactions import IntActClient
    return IntActClient()


@lru_cache(maxsize=1)
def _pdb_client():
    from pdb_structures import PDBClient
    return PDBClient()


@lru_cache(maxsize=1)
def _afdb_client():
    from alphafold_structures import AlphaFoldClient
    return AlphaFoldClient()


def _cap(records: list, max_returned: int | None) -> tuple[list, bool]:
    """Slice an already-complete record list for output; never affects retrieval."""
    if max_returned is not None and max_returned >= 0 and len(records) > max_returned:
        return records[:max_returned], True
    return records, False





@mcp.tool(annotations=READ_ONLY)
def emdb_get_entries(emdb_ids: list[str]) -> dict[str, Any]:
    """Fetch structured metadata records for EMDB cryo-EM entries.

    Accepts accessions as 'EMD-1234', 'emd-1234' or '1234'. Each record carries
    title, structure determination method (singleParticle / helical / tomography /
    subtomogramAveraging / electronCrystallography), resolution in Angstrom
    (null for entries with no reported resolution, e.g. raw tomograms) and the
    resolution method, deposition/release dates, sample and macromolecule names,
    fitted PDB model IDs (empty list when no model is fitted), primary citation
    (journal, year, first author, DOI, PMID), map dimensions and voxel size, and
    status. Obsolete entries report is_obsolete=true plus superseded_by
    accessions. Unknown accessions come back as {"emdb_id", "error": "not_found"}
    — never silently dropped. Metadata only; map volumes are never downloaded.
    """
    records = fetch_entry_records(_emdb_client(), emdb_ids)
    return {"n_requested": len(emdb_ids), "records": records}


@mcp.tool(annotations=READ_ONLY)
def emdb_search_entries(query: str, max_rows: int = 1000) -> dict[str, Any]:
    """Search EMDB with a Solr-style query; complete paged retrieval of compact rows.

    Query examples: 'title:"apoferritin" AND resolution:[0 TO 1.5]',
    'structure_determination_method:"singleParticle"',
    'current_status:"REL" AND release_date:[2024-01-01T00:00:00Z TO *]'.

    Returns num_found_released (the API's own released-entry count — ground
    truth), rows_retrieved, rows_by_status (REL vs OBS — obsolete entries are
    returned by the search route but not counted as released), released_complete
    (true iff every released match was retrieved; false means max_rows truncated
    the sweep or the counts disagree), and records: compact per-entry rows
    (emdb_id, title, resolution, structure_determination_method, current_status,
    release dates, fitted PDBs) sorted by EMD accession.
    """


    if blank(query):
        raise ValueError("query must be non-empty")
    result = run_search_spec(_emdb_client(), query, max_rows=max_rows)
    result["max_rows"] = max_rows
    return result


@mcp.tool(annotations=READ_ONLY)
def emdb_get_entry_section(
    emdb_ids: list[str],
    section: Literal["publications", "map", "sample", "imaging"],
) -> dict[str, Any]:
    """Fetch one detailed metadata section for EMDB entries.

    Sections: 'publications' — primary citation with complete author list,
    auxiliary citations, external references (PMID/DOI/ISSN); 'map' — file,
    format, data type, dimensions, voxel spacing, origin, axis order, cell,
    voxel statistics, contour levels, symmetry; 'sample' — per-macromolecule
    records (type, molecular weight, copies, EC number, source organism,
    sequence cross-refs) and per-supramolecule records; 'imaging' — microscope,
    voltage, electron source, detector, dose, imaging modes, defocus range,
    magnification, Cs, cryogen, grid/buffer/vitrification conditions (one record
    per microscopy session — entries can carry several).

    Unknown accessions are reported with "error": "not_found". Use
    emdb_get_entries first when you only need the headline record.
    """
    records = fetch_section_records(_emdb_client(), emdb_ids, section)
    return {"n_requested": len(emdb_ids), "section": section, "records": records}


@mcp.tool(annotations=READ_ONLY)
def emdb_get_validation(emdb_ids: list[str]) -> dict[str, Any]:
    """Fetch numeric validation-analysis metrics for EMDB entries.

    Per entry (from the EMDB /analysis route): Q-score, atom inclusion,
    recommended/predicted contour levels, FSC-derived resolution estimates and
    volume estimates, where the validation pipeline has computed them.
    available_blocks lists every block the validation service returned; sparse
    payloads (tomograms, model-free or historical entries) yield explicit nulls.
    Entries with no validation analysis report has_validation_analysis=false —
    never silently dropped.
    """
    records = fetch_validation_records(_emdb_client(), emdb_ids)
    return {"n_requested": len(emdb_ids), "records": records}





@mcp.tool(annotations=READ_ONLY)
def complexportal_get_complexes(complex_acs: list[str]) -> dict[str, Any]:
    """Fetch curated Complex Portal records by CPX accession.

    Each record: complex AC, recommended/systematic names + synonyms, species
    and taxid, participant list with stoichiometry (min/max copies), biological
    role and interactor type, evidence ECO code, GO annotations, and
    cross-references — the manually curated description of a stable
    macromolecular complex. Records come back in input order; unknown accessions
    are listed in not_found rather than silently dropped.

    For binary interaction *evidence* (who binds whom in which experiment) use
    the intact_* tools instead.
    """
    out = fetch_complexes(complex_acs, client=_cpx_client())
    out["n_requested"] = len(complex_acs)
    return out


@mcp.tool(annotations=READ_ONLY)
def complexportal_search_by_participant(
    accession: str,
    participants_only: bool = True,
) -> dict[str, Any]:
    """Search Complex Portal for complexes containing a molecule.

    `accession` is a participant accession — UniProt (e.g. 'P04637'), ChEBI, or
    RNAcentral. With participants_only=true (default) the search is
    field-qualified (pxref:<accession>) so only complexes that actually contain
    the molecule as a curated participant are returned; with false the bare
    accession is matched as free text too (descriptions, names), which
    over-reports but can catch mentions.

    All result pages are retrieved and the row count is verified against the
    service-reported total (total_reported == total_retrieved, or the call
    fails loudly). Hits are compact records (complex_ac, name, species,
    description) sorted by complex accession; fetch full detail with
    complexportal_get_complexes.
    """




    if blank(accession):
        raise ValueError("accession must be non-empty")
    return search_by_participant(
        accession, client=_cpx_client(), participants_only=participants_only
    )





@mcp.tool(annotations=READ_ONLY)
def intact_fetch_interactions(
    query: str,
    min_mi_score: float = 0.0,
    max_mi_score: float = 1.0,
    interactor_species: list[str] | None = None,
    max_records_returned: int = 500,
) -> dict[str, Any]:
    """Retrieve ALL IntAct binary interactions matching a query, MI-score filtered.

    `query` is a UniProt accession (e.g. 'P04637'), gene symbol, free text, or
    any IntAct Solr query. Retrieval is a complete paginated sweep verified
    against the server-reported total (total_elements == n_records, or the call
    fails loudly — silent truncation is impossible). min_mi_score/max_mi_score
    filter server-side on the IntAct MI confidence score (0.45 is a common
    "medium confidence" floor); interactor_species filters by species name or
    taxid (e.g. ["Homo sapiens"] or ["9606"]).

    Records are slim and structured: interactor pair (IntAct ACs, database
    identifiers, molecule names, species/taxids), interaction type, detection
    method (+MI id), experimental roles, host organism, MI score, PubMed id,
    first author, source database — sorted by descending MI score.

    Output lists at most max_records_returned records (records_truncated=true
    when the full verified sweep was larger; n_records always reports the true
    total). Large queries (e.g. CFTR ~10k interactions) take minutes at the
    polite 2 req/s pace — narrow with min_mi_score or species when possible.
    """




    if blank(query):
        raise ValueError("query must be non-empty")
    result = fetch_interactions(
        query,
        min_mi_score=min_mi_score,
        max_mi_score=max_mi_score,
        interactor_species=interactor_species,
        client=_intact_client(),
    )
    records, truncated = _cap(result.get("records", []), max_records_returned)
    result["records"] = records
    result["n_records_returned"] = len(records)
    result["records_truncated"] = truncated
    return result


@mcp.tool(annotations=READ_ONLY)
def intact_get_interactor(query: str) -> dict[str, Any]:
    """Resolve a molecule to its IntAct interactor record(s).

    `query` is a UniProt accession, gene symbol, or IntAct interactor AC (e.g.
    'EBI-7090529'). Returns ALL matching interactor records with an explicit
    n_matches — a UniProt accession can resolve to the canonical protein plus
    chain/isoform interactors, and this tool never silently picks one. Each
    record: interactor_ac, preferred_identifier, name, species, taxid, molecule
    type, and the interaction_count seen by IntAct (useful for sizing an
    intact_fetch_interactions sweep).
    """
    if blank(query):
        raise ValueError("query must be non-empty")
    return get_interactor(query, client=_intact_client())


@mcp.tool(annotations=READ_ONLY)
def intact_get_interaction_details(
    interaction_ac: str,
    include_participants: bool = True,
) -> dict[str, Any]:
    """Full curated detail for ONE IntAct interaction AC (e.g. 'EBI-15635490').

    Returns interaction type, host organism, detection method, publication,
    cross-references, annotations, kinetic/affinity parameters and confidences,
    plus per-participant records (identifier, species, biological and
    experimental role, participant detection methods) unless
    include_participants=false. Get interaction ACs from
    intact_fetch_interactions records. Unknown ACs return
    {"interaction_ac", "error": "not_found"}.
    """
    return get_interaction_details(
        interaction_ac,
        include_participants=include_participants,
        client=_intact_client(),
    )


@mcp.tool(annotations=READ_ONLY)
def intact_build_network(
    seed_accessions: list[str],
    min_mi_score: float = 0.45,
    max_interactors_expanded: int = 25,
    interactor_species: list[str] | None = None,
) -> dict[str, Any]:
    """Build a depth-1 IntAct interaction network around seed proteins.

    `seed_accessions` are UniProt accessions. Step 1: a complete, count-verified
    MI-score-filtered interaction sweep per seed. Step 2: the partners of every
    seed edge plus the seeds form the node set. Step 3: partner-partner edges
    are only discoverable by querying the partners themselves, so up to
    max_interactors_expanded partners are queried (most-connected first, ties
    by identifier) and edges with both endpoints inside the node set are kept.

    The expansion block reports exactly which partners were / were not expanded
    (expansion.complete=false means more partner-partner edges may exist).
    Output: nodes, edges (with MI score, detection method, PubMed id),
    per-seed sweep stats. Keep seeds few and min_mi_score >= 0.45 — every
    expansion is a full paginated sweep at 2 req/s.
    """
    return build_network(
        seed_accessions,
        min_mi_score=min_mi_score,
        max_interactors_expanded=max_interactors_expanded,
        interactor_species=interactor_species,
        client=_intact_client(),
    )





@mcp.tool(annotations=READ_ONLY)
def pdb_search_structures(
    text: str | None = None,
    organism: str | None = None,
    taxonomy_id: int | None = None,
    uniprot_accession: str | None = None,
    experimental_method: str | None = None,
    max_resolution_angstrom: float | None = None,
    ligand_comp_id: str | None = None,
    include_computed_models: bool = False,
    max_rows: int = 100,
) -> dict[str, Any]:
    """Search RCSB PDB entries by attribute filters; paged, capped + flagged.

    All filters AND together; at least one is required. `text` is a full-text
    relevance query ('p53 DNA binding domain'); `organism` is an exact
    source-organism lineage name ('Homo sapiens' — matches at any lineage
    level, so 'Eukaryota' works too); `taxonomy_id` an NCBI taxid (9606);
    `uniprot_accession` finds entries whose polymer entities map to that
    UniProt ('P04637' -> every p53 structure); `experimental_method` is the
    PDB vocabulary ('X-RAY DIFFRACTION', 'ELECTRON MICROSCOPY', 'SOLUTION
    NMR', ... — case-insensitive, unknown values error with the full list);
    `max_resolution_angstrom` keeps entries at or below that resolution;
    `ligand_comp_id` requires a bound nonpolymer component by chem-comp id
    ('ZN', 'ATP', 'HEM'). include_computed_models=true adds computed structure
    models (e.g. AlphaFold) to the default experimental-only results.

    Returns total_count (the API's own match total — ground truth),
    n_retrieved, truncated (true iff total_count > n_retrieved; max_rows,
    1..1000, caps retrieval), and records [{pdb_id, score}] in relevance
    order. Identifiers only — chain to pdb_get_structures for metadata.
    """





    reject_blank(text=text, organism=organism,
                 uniprot_accession=uniprot_accession,
                 experimental_method=experimental_method,
                 ligand_comp_id=ligand_comp_id)
    result = search_structures(
        _pdb_client(),
        text=text,
        organism=organism,
        taxonomy_id=taxonomy_id,
        uniprot_accession=uniprot_accession,
        experimental_method=experimental_method,
        max_resolution=max_resolution_angstrom,
        ligand_comp_id=ligand_comp_id,
        include_computed_models=include_computed_models,
        max_rows=max_rows,
    )
    result["max_rows"] = max_rows
    return result






@mcp.tool(annotations=READ_ONLY, allow_blank={"pdb_ids"})
def pdb_get_structures(pdb_ids: list[str]) -> dict[str, Any]:
    """Fetch entry-level summaries for PDB entries (batch, max 25 ids).

    Accepts 4-character PDB ids in any case ('1tup' == '1TUP'; duplicates are
    de-duplicated). Each record: title, experimental methods, resolution in
    Angstrom (null for methods without one, e.g. NMR), determination
    methodology (experimental vs computational), deposit/release/revision
    dates and status, molecular weight (kDa), assembly and entity counts
    (protein/DNA/RNA polymer + nonpolymer), bound ligand chem-comp ids,
    polymer/nonpolymer entity id lists (inputs for pdb_get_entities /
    pdb_get_ligands), and the primary citation (title, journal, year, authors,
    PubMed id, DOI). Unknown ids come back as {"pdb_id", "error":
    "not_found"} — never silently dropped. Metadata only; coordinate files
    are never downloaded.
    """




    cleaned: list[str] = []
    seen: set[str] = set()
    n_blank = 0
    n_duplicate = 0
    for raw in pdb_ids:
        pid = raw.strip()
        if not pid:
            n_blank += 1
        elif pid.upper() in seen:
            n_duplicate += 1
        else:
            seen.add(pid.upper())
            cleaned.append(pid)
    if len(cleaned) > PDB_MAX_IDS:
        raise ValueError(
            f"{len(cleaned)} unique ids requested; max {PDB_MAX_IDS} per call — "
            f"split the batch")
    records = pdb_fetch_entry_records(_pdb_client(), cleaned)
    return {
        "n_requested": len(pdb_ids),
        "n_unique": len(cleaned),
        "n_blank_skipped": n_blank,
        "n_duplicate_skipped": n_duplicate,
        "records": records,
    }


@mcp.tool(annotations=READ_ONLY)
def pdb_get_entities(
    pdb_id: str,
    entity_ids: list[str] | None = None,
    include_sequences: bool = False,
    max_bytes: int = 400_000,
) -> dict[str, Any]:
    """Polymer entity details for one PDB entry, incl. UniProt mappings.

    With entity_ids=null every polymer entity of the entry is fetched, capped
    at 25 with truncated=true and n_polymer_entities reporting the entry's
    true count (large assemblies like ribosomes carry 50+ — get the full id
    list from pdb_get_structures' polymer_entity_ids and page with explicit
    subsets like ["26", "27"]); with an explicit entity_ids subset the entry
    total is not fetched, so n_polymer_entities is null; an explicit
    entity_ids list larger than 25
    errors. Each record: description, polymer type (Protein / DNA /
    RNA), sequence length, mutation count, deposited copies, chain ids
    (asym + author), source organisms with taxids, UniProt accessions with
    per-entity sequence coverage (SIFTS), and UniProt-aligned regions
    (entity-seq vs reference-seq coordinates). Unknown entity ids are listed
    in not_found; an unknown entry id errors.

    include_sequences=true adds the canonical one-letter sequence per entity;
    if the combined sequences exceed max_bytes (default 400000) they are
    omitted and sequences_omitted explains why — metadata always survives.
    """
    out = fetch_entity_records(
        _pdb_client(), pdb_id, entity_ids=entity_ids,
        include_sequences=include_sequences,
    )
    if include_sequences:
        total = sum(len((r.get("sequence") or "").encode()) for r in out["records"])
        if total > max_bytes:
            for r in out["records"]:
                r.pop("sequence", None)
            out["sequences_omitted"] = (
                f"combined sequences are {total} bytes > max_bytes={max_bytes}; "
                f"re-call with fewer entity_ids or a larger max_bytes")
    return out


@mcp.tool(annotations=READ_ONLY)
def pdb_get_ligands(pdb_id: str, max_ligands: int = 25) -> dict[str, Any]:
    """Bound ligands (nonpolymer components) of one PDB entry, with chemistry.

    Walks the entry's nonpolymer entities and resolves each chemical
    component: per ligand — entity id, chem-comp id ('ZN', 'ATP'),
    description, deposited copy count, author chain ids, and a chem_comp
    block (name, formula, formula weight, formal charge, component type,
    InChIKey, stereo SMILES). Waters are not nonpolymer entities in the PDB
    data model and never appear. Entries with no ligands return ligands: [].

    n_nonpolymer_entities is the entry's true count; truncated=true when it
    exceeds max_ligands (clamped to 1..25, which bounds the request budget) —
    never silently dropped. Entities/components the data API no longer serves
    are reported inline with "error": "not_found" (partial results, not an
    aborted call). An unknown entry id errors.
    """
    return fetch_ligand_records(_pdb_client(), pdb_id, max_ligands=max_ligands)





@mcp.tool(annotations=READ_ONLY)
def alphafold_get_prediction(
    uniprot_accession: str,
    include_sequence: bool = False,
) -> dict[str, Any]:
    """AlphaFold DB predicted-structure metadata for one UniProt accession.

    Returns has_model, n_models and per-model records. A single accession can
    carry several models (canonical + isoforms like 'P04637-9', and community
    providers beyond the Google DeepMind monomer pipeline — provider_id /
    tool_used identify them). Each model: entry id, UniProt annotation (id,
    description, gene, organism, taxid, reviewed flags), sequence coordinates
    and length, global pLDDT (globalMetricValue, 0-100) plus the fraction of
    residues per pLDDT confidence bin (very_low <50, low 50-70, confident
    70-90, very_high >90), model version info and creation date, and download
    URLs (cif/bcif/pdb coordinates, PAE JSON + image, per-residue pLDDT JSON,
    MSA, AlphaMissense CSV where available) — URLs only, payloads are never
    downloaded; fetch them yourself if needed.

    Accessions without a prediction return has_model=false (not an error);
    malformed identifiers return an explicit "error" field.
    include_sequence=true adds the model sequence (protein one-letter).
    """
    return fetch_prediction_record(
        _afdb_client(), uniprot_accession, include_sequence=include_sequence)





@mcp.tool(annotations=READ_ONLY, allow_blank={"uniprot_accessions"})
def alphafold_check_coverage(uniprot_accessions: list[str]) -> dict[str, Any]:
    """Batch AlphaFold DB coverage check (max 40 unique UniProt accessions).

    Blank entries and duplicates are stripped before the batch cap applies,
    and disclosed: n_requested == n_unique + n_blank_skipped +
    n_duplicate_skipped always reconciles. One compact record per unique
    accession, in input order: has_model, n_models, and the primary
    (first-listed) model's model_entity_id, latest_version, global_plddt and
    sequence_length. Accessions with no prediction report has_model=false;
    malformed ones carry an explicit "error" field — never silently dropped.
    Use to triage which proteins of a set have usable predicted structures
    before pulling full records with alphafold_get_prediction.
    """
    out = fetch_coverage_records(_afdb_client(), uniprot_accessions)
    out["n_requested"] = len(uniprot_accessions)
    return out


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
