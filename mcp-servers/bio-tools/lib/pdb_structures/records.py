"""Record builders over the RCSB data API (entry / polymer entity / ligands)."""
from __future__ import annotations

from .client import PDBClient, NotFoundError



MAX_IDS_PER_CALL = 25


def normalize_pdb_id(pdb_id: str) -> str:
    return pdb_id.strip().upper()





def parse_entry(raw: dict) -> dict:
    info = raw.get("rcsb_entry_info", {}) or {}
    acc = raw.get("rcsb_accession_info", {}) or {}
    ids = raw.get("rcsb_entry_container_identifiers", {}) or {}
    cit = raw.get("rcsb_primary_citation") or {}
    resolutions = info.get("resolution_combined") or []
    return {
        "pdb_id": raw.get("rcsb_id"),
        "title": (raw.get("struct") or {}).get("title"),
        "experimental_methods": [e.get("method") for e in raw.get("exptl", []) or []],
        "resolution_angstrom": min(resolutions) if resolutions else None,
        "resolutions_combined": resolutions,
        "structure_determination_methodology": info.get("structure_determination_methodology"),
        "deposit_date": acc.get("deposit_date"),
        "initial_release_date": acc.get("initial_release_date"),
        "revision_date": acc.get("revision_date"),
        "status_code": acc.get("status_code"),
        "molecular_weight_kda": info.get("molecular_weight"),
        "assembly_count": info.get("assembly_count"),
        "polymer_entity_count": info.get("polymer_entity_count"),
        "polymer_entity_count_protein": info.get("polymer_entity_count_protein"),
        "polymer_entity_count_dna": info.get("polymer_entity_count_DNA"),
        "polymer_entity_count_rna": info.get("polymer_entity_count_RNA"),
        "nonpolymer_entity_count": info.get("nonpolymer_entity_count"),
        "polymer_composition": info.get("polymer_composition"),
        "ligand_comp_ids": info.get("nonpolymer_bound_components") or [],
        "polymer_entity_ids": ids.get("polymer_entity_ids") or [],
        "nonpolymer_entity_ids": ids.get("non_polymer_entity_ids") or [],
        "citation": {
            "title": cit.get("title"),
            "journal": cit.get("rcsb_journal_abbrev") or cit.get("journal_abbrev"),
            "year": cit.get("year"),
            "authors": cit.get("rcsb_authors") or [],
            "pubmed_id": cit.get("pdbx_database_id_PubMed"),
            "doi": cit.get("pdbx_database_id_DOI"),
        } if cit else None,
    }


def fetch_entry_records(client: PDBClient, pdb_ids: list[str]) -> list[dict]:
    """Entry summaries in input order; unknown ids -> {"pdb_id", "error": "not_found"}."""
    seen: set[str] = set()
    records: list[dict] = []
    for raw_id in pdb_ids:
        pdb_id = normalize_pdb_id(raw_id)
        if not pdb_id or pdb_id in seen:
            continue
        seen.add(pdb_id)
        try:
            records.append(parse_entry(client.get_data("entry", pdb_id)))
        except NotFoundError:
            records.append({"pdb_id": pdb_id, "error": "not_found"})
    return records





def parse_polymer_entity(raw: dict, include_sequence: bool = False) -> dict:
    ent = raw.get("rcsb_polymer_entity", {}) or {}
    ids = raw.get("rcsb_polymer_entity_container_identifiers", {}) or {}
    poly = raw.get("entity_poly", {}) or {}
    organisms = raw.get("rcsb_entity_source_organism", []) or []
    aligns = raw.get("rcsb_polymer_entity_align", []) or []
    record = {
        "rcsb_id": raw.get("rcsb_id"),
        "entry_id": ids.get("entry_id"),
        "entity_id": ids.get("entity_id"),
        "description": ent.get("pdbx_description"),
        "polymer_type": poly.get("rcsb_entity_polymer_type"),
        "polymer_type_detail": poly.get("type"),
        "sequence_length": poly.get("rcsb_sample_sequence_length"),
        "mutation_count": poly.get("rcsb_mutation_count"),
        "n_copies_deposited": ent.get("pdbx_number_of_molecules"),
        "molecular_weight_kda": ent.get("formula_weight"),
        "asym_ids": ids.get("asym_ids") or [],
        "auth_asym_ids": ids.get("auth_asym_ids") or [],
        "source_organisms": [
            {"scientific_name": o.get("scientific_name"),
             "ncbi_taxonomy_id": o.get("ncbi_taxonomy_id")}
            for o in organisms
        ],
        "uniprot_ids": ids.get("uniprot_ids") or [],
        "reference_sequence_identifiers": [
            {"database_name": r.get("database_name"),
             "database_accession": r.get("database_accession"),
             "entity_sequence_coverage": r.get("entity_sequence_coverage"),
             "reference_sequence_coverage": r.get("reference_sequence_coverage")}
            for r in ids.get("reference_sequence_identifiers") or []
        ],
        "uniprot_aligned_regions": [
            {"accession": a.get("reference_database_accession"),
             "regions": a.get("aligned_regions") or []}
            for a in aligns
            if a.get("reference_database_name") == "UniProt"
        ],
    }
    if include_sequence:
        record["sequence"] = poly.get("pdbx_seq_one_letter_code_can")
    return record


def fetch_entity_records(
    client: PDBClient,
    pdb_id: str,
    entity_ids: list[str] | None = None,
    include_sequences: bool = False,
) -> dict:
    """Polymer entity records for one entry.

    With ``entity_ids=None`` all polymer entities of the entry are fetched
    (one extra request resolves the entity-id list from the entry record),
    capped at ``MAX_IDS_PER_CALL`` with ``truncated=true`` —
    ``n_polymer_entities`` reports the entry's true count on that branch
    only. With an explicit ``entity_ids`` subset the entry record is never
    fetched, so ``n_polymer_entities`` is null (the requested subset says
    nothing about the entry's total) and ``truncated`` is false. An explicit
    list larger than the cap errors (the caller can split it). Unknown
    entity ids are reported in ``not_found``.
    """
    pdb_id = normalize_pdb_id(pdb_id)
    if entity_ids is None:
        entry = client.get_data("entry", pdb_id)
        all_ids = (entry.get("rcsb_entry_container_identifiers") or {}).get(
            "polymer_entity_ids") or []
        ids = all_ids[:MAX_IDS_PER_CALL]
        n_polymer_entities = len(all_ids)
    else:
        all_ids = [str(e).strip() for e in entity_ids if str(e).strip()]
        if len(all_ids) > MAX_IDS_PER_CALL:
            raise ValueError(
                f"{len(all_ids)} polymer entities requested; max {MAX_IDS_PER_CALL} "
                f"per call — pass an explicit entity_ids subset")
        ids = all_ids
        n_polymer_entities = None
    records: list[dict] = []
    not_found: list[str] = []
    for eid in ids:
        try:
            raw = client.get_data("polymer_entity", pdb_id, eid)
            records.append(parse_polymer_entity(raw, include_sequence=include_sequences))
        except NotFoundError:
            not_found.append(eid)
    return {
        "pdb_id": pdb_id,
        "n_polymer_entities": n_polymer_entities,
        "polymer_entity_ids": ids,
        "truncated": len(all_ids) > len(ids),
        "records": records,
        "not_found": not_found,
    }





def parse_chem_comp(raw: dict) -> dict:
    comp = raw.get("chem_comp", {}) or {}
    desc = raw.get("rcsb_chem_comp_descriptor", {}) or {}
    return {
        "comp_id": comp.get("id"),
        "name": comp.get("name"),
        "formula": comp.get("formula"),
        "formula_weight": comp.get("formula_weight"),
        "formal_charge": comp.get("pdbx_formal_charge"),
        "type": comp.get("type"),
        "inchikey": desc.get("InChIKey"),
        "smiles": desc.get("SMILES_stereo") or desc.get("SMILES"),
    }


def fetch_ligand_records(client: PDBClient, pdb_id: str, max_ligands: int = MAX_IDS_PER_CALL) -> dict:
    """Bound ligands of one entry, with chemical-component detail.

    Walks entry -> nonpolymer entities -> chem comps (unique comp ids fetched
    once). ``max_ligands`` is clamped to 1..MAX_IDS_PER_CALL (the request
    budget). ``truncated=true`` with ``n_nonpolymer_entities`` reporting the
    real count when the entry carries more nonpolymer entities than the clamp.
    Entities/components the data API no longer serves are reported inline with
    "error": "not_found" — partial results, never an aborted call or a silent
    drop.
    """
    pdb_id = normalize_pdb_id(pdb_id)
    max_ligands = max(1, min(int(max_ligands), MAX_IDS_PER_CALL))
    entry = client.get_data("entry", pdb_id)
    np_ids = (entry.get("rcsb_entry_container_identifiers") or {}).get(
        "non_polymer_entity_ids") or []
    use_ids = np_ids[:max_ligands]
    entities: list[dict] = []
    for eid in use_ids:
        try:
            raw = client.get_data("nonpolymer_entity", pdb_id, eid)
        except NotFoundError:
            entities.append({"entity_id": eid, "comp_id": None,
                             "error": "not_found"})
            continue
        ent_ids = raw.get("rcsb_nonpolymer_entity_container_identifiers", {}) or {}
        ent = raw.get("rcsb_nonpolymer_entity", {}) or {}
        entities.append({
            "entity_id": ent_ids.get("entity_id"),
            "comp_id": ent_ids.get("nonpolymer_comp_id"),
            "description": ent.get("pdbx_description"),
            "n_copies_deposited": ent.get("pdbx_number_of_molecules"),
            "auth_asym_ids": ent_ids.get("auth_asym_ids") or [],
        })
    comps: dict[str, dict] = {}
    for comp_id in sorted({e["comp_id"] for e in entities if e["comp_id"]}):
        try:
            comps[comp_id] = parse_chem_comp(client.get_data("chemcomp", comp_id))
        except NotFoundError:
            comps[comp_id] = {"comp_id": comp_id, "error": "not_found"}
    ligands = [dict(e, chem_comp=comps.get(e["comp_id"])) for e in entities]
    return {
        "pdb_id": pdb_id,
        "n_nonpolymer_entities": len(np_ids),
        "n_returned": len(ligands),
        "truncated": len(np_ids) > len(use_ids),
        "ligands": ligands,
    }
