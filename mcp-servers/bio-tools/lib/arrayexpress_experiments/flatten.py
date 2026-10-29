"""Per-accession detail retrieval and section-tree flattening.

``GET /api/v1/studies/{accession}`` returns the full BioStudies submission JSON: a small list
of top-level attributes plus a deeply nested ``section`` tree (Study -> Protocols / Author /
Organization / Publication / Samples / Assays and Data / score sections, each with attribute
lists, files and links). ``flatten_study`` reduces that tree to a flat experiment record with
the functional-genomics fields an analyst actually needs.

The flattened record is deterministic: list-valued fields are either in document order
(authors, protocols — order is meaningful) or sorted (organisms, file-type summaries,
array designs — order is not meaningful in the source).
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Iterator

from .client import BioStudiesClient


EXPERIMENT_RECORD_FIELDS = [
    "accession",
    "title",
    "release_date",
    "study_type",
    "organisms",
    "description",
    "assay_count",
    "sample_count",
    "technology",
    "assay_by_molecule",
    "experimental_designs",
    "experimental_factors",
    "authors",
    "submitter_organizations",
    "publications",
    "protocol_count",
    "protocol_types",
    "array_designs",
    "file_count",
    "files_by_type",
    "total_file_bytes",
    "links",
]






def _attr_map(node: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """name -> list of attribute dicts (attributes can repeat, e.g. Organism)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for attr in node.get("attributes", []) or []:
        if isinstance(attr, dict) and attr.get("name"):
            out.setdefault(attr["name"], []).append(attr)
    return out


def _attr_values(node: dict[str, Any], name: str) -> list[str]:
    return [a.get("value", "") for a in _attr_map(node).get(name, []) if a.get("value") is not None]


def _attr_value(node: dict[str, Any], name: str, default: str | None = None) -> str | None:
    vals = _attr_values(node, name)
    return vals[0] if vals else default


def iter_sections(node: Any) -> Iterator[dict[str, Any]]:
    """Depth-first iterator over every section dict in a study JSON section tree.

    Subsection entries can be either dicts or lists of dicts (tables); both are handled.
    """
    if isinstance(node, list):
        for item in node:
            yield from iter_sections(item)
    elif isinstance(node, dict):
        yield node
        for sub in node.get("subsections", []) or []:
            yield from iter_sections(sub)


def _sections_of_type(root: dict[str, Any], section_type: str) -> list[dict[str, Any]]:
    return [s for s in iter_sections(root) if s.get("type") == section_type]


def _iter_files(node: Any) -> Iterator[dict[str, Any]]:
    """Iterate over every file dict in a section tree (file entries can be nested lists)."""
    for sec in iter_sections(node):
        for entry in sec.get("files", []) or []:
            items = entry if isinstance(entry, list) else [entry]
            for f in items:
                if isinstance(f, dict):
                    yield f


def _iter_links(node: Any) -> Iterator[dict[str, Any]]:
    for sec in iter_sections(node):
        for entry in sec.get("links", []) or []:
            items = entry if isinstance(entry, list) else [entry]
            for l in items:
                if isinstance(l, dict):
                    yield l






def flatten_study(study_json: dict[str, Any]) -> dict[str, Any]:
    """Flatten a BioStudies study JSON (ArrayExpress experiment) into a single flat record."""
    top_attrs = {a.get("name"): a.get("value") for a in study_json.get("attributes", []) or []}
    section = study_json.get("section", {}) or {}

    accession = study_json.get("accno")
    title = _attr_value(section, "Title") or top_attrs.get("Title")
    release_date = top_attrs.get("ReleaseDate")
    study_type = _attr_value(section, "Study type")
    organisms = sorted(set(_attr_values(section, "Organism")))
    description = _attr_value(section, "Description")


    sample_count = None
    experimental_designs: list[str] = []
    experimental_factors: list[str] = []
    for samples in _sections_of_type(section, "Samples"):
        sc = _attr_value(samples, "Sample count")
        if sc is not None and sample_count is None:
            try:
                sample_count = int(sc)
            except ValueError:
                pass
        experimental_designs.extend(_attr_values(samples, "Experimental Designs"))
        experimental_factors.extend(_attr_values(samples, "Experimental Factors"))
    experimental_designs = sorted(set(experimental_designs))
    experimental_factors = sorted(set(experimental_factors))


    assay_count = None
    technology = None
    assay_by_molecule = None
    for aad in _sections_of_type(section, "Assays and Data"):
        ac = _attr_value(aad, "Assay count")
        if ac is not None and assay_count is None:
            try:
                assay_count = int(ac)
            except ValueError:
                pass
        technology = technology or _attr_value(aad, "Technology")
        assay_by_molecule = assay_by_molecule or _attr_value(aad, "Assay by Molecule")


    org_names: dict[str, str] = {}
    for org in _sections_of_type(section, "Organization") + _sections_of_type(section, "Organisation"):
        accno = org.get("accno") or ""
        name = _attr_value(org, "Name")
        if name:
            org_names[accno] = name
    authors: list[dict[str, Any]] = []
    for author in _sections_of_type(section, "Author"):
        amap = _attr_map(author)
        affil_refs = [a.get("value") for a in amap.get("affiliation", []) if a.get("value")]
        authors.append({
            "name": _attr_value(author, "Name"),
            "email": _attr_value(author, "Email"),
            "role": _attr_value(author, "Role"),
            "affiliations": [org_names.get(ref, ref) for ref in affil_refs],
        })
    submitter_organizations = sorted(set(org_names.values()))


    publications: list[dict[str, Any]] = []
    for pub in _sections_of_type(section, "Publication"):
        publications.append({
            "accno": pub.get("accno") or None,
            "title": _attr_value(pub, "Title"),
            "authors": _attr_value(pub, "Authors"),
            "doi": _attr_value(pub, "DOI"),
            "status": _attr_value(pub, "Status"),
        })
    publications.sort(key=lambda p: json.dumps(p, sort_keys=True))


    protocol_sections = _sections_of_type(section, "Protocols")
    protocol_types = sorted({t for p in protocol_sections for t in _attr_values(p, "Type") if t})


    array_designs = sorted({
        l.get("url") for l in _iter_links(section)
        if l.get("url") and any(
            a.get("name") == "Type" and a.get("value") == "Array Design"
            for a in l.get("attributes", []) or []
        )
    })


    files = list(_iter_files(section))
    files_by_type: dict[str, int] = {}
    total_bytes = 0
    for f in files:
        ftype = None
        for a in f.get("attributes", []) or []:
            if a.get("name") in ("Type", "Description") and a.get("value"):
                ftype = a.get("value")
                break
        ftype = ftype or "unspecified"
        files_by_type[ftype] = files_by_type.get(ftype, 0) + 1
        size = f.get("size")
        if isinstance(size, (int, float)):
            total_bytes += int(size)
    files_by_type = dict(sorted(files_by_type.items()))


    links = sorted({
        (l.get("url") or "",
         next((a.get("value") for a in (l.get("attributes") or []) if a.get("name") == "Type"), ""))
        for l in _iter_links(section)
    })
    links_out = [{"target": u, "type": t} for (u, t) in links if u]

    return {
        "accession": accession,
        "title": title,
        "release_date": release_date,
        "study_type": study_type,
        "organisms": organisms,
        "description": description,
        "assay_count": assay_count,
        "sample_count": sample_count,
        "technology": technology,
        "assay_by_molecule": assay_by_molecule,
        "experimental_designs": experimental_designs,
        "experimental_factors": experimental_factors,
        "authors": authors,
        "submitter_organizations": submitter_organizations,
        "publications": publications,
        "protocol_count": len(protocol_sections),
        "protocol_types": protocol_types,
        "array_designs": array_designs,
        "file_count": len(files),
        "files_by_type": files_by_type,
        "total_file_bytes": total_bytes,
        "links": links_out,
    }


def fetch_experiment(
    accession: str,
    client: BioStudiesClient | None = None,
    return_raw: bool = False,
) -> dict[str, Any]:
    """Fetch ``/studies/{accession}`` and return the flattened experiment record.

    With ``return_raw=True`` returns ``(record, raw_study_json)``.
    """
    client = client or BioStudiesClient()
    raw = client.get_json(f"studies/{urllib.parse.quote(str(accession), safe='')}")
    record = flatten_study(raw)
    if return_raw:
        return record, raw
    return record






def canonicalize(record: Any) -> bytes:
    """Canonical JSON bytes for gate comparison.

    Rules (documented in README): UTF-8 JSON with sorted keys, compact separators,
    NFC-normalized strings, no volatile fields added or removed (the flattened record
    contains no timestamps or request identifiers).
    """
    import unicodedata

    def norm(x: Any) -> Any:
        if isinstance(x, str):
            return unicodedata.normalize("NFC", x)
        if isinstance(x, dict):
            return {norm(k): norm(v) for k, v in x.items()}
        if isinstance(x, list):
            return [norm(v) for v in x]
        return x

    return json.dumps(norm(record), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
