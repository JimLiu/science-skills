"""mcp-chembl server — tool handlers + stdio entry point.

Tool names/schemas are served verbatim from ``schemas.json`` (captured from
the original hosted connector). Retrieval is delegated to the fleet packages
(chembl-drug-search, chembl-bioactivity, chembl-targets); ``marshal``
reshapes raw ChEMBL REST payloads into the original output formats.
"""

from __future__ import annotations

import urllib.parse
from functools import lru_cache

from mcp_servers_common import Tier1Server, load_schemas
from mcp_servers_common.gate import apply_gate_tier1

from . import marshal

DEFAULT_LIMIT = 20




SEARCH_WALK_CAP = 10_000


def _limit(args: dict, cap: int = 1000) -> int:


    return max(1, min(cap, int(args.get("limit", DEFAULT_LIMIT))))

















def _usable(v: object) -> bool:
    """True when a criterion value actually filters: a non-blank string, or
    any non-None non-string (numbers are never blank)."""
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    return True


def _any_blank(args: dict, keys: tuple[str, ...]) -> bool:
    """True when at least one criterion in `keys` is present but blank."""
    return any(
        isinstance(args.get(k), str) and not str(args[k]).strip()
        for k in keys
    )



@lru_cache(maxsize=1)
def _drugs():
    from chembl_drug_search import ChemblDrugSearchClient
    return ChemblDrugSearchClient()


@lru_cache(maxsize=1)
def _bio():
    from chembl_bioactivity import ChEMBLClient
    return ChEMBLClient()


@lru_cache(maxsize=1)
def _targets():
    from chembl_targets import ChemblTargetsClient
    return ChemblTargetsClient()


def _single_page(client, resource: str, params: dict, limit: int) -> tuple[list, int]:
    """One bounded page via the fleet ChEMBLClient's paced/retrying session,
    with the verified upstream total from page_meta.total_count.

    The original connector used the same single-call pattern but the total it
    reported is preserved here, and every list response additionally carries
    a ``truncated`` flag (see README: deliberate behavior changes).
    """
    from chembl_bioactivity.client import RESOURCES
    collection_key, sort_key = RESOURCES[resource]
    payload = client._get(resource, {**params, "limit": limit, "offset": 0,
                                     "order_by": sort_key})
    meta = payload.get("page_meta") or {}
    return payload.get(collection_key) or [], meta.get("total_count")




def compound_search(args: dict) -> str:
    from chembl_drug_search.client import MoleculeNotFoundError

    limit = _limit(args)
    max_phase = args.get("max_phase")
    client = _drugs()


    if not any(_usable(args.get(k)) for k in ("chembl_id", "smiles", "name")):
        return marshal.compact_json(marshal.compound_search_response([], 0))

    if _usable(args.get("chembl_id")):
        try:
            records = client.get_molecules([args["chembl_id"]])
        except MoleculeNotFoundError:
            records = []
        if max_phase is not None:
            records = [m for m in records
                       if m.get("max_phase") is not None
                       and float(m["max_phase"]) == float(max_phase)]
        return marshal.compact_json(
            marshal.compound_search_response(records[:limit], len(records)))

    if _usable(args.get("smiles")):
        threshold = args.get("similarity_threshold")








        if threshold is not None:
            records, total = client.similarity_search(
                args["smiles"], int(threshold), max_records=SEARCH_WALK_CAP)
        else:
            records, total = client.substructure_search(
                args["smiles"], max_records=SEARCH_WALK_CAP)


            records = sorted(
                records, key=lambda r: r.get("molecule_chembl_id") or "")
        walk_truncated = total is not None and total > len(records)
        if max_phase is not None:



            records = [m for m in records
                       if m.get("max_phase") is not None
                       and float(m["max_phase"]) == float(max_phase)]
        resp = marshal.compound_search_response(records[:limit], len(records))
        if walk_truncated:



            resp["walk_truncated"] = True
            resp["upstream_total"] = total
        return marshal.compact_json(resp)




    params = {"molecule_synonyms__molecule_synonym__icontains": args["name"]}
    if max_phase is not None:
        params["max_phase"] = max_phase
    records, total = client.paginate("/molecule.json", "molecules", params,
                                     max_records=limit)
    if not records:
        params = {"pref_name__icontains": args["name"]}
        if max_phase is not None:
            params["max_phase"] = max_phase
        records, total = client.paginate("/molecule.json", "molecules", params,
                                         max_records=limit)
    return marshal.compact_json(marshal.compound_search_response(records, total))




def drug_search(args: dict) -> str:
    limit = _limit(args)
    client = _drugs()
    indication = args.get("indication")
    if not _usable(indication):


        return marshal.compact_json(marshal.drug_search_response(
            [], 0,
            indication_query={
                "term": indication if isinstance(indication, str) else "",
                "match_field": "efo",
                "only_approved": bool(args.get("only_approved")),
            },
            total_indication_rows=0))




    post_filtered = bool(
        args.get("molecule_chembl_id") or args.get("drug_name")
        or args.get("max_phase") is not None)
    res = client.search_drugs_by_indication(
        args["indication"], only_approved=bool(args.get("only_approved")),
        max_drugs=None if post_filtered else limit)
    drugs = res["drugs"]

    if args.get("molecule_chembl_id"):
        drugs = [d for d in drugs
                 if d["parent_molecule_chembl_id"] == args["molecule_chembl_id"]]
    if args.get("drug_name"):
        needle = args["drug_name"].lower()
        drugs = [d for d in drugs if needle in (d.get("pref_name") or "").lower()]
    if args.get("max_phase") is not None:
        wanted = float(args["max_phase"])
        drugs = [d for d in drugs
                 if d.get("max_phase") is not None
                 and float(d["max_phase"]) >= wanted]

    total = len(drugs) if post_filtered else res["total_parents"]
    page = drugs[:limit]





    mol_by_id = {
        m["molecule_chembl_id"]: m
        for m in (client.get_molecules(
            [d["parent_molecule_chembl_id"] for d in page],
            missing_ok=True) if page else [])
    }
    pairs = [(mol_by_id.get(d["parent_molecule_chembl_id"], {}), d)
             for d in page]
    return marshal.compact_json(marshal.drug_search_response(
        pairs, total,
        indication_query=res["indication_query"],
        total_indication_rows=res["total_indication_rows"]))




def get_admet(args: dict) -> str:
    mol_id = args["molecule_chembl_id"]






    records = _bio().get_molecules([mol_id])
    return marshal.compact_json(
        marshal.admet_response(records[0] if records else None, mol_id))




_BIOACTIVITY_STR_CRITERIA = ("molecule_chembl_id", "target_chembl_id",
                             "activity_type", "unit")


def get_bioactivity(args: dict) -> str:
    limit = _limit(args)
    params: dict = {}
    if _usable(args.get("molecule_chembl_id")):
        params["molecule_chembl_id"] = args["molecule_chembl_id"]
    if _usable(args.get("target_chembl_id")):
        params["target_chembl_id"] = args["target_chembl_id"]
    if _usable(args.get("activity_type")):
        params["standard_type"] = args["activity_type"]
    if args.get("min_pchembl") is not None:
        params["pchembl_value__gte"] = args["min_pchembl"]
    if args.get("min_value") is not None:
        params["standard_value__gte"] = args["min_value"]
    if args.get("max_value") is not None:
        params["standard_value__lte"] = args["max_value"]
    if _usable(args.get("unit")):
        params["standard_units"] = args["unit"]
    if not params and _any_blank(args, _BIOACTIVITY_STR_CRITERIA):


        return marshal.compact_json(marshal.bioactivity_response([], 0))
    activities, total = _single_page(_bio(), "activity", params, limit)
    return marshal.compact_json(marshal.bioactivity_response(activities, total))




_MECHANISM_CRITERIA = ("molecule_chembl_id", "target_chembl_id",
                       "action_type")


def get_mechanism(args: dict) -> str:
    limit = _limit(args)
    params: dict = {}
    if _usable(args.get("target_chembl_id")):
        params["target_chembl_id"] = args["target_chembl_id"]
    if _usable(args.get("action_type")):
        params["action_type"] = args["action_type"]
    mol_id = args.get("molecule_chembl_id")
    if _usable(mol_id):
        params["molecule_chembl_id"] = mol_id
    if not params and _any_blank(args, _MECHANISM_CRITERIA):


        return marshal.compact_json(marshal.mechanism_response([], 0))
    mechanisms, total = _single_page(_bio(), "mechanism", params, limit)
    if not mechanisms and _usable(mol_id):


        params.pop("molecule_chembl_id")
        params["parent_molecule_chembl_id"] = mol_id
        mechanisms, total = _single_page(_bio(), "mechanism", params, limit)
    return marshal.compact_json(marshal.mechanism_response(mechanisms, total))




_TARGET_CRITERIA = ("target_chembl_id", "gene_symbol", "target_name",
                    "organism", "target_type")


def target_search(args: dict) -> str:
    limit = _limit(args)
    params: dict = {}
    if _usable(args.get("target_chembl_id")):
        params["target_chembl_id"] = args["target_chembl_id"]
    if _usable(args.get("gene_symbol")):
        params["target_components__target_component_synonyms__"
               "component_synonym__iexact"] = args["gene_symbol"]
    if _usable(args.get("target_name")):
        params["pref_name__icontains"] = args["target_name"]
    if _usable(args.get("organism")):
        params["organism__icontains"] = args["organism"]
    if _usable(args.get("target_type")):
        params["target_type"] = args["target_type"]
    if not params and _any_blank(args, _TARGET_CRITERIA):


        return marshal.compact_json(marshal.target_search_response([], 0))
    records, total = _targets().paginate("target", params, max_records=limit)
    return marshal.compact_json(marshal.target_search_response(records, total))


HANDLERS = {
    "compound_search": compound_search,
    "drug_search": drug_search,
    "get_admet": get_admet,
    "get_bioactivity": get_bioactivity,
    "get_mechanism": get_mechanism,
    "target_search": target_search,
}


def build_server() -> Tier1Server:
    return Tier1Server("chembl-mcp-server", load_schemas(__package__), HANDLERS)


def main() -> None:



    t1 = build_server()
    apply_gate_tier1(t1)
    t1.run()


if __name__ == "__main__":
    main()
