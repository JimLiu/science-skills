"""Mart / dataset / attribute / filter introspection for the BioMart martservice.

Closes the five PARTIAL coverage items vs the biomart connector:
  list_marts             -> list_marts()
  list_datasets          -> list_datasets(mart)
  list_common_attributes -> list_attributes(dataset, page="feature_page")
  list_all_attributes    -> list_attributes(dataset)
  list_filters           -> list_filters(dataset)

Routes (same martservice endpoint the query path uses, but GET with type=):
  ?type=registry                 -> MartRegistry XML (one MartURLLocation per mart)
  ?type=datasets&mart={mart}     -> TSV (one TableSet line per dataset)
  ?type=attributes&dataset={ds}  -> TSV: name, display, description, page,
                                    formats, table, column
  ?type=filters&dataset={ds}     -> TSV: name, display, options, description,
                                    page, type, operator, table, column
  ?type=configuration&dataset={ds} -> dataset configuration XML (used by the
                                    accuracy gate as an independent second
                                    formulation of the same metadata; not
                                    used by the listing functions themselves)

All listings are deterministic: rows sorted by internal name (attributes carry
a per-page sort so the same attribute appearing on several pages keeps every
page row).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from .client import BiomartClient, BiomartError


def _get_metadata(client: BiomartClient, params: dict[str, str]) -> str:
    """GET the martservice with metadata params, with the client's politeness.

    Metadata responses carry no [success] completion stamp, so this goes
    through the session directly (with throttle + retry) rather than _post.
    """
    last_error: Exception | None = None
    for attempt in range(client.max_retries + 1):
        if attempt:
            import time
            time.sleep(client.backoff_base * (2 ** (attempt - 1)))
        client._throttle()
        client.request_count += 1
        client._last_request_at = __import__("time").monotonic()
        try:
            resp = client._session.get(
                client.martservice_url, params=params, timeout=client.timeout,
                headers={"Accept-Encoding": "gzip"},
            )
        except Exception as exc:
            last_error = exc
            continue
        client.bytes_downloaded += len(resp.content)
        if resp.status_code >= 500 or resp.status_code in (405, 429):
            last_error = BiomartError(f"HTTP {resp.status_code} from martservice")
            continue
        if resp.status_code != 200:
            raise BiomartError(f"HTTP {resp.status_code} from martservice: {resp.text[:300]}")
        text = resp.text
        if text.lstrip().lower().startswith(("<html", "<!doctype html")):
            last_error = BiomartError("martservice returned an HTML page (outage)")
            continue
        if "Problem retrieving" in text[:500] or text.lstrip().startswith("Query ERROR"):
            raise BiomartError(text[:500].strip())
        return text
    raise BiomartError(
        f"martservice metadata request failed after {client.max_retries + 1} "
        f"attempts: {last_error!r}")



def list_marts(client: BiomartClient | None = None) -> list[dict[str, Any]]:
    """Marts from the registry XML, sorted by name.

    Each record: name, display_name, database, virtual_schema, visible, default.
    """
    client = client or BiomartClient()
    xml_text = _get_metadata(client, {"type": "registry"})
    root = ET.fromstring(xml_text)
    marts = []
    for loc in root.iter("MartURLLocation"):
        marts.append({
            "name": loc.get("name"),
            "display_name": loc.get("displayName"),
            "database": loc.get("database"),
            "virtual_schema": loc.get("serverVirtualSchema"),
            "visible": loc.get("visible") == "1",
            "default": loc.get("default") == "1",
        })
    marts.sort(key=lambda m: m["name"] or "")
    return marts



def list_datasets(mart: str = "ENSEMBL_MART_ENSEMBL",
                  client: BiomartClient | None = None) -> list[dict[str, Any]]:
    """Datasets of one mart (TSV route), sorted by dataset name.

    Each record: name, display_name, assembly, visible, interface, last_updated.
    """
    client = client or BiomartClient()
    text = _get_metadata(client, {"type": "datasets", "mart": mart})
    datasets = []
    for line in text.replace("\r\n", "\n").split("\n"):
        fields = line.split("\t")
        if len(fields) < 9 or fields[0].strip() != "TableSet":
            continue
        datasets.append({
            "name": fields[1],
            "display_name": fields[2],
            "visible": fields[3] == "1",
            "assembly": fields[4],
            "interface": fields[7],
            "last_updated": fields[8],
        })
    if not datasets:
        raise BiomartError(f"no TableSet lines in datasets response for mart {mart!r}")
    datasets.sort(key=lambda d: d["name"])
    return datasets



def list_attributes(dataset: str, page: str | None = None,
                    client: BiomartClient | None = None) -> list[dict[str, Any]]:
    """Attributes of one dataset (TSV route), sorted by (name, page).

    ``page=None`` lists every attribute page (the connector's
    list_all_attributes); ``page="feature_page"`` restricts to the default
    page (list_common_attributes — what the BioMart web UI shows first).
    """
    client = client or BiomartClient()
    text = _get_metadata(client, {"type": "attributes", "dataset": dataset})
    attrs = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 4:
            raise BiomartError(f"malformed attributes line: {line[:200]!r}")
        rec = {
            "name": fields[0],
            "display_name": fields[1],
            "description": fields[2] or None,
            "page": fields[3],
            "formats": fields[4] if len(fields) > 4 else None,
        }
        if page is None or rec["page"] == page:
            attrs.append(rec)
    if not attrs:
        raise BiomartError(
            f"no attributes parsed for dataset {dataset!r}"
            + (f" page {page!r}" if page else ""))
    attrs.sort(key=lambda a: (a["name"], a["page"]))
    return attrs



def list_filters(dataset: str,
                 client: BiomartClient | None = None) -> list[dict[str, Any]]:
    """Filters of one dataset (TSV route), sorted by name.

    Each record: name, display_name, type, operator, page, description,
    n_options (option lists like chromosome names are summarized by count —
    the full lists run to hundreds of patch/scaffold names).
    """
    client = client or BiomartClient()
    text = _get_metadata(client, {"type": "filters", "dataset": dataset})
    filters = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            raise BiomartError(f"malformed filters line: {line[:200]!r}")
        options = fields[2].strip()
        if options.startswith("[") and options.endswith("]"):
            inner = options[1:-1].strip()
            n_options = len(inner.split(",")) if inner else 0
        else:
            n_options = 0
        filters.append({
            "name": fields[0],
            "display_name": fields[1],
            "n_options": n_options,
            "description": fields[3] or None,
            "page": fields[4],
            "type": fields[5],
            "operator": fields[6],
        })
    if not filters:
        raise BiomartError(f"no filters parsed for dataset {dataset!r}")
    filters.sort(key=lambda f: f["name"])
    return filters



def configuration_names(dataset: str,
                        client: BiomartClient | None = None) -> dict[str, set]:
    """Attribute and filter internal names from the dataset configuration XML.

    This is a SECOND, independent server-side formulation of the dataset
    metadata (the configuration document that drives the MartView UI). The
    accuracy gate compares these name sets against the TSV listings; the
    listing functions above never call this.
    """
    client = client or BiomartClient()
    xml_text = _get_metadata(client, {"type": "configuration", "dataset": dataset})
    root = ET.fromstring(xml_text)
    attr_names = {
        d.get("internalName") for d in root.iter("AttributeDescription")
        if d.get("internalName") and d.get("hidden") != "true"
    }
    filter_names = set()
    for d in root.iter("FilterDescription"):
        if d.get("hidden") == "true":
            continue
        opts = [o for o in d.iter("Option") if o.get("type")]
        if d.get("type") == "container" and opts:


            filter_names.update(o.get("internalName") for o in opts
                                if o.get("internalName") and o.get("hidden") != "true")
        elif d.get("internalName"):
            filter_names.add(d.get("internalName"))
    return {"attributes": attr_names, "filters": filter_names}
