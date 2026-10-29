"""Per-section extractors over EMDB entry documents and the /analysis route.

Closes the five PARTIAL coverage items vs tooluniverse/emdb:
  EMDB_get_publications  -> extract_publications(entry)
  EMDB_get_map_info      -> extract_map_info(entry)
  EMDB_get_sample_info   -> extract_sample_info(entry)
  EMDB_get_imaging_info  -> extract_imaging_info(entry)
  EMDB_get_validation    -> extract_validation_record(analysis_payload)
                            (data from GET /analysis/{id}, see EMDBClient.get_analysis)

All entry-document extractors operate on the SAME ``GET /entry/{id}`` JSON that
``extract_entry_record`` consumes — fetch once, extract any section, zero extra
requests. Validation comes from the separate ``/analysis`` route (EMDB
Validation Analysis service); image/JPEG filename blocks in that payload are
presentation assets and are deliberately not emitted — only numeric validation
metrics are.
"""
from __future__ import annotations

from typing import Any

from .records import _as_float, _date_only, _listify, _value_of


def _unit_value(node: Any) -> dict:
    """{'valueOf_': x, 'units': u} -> {'value': float|str|None, 'units': u|None}."""
    if isinstance(node, dict):
        return {"value": _as_float(node.get("valueOf_")) if node.get("valueOf_") not in (None, "")
                else None,
                "units": node.get("units")}
    return {"value": _as_float(node), "units": None}



def extract_publications(entry: dict) -> dict:
    """Full publication block: primary citation with complete author list and
    external references, plus any auxiliary citations (``citation_list`` keys
    other than ``primary_citation`` — rare but present on some entries)."""
    xref = entry.get("crossreferences") or {}
    cit_list = xref.get("citation_list") or {}

    def _one(citation_node: Any) -> dict | None:
        if not isinstance(citation_node, dict) or not citation_node:
            return None

        inner = citation_node
        if "external_references" not in inner and "author" not in inner:
            first_val = next(iter(citation_node.values()), None)
            if isinstance(first_val, dict):
                inner = first_val
            else:
                return None
        authors = []
        for a in _listify(inner.get("author")):
            name = _value_of(a)
            if name:
                order = a.get("order") if isinstance(a, dict) else None
                authors.append({"name": str(name), "order": order})
        authors.sort(key=lambda x: (x["order"] is None, x["order"]))
        refs: dict[str, str | None] = {"doi": None, "pmid": None, "issn": None, "csd": None}
        for ref in _listify(inner.get("external_references")):
            if not isinstance(ref, dict):
                continue
            ref_type = (ref.get("type_") or "").upper()
            val = str(_value_of(ref) or "")
            if ref_type == "DOI":
                refs["doi"] = val[4:] if val.lower().startswith("doi:") else val
            elif ref_type == "PUBMED":
                refs["pmid"] = val
            elif ref_type == "ISSN":
                refs["issn"] = val
            elif ref_type == "CSD":
                refs["csd"] = val
        year = inner.get("year")
        return {
            "title": (inner.get("title") or "").strip() or None,
            "authors": authors,
            "journal": inner.get("journal_abbreviation") or inner.get("journal"),
            "journal_full": inner.get("journal"),
            "volume": inner.get("volume"),
            "first_page": inner.get("first_page"),
            "last_page": inner.get("last_page"),
            "year": int(year) if year not in (None, "") else None,
            "country": inner.get("country"),
            "published": bool(inner.get("published")) if inner.get("published") is not None else None,
            "external_references": refs,
        }

    primary = _one(cit_list.get("primary_citation"))
    secondary = []
    for key, node in cit_list.items():
        if key == "primary_citation":
            continue
        for item in _listify(node):
            rec = _one(item)
            if rec:
                secondary.append(rec)
    return {
        "emdb_id": entry.get("emdb_id"),
        "primary_citation": primary,
        "secondary_citations": secondary,
    }



def extract_map_info(entry: dict) -> dict:
    """Complete map metadata block: file/format/data type, dimensions, spacing,
    origin, axis order, cell, voxel statistics, contour levels, symmetry, label."""
    map_block = entry.get("map") or {}
    dims = map_block.get("dimensions") or {}
    origin = map_block.get("origin") or {}
    spacing = map_block.get("spacing") or {}
    axis = map_block.get("axis_order") or {}
    pixel = map_block.get("pixel_spacing") or {}
    cell = map_block.get("cell") or {}
    stats = map_block.get("statistics") or {}
    contours = []
    for c in _listify((map_block.get("contour_list") or {}).get("contour")):
        if isinstance(c, dict):
            contours.append({
                "level": _as_float(c.get("level")),
                "primary": bool(c.get("primary")) if c.get("primary") is not None else None,
                "source": c.get("source"),
            })
    sym = map_block.get("symmetry") or {}
    return {
        "emdb_id": entry.get("emdb_id"),
        "file": map_block.get("file"),
        "format": map_block.get("format"),
        "size_kbytes": map_block.get("size_kbytes"),
        "data_type": map_block.get("data_type"),
        "dimensions": {"col": dims.get("col"), "row": dims.get("row"), "sec": dims.get("sec")},
        "origin": {"col": origin.get("col"), "row": origin.get("row"), "sec": origin.get("sec")},
        "spacing": {"x": spacing.get("x"), "y": spacing.get("y"), "z": spacing.get("z")},
        "axis_order": {"fast": axis.get("fast"), "medium": axis.get("medium"), "slow": axis.get("slow")},
        "pixel_spacing_angstrom": {ax: _unit_value(pixel.get(ax) or {}) for ax in ("x", "y", "z")},
        "cell": {k: _unit_value(cell.get(k) or {}) for k in ("a", "b", "c", "alpha", "beta", "gamma")},
        "statistics": {
            "minimum": _as_float(stats.get("minimum")),
            "maximum": _as_float(stats.get("maximum")),
            "average": _as_float(stats.get("average")),
            "std": _as_float(stats.get("std")),
        },
        "contour_levels": contours,
        "space_group": _value_of(sym.get("space_group")) if sym else None,
        "label": map_block.get("label"),
    }



def extract_sample_info(entry: dict) -> dict:
    """Detailed sample block: per-macromolecule records (type, weight, copies,
    EC number, source organism, sequence cross-refs) and per-supramolecule
    records — beyond the name lists that ``extract_entry_record`` carries."""
    sample = entry.get("sample") or {}

    def _weight(node: Any) -> dict | None:
        if not isinstance(node, dict):
            return None
        theo = node.get("theoretical")
        exp = node.get("experimental")
        out = {}
        if theo is not None:
            out["theoretical"] = _unit_value(theo)
        if exp is not None:
            out["experimental"] = _unit_value(exp)
        return out or None

    def _source(node: Any) -> dict | None:
        if not isinstance(node, dict):
            return None
        organism = node.get("organism")
        return {
            "organism": str(_value_of(organism)) if organism else None,
            "ncbi_taxid": (organism.get("ncbi") if isinstance(organism, dict) else None),
        }

    macromolecules = []
    for m in _listify((sample.get("macromolecule_list") or {}).get("macromolecule")):
        if not isinstance(m, dict):
            continue
        seq = m.get("sequence") or {}
        ext_refs = []
        for ref in _listify(seq.get("external_references")):
            if isinstance(ref, dict):
                ext_refs.append({"type": ref.get("type_"), "id": str(_value_of(ref) or "") or None})
        macromolecules.append({
            "id": m.get("macromolecule_id"),
            "type": m.get("instance_type"),
            "name": str(_value_of(m.get("name")) or "") or None,
            "molecular_weight": _weight(m.get("molecular_weight")),
            "number_of_copies": m.get("number_of_copies"),
            "ec_number": [str(_value_of(e)) for e in _listify(m.get("ec_number")) if _value_of(e)],
            "enantiomer": m.get("enantiomer"),
            "natural_source": _source(m.get("natural_source")),
            "recombinant_expression": bool(m.get("recombinant_expression")) if m.get("recombinant_expression") is not None else None,
            "sequence_external_references": ext_refs,
        })
    supramolecules = []
    for s in _listify((sample.get("supramolecule_list") or {}).get("supramolecule")):
        if not isinstance(s, dict):
            continue
        supramolecules.append({
            "id": s.get("supramolecule_id"),
            "type": s.get("instance_type"),
            "name": str(_value_of(s.get("name")) or "") or None,
            "parent": s.get("parent"),
            "molecular_weight": _weight(s.get("molecular_weight")),
            "natural_source": _source(s.get("natural_source")),
            "macromolecule_ids": [
                m for m in _listify((s.get("macromolecule_list") or {}).get("macromolecule_id"))
            ] or _listify((s.get("macromolecule_list") or {}).get("macromolecule")),
        })
    return {
        "emdb_id": entry.get("emdb_id"),
        "name": str(_value_of(sample.get("name")) or "") or None,
        "macromolecules": macromolecules,
        "supramolecules": supramolecules,
    }



def extract_imaging_info(entry: dict) -> dict:
    """Microscopy + specimen-preparation metadata: microscope, voltage, source,
    detector, dose, modes, defocus range, magnification, Cs, cryogen; grid,
    buffer and vitrification conditions. One record per microscopy session /
    preparation (entries can carry several, e.g. tilt series)."""
    sd_list = (entry.get("structure_determination_list") or {}).get("structure_determination") or []
    sd = sd_list[0] if sd_list else {}

    sessions = []
    for mic in _listify((sd.get("microscopy_list") or {}).get("microscopy")):
        if not isinstance(mic, dict):
            continue
        recordings = []
        for rec in _listify((mic.get("image_recording_list") or {}).get("image_recording")):
            if not isinstance(rec, dict):
                continue
            film = rec.get("film_or_detector_model")
            recordings.append({
                "id": rec.get("image_recording_id"),
                "detector": str(_value_of(film) or "") or None,
                "average_electron_dose_per_image": _unit_value(rec.get("average_electron_dose_per_image") or {}),
                "number_real_images": rec.get("number_real_images"),
                "average_exposure_time": _unit_value(rec.get("average_exposure_time") or {}),
            })
        sessions.append({
            "id": mic.get("microscopy_id"),
            "type": mic.get("instance_type"),
            "microscope": mic.get("microscope"),
            "acceleration_voltage": _unit_value(mic.get("acceleration_voltage") or {}),
            "electron_source": mic.get("electron_source"),
            "illumination_mode": mic.get("illumination_mode"),
            "imaging_mode": mic.get("imaging_mode"),
            "nominal_cs": _unit_value(mic.get("nominal_cs") or {}),
            "nominal_defocus_min": _unit_value(mic.get("nominal_defocus_min") or {}),
            "nominal_defocus_max": _unit_value(mic.get("nominal_defocus_max") or {}),
            "nominal_magnification": _as_float(_value_of(mic.get("nominal_magnification"))),
            "specimen_holder_model": mic.get("specimen_holder_model"),
            "cooling_holder_cryogen": mic.get("cooling_holder_cryogen"),
            "image_recordings": recordings,
        })

    preparations = []
    for prep in _listify((sd.get("specimen_preparation_list") or {}).get("specimen_preparation")):
        if not isinstance(prep, dict):
            continue
        buffer = prep.get("buffer") or {}
        grid = prep.get("grid") or {}
        vit = prep.get("vitrification") or {}
        preparations.append({
            "id": prep.get("preparation_id"),
            "type": prep.get("instance_type"),
            "buffer": {"ph": _as_float(buffer.get("ph")), "details": buffer.get("details")} if buffer else None,
            "grid": {
                "material": grid.get("material"),
                "mesh": grid.get("mesh"),
                "model": grid.get("model"),
                "pretreatment": (grid.get("pretreatment") or {}).get("type_"),
            } if grid else None,
            "vitrification": {
                "cryogen_name": vit.get("cryogen_name"),
                "instrument": vit.get("instrument"),
                "chamber_humidity": _unit_value(vit.get("chamber_humidity") or {}),
                "chamber_temperature": _unit_value(vit.get("chamber_temperature") or {}),
            } if vit else None,
        })

    return {
        "emdb_id": entry.get("emdb_id"),
        "method": sd.get("method"),
        "microscopy": sessions,
        "specimen_preparations": preparations,
    }




_VALIDATION_SCALAR_BLOCKS = (
    "recommended_contour_level",
    "predicated_contour_level",
    "rawmap_contour_level",
    "model_map_ratio",
    "model_volume",
    "mask_volume",
    "surfaces",
    "surface_ratio",
    "feature_assessment",
    "relion_mask_coverage",
)


def extract_validation_record(analysis_payload: dict, emdb_id: str) -> dict:
    """Numeric validation metrics from the /analysis/{id} payload.

    The payload is keyed by the bare accession number and mixes numeric
    metrics with JPEG asset filenames; only the numeric metrics are emitted.
    Sparse payloads (tomograms, model-free or historical entries) yield
    explicit nulls, never errors. ``available_blocks`` lists every key the
    route returned so callers can see what the validation service computed.
    """
    num = emdb_id.split("-", 1)[-1]
    inner = analysis_payload.get(num) or {}

    res_node = (inner.get("resolution") or {})
    resolution = _as_float(res_node.get("value")) if isinstance(res_node, dict) else None

    qscore = None
    q = inner.get("qscore")
    if isinstance(q, dict):
        qscore = _as_float(q.get("allmodels_average_qscore"))

    atom_inclusion = None
    ai = inner.get("atom_inclusion_by_level")
    if isinstance(ai, dict):
        atom_inclusion = _as_float(ai.get("average_ai_allmodels"))

    record: dict[str, Any] = {
        "emdb_id": f"EMD-{num}",
        "has_validation_analysis": bool(inner),
        "resolution_angstrom": resolution,
        "qscore_average": qscore,
        "atom_inclusion_average": atom_inclusion,
        "available_blocks": sorted(inner.keys()),
    }
    for block in _VALIDATION_SCALAR_BLOCKS:
        record[block] = inner.get(block) if isinstance(inner.get(block), dict) else None
    return record


def fetch_validation_records(client, emdb_ids: list[str]) -> list[dict]:
    """Fetch /analysis for a list of accessions, in deterministic input order.

    Accessions the validation service has no analysis for are reported as
    ``has_validation_analysis: false`` (the route 404s or returns an empty
    payload), never silently dropped.
    """
    from .client import EMDBNotFound, normalize_emdb_id

    records = []
    for emdb_id in emdb_ids:
        norm = normalize_emdb_id(emdb_id)
        try:
            payload = client.get_analysis(norm)
        except EMDBNotFound:
            records.append({"emdb_id": norm, "has_validation_analysis": False,
                            "error": "not_found"})
            continue
        records.append(extract_validation_record(payload, norm))
    return records


def fetch_section_records(client, emdb_ids: list[str], section: str) -> list[dict]:
    """Fetch entry documents and extract one section per accession.

    section in {'publications', 'map', 'sample', 'imaging'}. Unknown
    accessions are reported with ``"error": "not_found"``.
    """
    from .client import EMDBNotFound, normalize_emdb_id

    extractors = {
        "publications": extract_publications,
        "map": extract_map_info,
        "sample": extract_sample_info,
        "imaging": extract_imaging_info,
    }
    if section not in extractors:
        raise ValueError(f"unknown section {section!r}; expected one of {sorted(extractors)}")
    extract = extractors[section]
    records = []
    for emdb_id in emdb_ids:
        norm = normalize_emdb_id(emdb_id)
        try:
            entry = client.get_entry(norm)
        except EMDBNotFound:
            records.append({"emdb_id": norm, "error": "not_found"})
            continue
        records.append(extract(entry))
    return records
