"""Marshal fleet-tool results into the ORIGINAL ChEMBL connector's output
formats (see mcp-servers/_snapshots/original_outputs/mcp-chembl/).

Retrieval lives in the fleet packages (chembl-drug-search, chembl-bioactivity,
chembl-targets); this module only reshapes their raw ChEMBL REST payloads.

The original connector serialized compact JSON (no whitespace) and applied a
handful of type conversions on top of the raw REST records (ChEMBL returns
many numerics as strings): those conversions are reproduced here exactly as
observed in the captures.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any


MAX_XREFS_PER_COMPONENT = 50


def compact_json(obj: object) -> str:
    """Serialize like the original ChEMBL connector (compact, no whitespace)."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _num(x: Any) -> Any:
    """ChEMBL emits numerics as strings ("4.0", "1.31"); the original
    connector emitted them as numbers. None passes through."""
    if x is None or isinstance(x, (int, float)):
        return x
    try:
        return float(x)
    except (TypeError, ValueError):
        return x


def _bool(x: Any) -> Any:
    """0/1 (or bool) -> bool; None passes through."""
    return None if x is None else bool(x)








def molecule_properties_block(mp: dict | None) -> dict | None:
    if mp is None:
        return None
    return {
        "alogp": _num(mp.get("alogp")),
        "aromatic_rings": mp.get("aromatic_rings"),
        "full_mwt": _num(mp.get("full_mwt")),
        "hba": mp.get("hba"),
        "hbd": mp.get("hbd"),
        "heavy_atoms": mp.get("heavy_atoms"),
        "psa": _num(mp.get("psa")),
        "rtb": mp.get("rtb"),
        "ro3_pass": mp.get("ro3_pass"),
        "num_ro5_violations": mp.get("num_ro5_violations"),
        "qed_weighted": _num(mp.get("qed_weighted")),
        "molecular_formula": mp.get("full_molformula"),
        "mw_freebase": _num(mp.get("mw_freebase")),
        "np_likeness_score": _num(mp.get("np_likeness_score")),
        "med_chem_friendly": mp.get("med_chem_friendly"),
        "molecular_species": mp.get("molecular_species"),
    }




def compound_record(m: dict) -> dict:
    """Raw /molecule (or /similarity, /substructure) record -> the original
    connector's compound shape (key set, order and types per capture)."""
    structures = m.get("molecule_structures") or {}
    atc = m.get("atc_classifications") or []
    score = m.get("similarity", m.get("score"))
    return {
        "molecule_chembl_id": m.get("molecule_chembl_id"),
        "pref_name": m.get("pref_name"),
        "molecule_type": m.get("molecule_type"),
        "max_phase": _num(m.get("max_phase")),
        "first_approval": m.get("first_approval"),
        "oral": _bool(m.get("oral")),
        "parenteral": _bool(m.get("parenteral")),
        "topical": _bool(m.get("topical")),
        "black_box_warning": _bool(m.get("black_box_warning")),
        "therapeutic_flag": _bool(m.get("therapeutic_flag")),
        "natural_product": _bool(m.get("natural_product")),
        "withdrawn_flag": _bool(m.get("withdrawn_flag")),
        "molecule_properties": molecule_properties_block(m.get("molecule_properties")),
        "smiles": structures.get("canonical_smiles"),
        "inchi": structures.get("standard_inchi"),
        "inchi_key": structures.get("standard_inchi_key"),
        "synonyms": [s.get("molecule_synonym")
                     for s in (m.get("molecule_synonyms") or [])],
        "availability_type": m.get("availability_type"),
        "chirality": m.get("chirality"),
        "chemical_probe": m.get("chemical_probe"),
        "dosed_ingredient": _bool(m.get("dosed_ingredient")),
        "first_in_class": m.get("first_in_class"),
        "helm_notation": m.get("helm_notation"),
        "inorganic_flag": m.get("inorganic_flag"),
        "orphan": m.get("orphan"),
        "polymer_flag": m.get("polymer_flag"),
        "prodrug": m.get("prodrug"),
        "structure_type": m.get("structure_type"),
        "usan_stem": m.get("usan_stem"),
        "usan_stem_definition": m.get("usan_stem_definition"),
        "usan_substem": m.get("usan_substem"),
        "usan_year": m.get("usan_year"),
        "veterinary": m.get("veterinary"),
        "score": _num(score),
        "cross_references": m.get("cross_references") or None,
        "atc_classifications": [
            {"level1": None, "level1_description": None,
             "level2": None, "level2_description": None,
             "level3": None, "level3_description": None,
             "level4": None, "level4_description": None,
             "level5": code}
            for code in atc
        ] or None,
        "molecule_hierarchy": m.get("molecule_hierarchy"),
    }


def compound_search_response(records: list[dict], total: int | None) -> dict:
    compounds = [compound_record(m) for m in records]
    total = len(compounds) if total is None else total
    return {
        "count": len(compounds),
        "total": total,
        "compounds": compounds,


        "truncated": len(compounds) < total,
    }




def drug_record(m: dict, joined: dict | None = None) -> dict:
    """Full /molecule record (+ the fleet's indication-join record) -> the
    original connector's drug shape.

    The original's record mixed molecule fields with /drug-route placeholder
    keys that were always null in the capture (applicants, black_box,
    drug_type, research_codes, rule_of_five, synonyms, ...); those keys are
    kept (null) for shape parity. Type oddities are reproduced from the
    capture: oral/parenteral/therapeutic_flag/withdrawn_flag are bools while
    topical and black_box_warning are 0/1 ints.
    """
    joined = joined or {}
    rec = {
        "molecule_chembl_id": m.get("molecule_chembl_id"),
        "pref_name": m.get("pref_name"),
        "molecule_type": m.get("molecule_type"),
        "max_phase": _num(m.get("max_phase")),
        "first_approval": m.get("first_approval"),
        "oral": _bool(m.get("oral")),
        "parenteral": _bool(m.get("parenteral")),
        "therapeutic_flag": _bool(m.get("therapeutic_flag")),
        "indications": m.get("indications"),
        "applicants": m.get("applicants"),
        "atc_code_description": m.get("atc_code_description"),
        "availability_type": m.get("availability_type"),
        "biotherapeutic": m.get("biotherapeutic"),
        "black_box": m.get("black_box"),
        "black_box_warning": int(m["black_box_warning"])
        if m.get("black_box_warning") is not None else None,
        "chirality": m.get("chirality"),
        "drug_type": m.get("drug_type"),
        "first_in_class": m.get("first_in_class"),
        "helm_notation": m.get("helm_notation"),
        "molecule_properties": molecule_properties_block(m.get("molecule_properties")),
        "molecule_structures": m.get("molecule_structures"),
        "molecule_synonyms": m.get("molecule_synonyms"),
        "ob_patent": m.get("ob_patent"),
        "sc_patent": m.get("sc_patent"),
        "prodrug": m.get("prodrug"),
        "research_codes": m.get("research_codes"),
        "rule_of_five": m.get("rule_of_five"),
        "synonyms": m.get("synonyms"),
        "topical": int(m["topical"]) if m.get("topical") is not None else None,
        "usan_stem": m.get("usan_stem"),
        "usan_stem_definition": m.get("usan_stem_definition"),
        "usan_stem_substem": m.get("usan_stem_substem"),
        "usan_year": m.get("usan_year"),
        "withdrawn_flag": _bool(m.get("withdrawn_flag")),
    }



    if joined:
        rec["best_phase_for_ind"] = joined.get("best_phase_for_ind")
        rec["efo_terms"] = joined.get("efo_terms")
        rec["indication_rows"] = joined.get("indication_rows")
        rec["warning_summary"] = joined.get("warning_summary")
    return rec


def drug_search_response(pairs: list[tuple[dict, dict]], total: int,
                         indication_query: dict,
                         total_indication_rows: int | None) -> dict:
    drugs = [drug_record(m, joined) for m, joined in pairs]
    return {
        "count": len(drugs),
        "total": total,
        "drugs": drugs,

        "truncated": len(drugs) < total,
        "indication_query": indication_query,
        "total_indication_rows": total_indication_rows,
    }




def admet_response(molecule: dict | None, molecule_chembl_id: str) -> dict:
    if molecule is None:
        return {
            "found": False,
            "properties": None,

            "message": f"No molecule found for {molecule_chembl_id}",
        }
    mp = molecule.get("molecule_properties") or {}
    return {
        "found": True,
        "properties": {
            "molecule_chembl_id": molecule.get("molecule_chembl_id"),
            "alogp": _num(mp.get("alogp")),
            "molecular_weight": _num(mp.get("full_mwt")),
            "mw_freebase": _num(mp.get("mw_freebase")),
            "psa": _num(mp.get("psa")),
            "hba": mp.get("hba"),
            "hbd": mp.get("hbd"),
            "rtb": mp.get("rtb"),
            "aromatic_rings": mp.get("aromatic_rings"),
            "heavy_atoms": mp.get("heavy_atoms"),
            "num_ro5_violations": mp.get("num_ro5_violations"),
            "ro3_pass": mp.get("ro3_pass"),
            "qed_weighted": _num(mp.get("qed_weighted")),
            "molecular_formula": mp.get("full_molformula"),
        },
    }




def activity_record(a: dict) -> dict:
    """Raw /activity record -> the original's 45-key shape (order per capture).

    Numeric strings become numbers; an empty activity_properties list becomes
    null (as captured); ligand_efficiency sub-values become numbers.
    """
    le = a.get("ligand_efficiency")
    if isinstance(le, dict):
        le = {k: _num(v) for k, v in le.items()}
    props = a.get("activity_properties")
    return {
        "activity_id": a.get("activity_id"),
        "molecule_chembl_id": a.get("molecule_chembl_id"),
        "target_chembl_id": a.get("target_chembl_id"),
        "target_pref_name": a.get("target_pref_name"),
        "target_organism": a.get("target_organism"),
        "standard_type": a.get("standard_type"),
        "standard_relation": a.get("standard_relation"),
        "standard_value": _num(a.get("standard_value")),
        "standard_units": a.get("standard_units"),
        "pchembl_value": _num(a.get("pchembl_value")),
        "assay_chembl_id": a.get("assay_chembl_id"),
        "assay_description": a.get("assay_description"),
        "assay_type": a.get("assay_type"),
        "data_validity_comment": a.get("data_validity_comment"),
        "activity_comment": a.get("activity_comment"),
        "activity_properties": props or None,
        "action_type": a.get("action_type"),
        "bao_endpoint": a.get("bao_endpoint"),
        "bao_format": a.get("bao_format"),
        "bao_label": a.get("bao_label"),
        "canonical_smiles": a.get("canonical_smiles"),
        "data_validity_description": a.get("data_validity_description"),
        "document_chembl_id": a.get("document_chembl_id"),
        "document_journal": a.get("document_journal"),
        "document_year": a.get("document_year"),
        "ligand_efficiency": le,
        "molecule_pref_name": a.get("molecule_pref_name"),
        "parent_molecule_chembl_id": a.get("parent_molecule_chembl_id"),
        "potential_duplicate": a.get("potential_duplicate"),
        "qudt_units": a.get("qudt_units"),
        "uo_units": a.get("uo_units"),
        "record_id": a.get("record_id"),
        "src_id": a.get("src_id"),
        "toid": a.get("toid"),
        "standard_flag": a.get("standard_flag"),
        "standard_text_value": a.get("standard_text_value"),
        "standard_upper_value": _num(a.get("standard_upper_value")),
        "target_tax_id": a.get("target_tax_id"),
        "text_value": a.get("text_value"),
        "type": a.get("type"),
        "units": a.get("units"),
        "upper_value": _num(a.get("upper_value")),
        "value": _num(a.get("value")),
        "relation": a.get("relation"),
        "assay_variant_accession": a.get("assay_variant_accession"),
        "assay_variant_mutation": a.get("assay_variant_mutation"),
    }


def _activity_summary(activities: list[dict]) -> str:
    scored = [a for a in activities if a.get("pchembl_value") is not None]
    scored.sort(key=lambda a: (-a["pchembl_value"],
                               a.get("standard_value")
                               if a.get("standard_value") is not None else 0))
    top = scored[:3]
    if not top:
        return "No pChEMBL-scored activities in this result set"
    parts = [
        f"{a.get('target_pref_name')}: {a.get('standard_type')}="
        f"{a.get('standard_value')}{a.get('standard_units') or ''} "
        f"(pChEMBL={a['pchembl_value']:.2f})"
        for a in top
    ]
    return "Most potent activities: " + "; ".join(parts)


def bioactivity_response(raw_activities: list[dict], total: int | None) -> dict:
    activities = [activity_record(a) for a in raw_activities]
    total = len(activities) if total is None else total
    return {
        "count": len(activities),
        "total": total,
        "activities": activities,
        "summary": _activity_summary(activities),

        "truncated": len(activities) < total,
    }




def mechanism_record(m: dict) -> dict:
    """Raw /mechanism record -> original shape: direct_interaction and
    disease_efficacy become bools; everything else passes through."""
    return {
        "mec_id": m.get("mec_id"),
        "molecule_chembl_id": m.get("molecule_chembl_id"),
        "mechanism_of_action": m.get("mechanism_of_action"),
        "target_chembl_id": m.get("target_chembl_id"),
        "action_type": m.get("action_type"),
        "direct_interaction": _bool(m.get("direct_interaction")),
        "disease_efficacy": _bool(m.get("disease_efficacy")),
        "mechanism_comment": m.get("mechanism_comment"),
        "binding_site_comment": m.get("binding_site_comment"),
        "selectivity_comment": m.get("selectivity_comment"),
        "molecular_mechanism": m.get("molecular_mechanism"),
        "max_phase": m.get("max_phase"),
        "parent_molecule_chembl_id": m.get("parent_molecule_chembl_id"),
        "record_id": m.get("record_id"),
        "site_id": m.get("site_id"),
        "mechanism_refs": m.get("mechanism_refs"),
        "variant_sequence": m.get("variant_sequence"),
    }


def mechanism_response(raw_mechanisms: list[dict], total: int | None) -> dict:
    mechanisms = [mechanism_record(m) for m in raw_mechanisms]
    total = len(mechanisms) if total is None else total
    counts = Counter(m["action_type"] for m in mechanisms
                     if m.get("action_type"))
    if counts:
        summary = "Primary action types: " + ", ".join(
            f"{t} ({n})"
            for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    else:
        summary = "No mechanism of action records found"
    return {
        "count": len(mechanisms),
        "total": total,
        "mechanisms": mechanisms,
        "summary": summary,

        "truncated": len(mechanisms) < total,
    }




def target_record(t: dict) -> dict:
    """Raw /target record -> original shape.

    Per capture: each component gains a `gene_symbol` (first GENE_SYMBOL
    synonym) and drops the synonym list; xrefs gain an `xref_src_url` key
    (null upstream); an empty cross_references list becomes null; `score` is
    emitted (null — the /target route carries no relevance score).
    """
    components = []
    for c in t.get("target_components") or []:
        gene_symbol = next(
            (s.get("component_synonym")
             for s in (c.get("target_component_synonyms") or [])
             if s.get("syn_type") == "GENE_SYMBOL"),
            None)
        xrefs = c.get("target_component_xrefs") or []
        component = {
            "component_id": c.get("component_id"),
            "component_type": c.get("component_type"),
            "accession": c.get("accession"),
            "component_description": c.get("component_description"),
            "gene_symbol": gene_symbol,
            "relationship": c.get("relationship"),




            "target_component_xrefs": [
                {"xref_id": x.get("xref_id"),
                 "xref_name": x.get("xref_name"),
                 "xref_src_db": x.get("xref_src_db"),
                 "xref_src_url": x.get("xref_src_url")}
                for x in xrefs[:MAX_XREFS_PER_COMPONENT]
            ],
        }
        if len(xrefs) > MAX_XREFS_PER_COMPONENT:
            component["xrefs_truncated_from"] = len(xrefs)
        components.append(component)
    return {
        "target_chembl_id": t.get("target_chembl_id"),
        "pref_name": t.get("pref_name"),
        "target_type": t.get("target_type"),
        "organism": t.get("organism"),
        "tax_id": t.get("tax_id"),
        "components": components,
        "species_group_flag": _bool(t.get("species_group_flag")),
        "cross_references": t.get("cross_references") or None,
        "score": _num(t.get("score")),
    }


def target_search_response(raw_targets: list[dict], total: int | None) -> dict:
    targets = [target_record(t) for t in raw_targets]
    total = len(targets) if total is None else total
    return {
        "count": len(targets),
        "total": total,
        "targets": targets,

        "truncated": len(targets) < total,
    }
