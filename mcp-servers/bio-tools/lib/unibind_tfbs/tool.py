"""High-level UniBind retrieval: dataset search/detail + hubApi region query.

Probed facts this module depends on (verified live 2026-06-09):

* ``/api/v1/datasets/`` is DRF page-number paginated; the server silently
  caps ``page_size`` at 500. Filters that work as query params: ``tf_name``,
  ``cell_line``, ``species``, ``collection`` (Robust/Permissive),
  ``jaspar_id``, free-text ``search``.
* Dataset ids ("tf_id") look like
  ``ENCSR000AUE.A549_lung_carcinoma.CTCF`` — <identifier>.<cell_line>.<tf>.
* UniBind's REST API has NO genomic-region endpoint. Region queries go
  through the UCSC hubApi against the registered "UniBind 2021" public
  track hubs (track ``UniBind``, bigBed 9). Row names encode
  ``<dataset>_<cell-line-with-dashes>_<TF>_<JASPAR matrix>``.
* hubApi honors ``maxItemsOutput`` and sets ``maxItemsLimit: true`` in the
  response when the cap was hit (the scan is then an incomplete prefix).
"""
from __future__ import annotations

import time
from mcp_servers_common.pathseg import seg

from .client import PacedJsonClient, UniBindApiError

UNIBIND_BASE = "https://unibind.uio.no/api/v1"
UCSC_BASE = "https://api.genome.ucsc.edu"
PAGE_SIZE = 500
WALK_DEADLINE_S = 40.0
REGION_FETCH_CAP = 20_000

_HUB_URL = ("https://unibind.uio.no/static/data/latest/"
            "UniBind_hubs_{collection}/UCSC/hub.txt")



HUB_GENOMES = {
    "Robust": ("hg38", "mm10", "ce11", "dm6", "danRer11", "sacCer3",
               "rn6", "araTha1"),
    "Permissive": ("hg38", "mm10", "ce11", "dm6", "danRer11", "sacCer3",
                   "rn6", "araTha1", "spo2"),
}
MAX_REGION_SPAN = 1_000_000


def make_unibind_client() -> PacedJsonClient:
    return PacedJsonClient(UNIBIND_BASE)


def make_ucsc_client() -> PacedJsonClient:
    return PacedJsonClient(UCSC_BASE)


def _parse_tf_id(tf_id: str) -> dict:
    """Split '<identifier>.<cell_line>.<tf>' (cell_line may contain dots)."""
    parts = tf_id.split(".")
    if len(parts) < 3:
        return {"identifier": None, "cell_line": None}
    return {"identifier": parts[0], "cell_line": ".".join(parts[1:-1])}


def search_datasets(client: PacedJsonClient, tf_name: str | None = None,
                    cell_line: str | None = None, species: str | None = None,
                    collection: str | None = None,
                    jaspar_id: str | None = None, search: str | None = None,
                    max_rows: int = 200) -> dict:
    """Bounded-prefix DRF walk of /datasets/ with an exact upstream total.

    Returns {"total", "returned", "truncated", "datasets"}; ``total`` is the
    API's own count, rows are a stable prefix (upstream default ordering).
    When the walk completes under the cap, row count is verified against
    ``total`` (mismatch raises).
    """
    params = {k: v for k, v in {
        "tf_name": tf_name, "cell_line": cell_line, "species": species,
        "collection": collection, "jaspar_id": jaspar_id, "search": search,
    }.items() if v is not None}
    params["page_size"] = PAGE_SIZE
    max_rows = max(0, max_rows)

    deadline = time.monotonic() + WALK_DEADLINE_S
    payload = client.get_json("/datasets/", params=params)
    total = payload["count"]
    raw: list[dict] = list(payload["results"])
    next_url = payload.get("next")
    while next_url and len(raw) < max_rows:
        if time.monotonic() > deadline:
            raise UniBindApiError(
                "dataset walk exceeded the per-call time budget; narrow the "
                "filters or lower max_rows")
        payload = client.get_json(next_url)
        raw.extend(payload["results"])
        next_url = payload.get("next")
    if next_url is None and len(raw) != total:
        raise UniBindApiError(
            f"pagination walk returned {len(raw)} rows but API count={total}")

    rows = []
    for r in raw[:max_rows]:
        tf_id = r.get("url", "").rstrip("/").rsplit("/", 1)[-1]
        row = {"tf_id": tf_id, "tf_name": r.get("tf_name"),
               "total_peaks": r.get("total_peaks")}
        row.update(_parse_tf_id(tf_id))
        rows.append(row)
    return {"total": total, "returned": len(rows),
            "truncated": len(rows) < total, "datasets": rows}


def get_dataset(client: PacedJsonClient, tf_id: str) -> dict:
    """One dataset's detail, flattened: per-model TFBS counts + file URLs."""
    d = client.get_json(f"/datasets/{seg(tf_id)}/")
    models = []
    for model_group in d.get("tfbs", []):
        for model_name, entries in model_group.items():
            for e in entries:
                models.append({
                    "prediction_model": model_name,
                    "jaspar_id": e.get("jaspar_id"),
                    "jaspar_version": e.get("jaspar_version"),
                    "total_tfbs": e.get("total_tfbs"),
                    "score_threshold": e.get("score_threshold"),
                    "distance_threshold": e.get("distance_threshold"),
                    "adj_centrimo_pvalue": e.get("adj_centrimo_pvalue"),
                    "bed_url": e.get("bed_url"),
                    "fasta_url": e.get("fasta_url"),
                })
    return {
        "tf_id": d.get("tf_id"),
        "tf_name": d.get("tf_name"),
        "identifiers": d.get("identifier"),
        "cell_lines": d.get("cell_line"),
        "biological_conditions": d.get("biological_condition"),
        "jaspar_ids": d.get("jaspar_id"),
        "prediction_models": d.get("prediction_models"),
        "total_peaks": d.get("total_peaks"),
        "n_models": len(models),
        "models": models,
    }


def _parse_site_name(name: str) -> dict:
    """Split '<dataset>_<cell-line>_<TF>_<MAxxxx.v>' bigBed item names."""
    parts = name.split("_")
    if len(parts) < 4:
        return {"dataset": None, "cell_line": None, "tf_name": None,
                "jaspar_matrix": None}
    return {"dataset": parts[0], "cell_line": "_".join(parts[1:-2]),
            "tf_name": parts[-2], "jaspar_matrix": parts[-1]}


def tfbs_in_region(ucsc_client: PacedJsonClient, genome: str, chrom: str,
                   start: int, end: int, tf_name: str | None = None,
                   collection: str = "Robust",
                   max_sites: int = 2000) -> dict:
    """TF binding sites overlapping a genomic interval, via the UCSC hubApi.

    Scans up to REGION_FETCH_CAP items from the hub track; the response's
    ``maxItemsLimit`` flag is surfaced as ``region_scan_complete`` so a
    capped scan is never silently presented as complete.
    """
    if collection not in HUB_GENOMES:
        raise ValueError(f"collection must be one of {sorted(HUB_GENOMES)}, "
                         f"got {collection!r}")
    if genome not in HUB_GENOMES[collection]:
        raise ValueError(
            f"genome {genome!r} is not in the UniBind {collection} hub; "
            f"valid genomes: {', '.join(HUB_GENOMES[collection])}")
    if end <= start:
        raise ValueError("end must be > start")
    if end - start > MAX_REGION_SPAN:
        raise ValueError(
            f"region span {end - start} exceeds the {MAX_REGION_SPAN} bp "
            "limit; query smaller windows")
    max_sites = max(0, max_sites)

    payload = ucsc_client.get_json("/getData/track", params={
        "hubUrl": _HUB_URL.format(collection=collection),
        "genome": genome, "track": "UniBind",
        "chrom": chrom, "start": start, "end": end,
        "maxItemsOutput": REGION_FETCH_CAP,
    })
    if "UniBind" not in payload:
        raise UniBindApiError(
            f"hubApi response missing track data: {str(payload)[:300]}")
    items = payload["UniBind"]

    if isinstance(items, dict):
        items = [row for rows in items.values() for row in rows]
    scan_complete = not payload.get("maxItemsLimit", False)

    sites = []
    want_tf = tf_name.casefold() if tf_name else None
    for it in items:
        meta = _parse_site_name(it.get("name", ""))
        if want_tf and (meta["tf_name"] or "").casefold() != want_tf:
            continue
        sites.append({
            "chrom": it["chrom"], "start": it["chromStart"],
            "end": it["chromEnd"], "strand": it.get("strand"),
            **meta,
        })
    n_matching = len(sites)
    returned = sites[:max_sites]
    return {
        "genome": genome, "chrom": chrom, "start": start, "end": end,
        "collection": collection, "tf_name_filter": tf_name,
        "items_scanned": len(items),
        "region_scan_complete": scan_complete,
        "n_matching": n_matching,
        "returned": len(returned),
        "truncated": len(returned) < n_matching or not scan_complete,
        "sites": returned,
    }
