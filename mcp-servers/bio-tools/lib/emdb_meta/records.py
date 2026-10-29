"""Extraction of structured map-metadata records from EMDB /entry/{id} JSON documents."""
from __future__ import annotations

import json
from typing import Any



def _value_of(node: Any) -> Any:
    """Many EMDB v3 fields are {'valueOf_': x, 'units': u, ...}; unwrap to x."""
    if isinstance(node, dict) and "valueOf_" in node:
        return node["valueOf_"]
    return node


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_only(value: Any) -> str | None:
    """'2020-08-20T00:00:00' -> '2020-08-20'."""
    if not value:
        return None
    return str(value)[:10]


def _listify(node: Any) -> list:
    if node is None:
        return []
    if isinstance(node, list):
        return node
    return [node]



def extract_entry_record(entry: dict) -> dict:
    """Flatten one EMDB entry JSON document into a structured map-metadata record.

    Explicit handling:
      * entries with no fitted PDB model: ``fitted_pdb_ids`` is ``[]`` (the raw
        document has ``crossreferences.pdb_list = null``, which breaks naive
        ``entry["crossreferences"]["pdb_list"]["pdb_reference"]`` extraction);
      * obsolete / superseded entries: ``status`` carries the raw code (e.g.
        ``OBS``), ``is_obsolete`` is True and ``superseded_by`` lists the
        replacement accession(s) from ``admin.obsolete_list``;
      * entries with no reported resolution (e.g. raw tomograms): ``resolution``
        is ``None`` and ``resolution_method`` is ``None``.
    """
    admin = entry.get("admin") or {}
    xref = entry.get("crossreferences") or {}
    sample = entry.get("sample") or {}
    map_block = entry.get("map") or {}


    status_node = admin.get("current_status") or {}
    status_code = _value_of(status_node.get("code")) if status_node else None
    obsolete_entries = []
    obsolete_list = admin.get("obsolete_list") or {}
    for item in _listify(obsolete_list.get("entry")):
        if isinstance(item, dict):
            repl = item.get("entry")
            if repl:
                obsolete_entries.append({"entry": str(repl), "date": _date_only(item.get("date"))})
    superseded_by = sorted({o["entry"] for o in obsolete_entries})


    key_dates = admin.get("key_dates") or {}


    sd_list = (entry.get("structure_determination_list") or {}).get("structure_determination") or []
    sd = sd_list[0] if sd_list else {}
    method = sd.get("method")
    aggregation_state = sd.get("aggregation_state")
    resolution = None
    resolution_units = None
    resolution_method = None
    image_processing = _listify(sd.get("image_processing"))
    if image_processing:
        final = (image_processing[0] or {}).get("final_reconstruction") or {}
        res_node = final.get("resolution")
        if res_node is not None:
            resolution = _as_float(_value_of(res_node))
            if isinstance(res_node, dict):
                resolution_units = res_node.get("units")
        resolution_method = final.get("resolution_method")


    sample_name = _value_of(sample.get("name")) if sample.get("name") else None
    macromolecules = []
    mml = (sample.get("macromolecule_list") or {}).get("macromolecule") or []
    for m in _listify(mml):
        name = _value_of((m or {}).get("name"))
        if name:
            macromolecules.append(str(name))
    supramolecules = []
    sml = (sample.get("supramolecule_list") or {}).get("supramolecule") or []
    for s in _listify(sml):
        name = _value_of((s or {}).get("name"))
        if name:
            supramolecules.append(str(name))


    fitted_pdb_ids: list[str] = []
    pdb_list = xref.get("pdb_list") or {}
    for ref in _listify(pdb_list.get("pdb_reference")):
        if isinstance(ref, dict) and ref.get("pdb_id"):
            fitted_pdb_ids.append(str(ref["pdb_id"]).lower())
    fitted_pdb_ids = sorted(set(fitted_pdb_ids))


    citation = {
        "title": None, "journal": None, "year": None, "published": None,
        "doi": None, "pmid": None, "first_author": None, "author_count": 0,
    }
    cit_list = xref.get("citation_list") or {}
    primary = cit_list.get("primary_citation") or {}

    inner = None
    if isinstance(primary, dict):
        if "external_references" in primary or "author" in primary:
            inner = primary
        elif len(primary) >= 1:
            first_val = next(iter(primary.values()))
            if isinstance(first_val, dict):
                inner = first_val
    if inner:
        citation["title"] = (inner.get("title") or "").strip() or None
        citation["journal"] = inner.get("journal_abbreviation") or inner.get("journal")
        year = inner.get("year")
        citation["year"] = int(year) if year not in (None, "") else None
        citation["published"] = bool(inner.get("published")) if inner.get("published") is not None else None
        authors = _listify(inner.get("author"))
        names = [str(_value_of(a)) for a in authors if _value_of(a)]
        citation["author_count"] = len(names)
        citation["first_author"] = names[0] if names else None
        for ref in _listify(inner.get("external_references")):
            if not isinstance(ref, dict):
                continue
            ref_type = (ref.get("type_") or "").upper()
            val = str(_value_of(ref) or "")
            if ref_type == "DOI" and citation["doi"] is None:
                citation["doi"] = val[4:] if val.lower().startswith("doi:") else val
            elif ref_type == "PUBMED" and citation["pmid"] is None:
                citation["pmid"] = val


    dims = map_block.get("dimensions") or {}
    spacing = map_block.get("pixel_spacing") or {}

    def _axis(axis: str) -> dict:
        node = spacing.get(axis) or {}
        return {"value": _as_float(_value_of(node)), "units": node.get("units") if isinstance(node, dict) else None}

    record = {
        "emdb_id": entry.get("emdb_id"),
        "title": admin.get("title"),
        "status": status_code,
        "is_obsolete": status_code == "OBS",
        "superseded_by": superseded_by,
        "obsolete_date": _date_only(key_dates.get("obsolete")),
        "method": method,
        "aggregation_state": aggregation_state,
        "resolution_angstrom": resolution,
        "resolution_method": resolution_method,
        "deposition_date": _date_only(key_dates.get("deposition")),
        "header_release_date": _date_only(key_dates.get("header_release")),
        "map_release_date": _date_only(key_dates.get("map_release")),
        "update_date": _date_only(key_dates.get("update")),
        "sample_name": sample_name,
        "macromolecule_names": macromolecules,
        "supramolecule_names": supramolecules,
        "fitted_pdb_ids": fitted_pdb_ids,
        "has_fitted_model": bool(fitted_pdb_ids),
        "citation": citation,
        "map": {
            "file": map_block.get("file"),
            "size_kbytes": map_block.get("size_kbytes"),
            "dimensions": {
                "col": dims.get("col"), "row": dims.get("row"), "sec": dims.get("sec"),
            },
            "voxel_size_angstrom": {
                "x": _axis("x"), "y": _axis("y"), "z": _axis("z"),
            },
        },
    }
    return record


def fetch_entry_records(client, emdb_ids: list[str]) -> list[dict]:
    """Fetch and flatten a list of EMD accessions, in deterministic input order.

    Unknown accessions are reported explicitly with ``"error": "not_found"``
    rather than being silently dropped.
    """
    from .client import EMDBNotFound, normalize_emdb_id

    records = []
    for emdb_id in emdb_ids:
        norm = normalize_emdb_id(emdb_id)
        try:
            entry = client.get_entry(norm)
        except EMDBNotFound:
            records.append({"emdb_id": norm, "error": "not_found"})
            continue
        records.append(extract_entry_record(entry))
    return records



def canonicalize(obj: Any) -> bytes:
    """Stable byte serialization used by the gate and the run-to-run identity check.

    Rules (documented in README): JSON with sorted keys, lists kept in the
    deterministic order produced by the tool (battery input order for entries,
    EMD-accession order for search hits), UTF-8, no volatile fields included
    (the tool never emits request timestamps or _id values).
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
