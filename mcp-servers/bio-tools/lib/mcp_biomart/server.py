"""mcp-biomart server — tool handlers + stdio entry point.

Tool names/schemas are served verbatim from ``schemas.json`` (captured from
the original hosted Biomart connector, whose bundled source is operon
``core/mcp-sources/biomart/biomart_mcp.py`` — FastMCP + pybiomart).

Retrieval reuses the fleet's ``biomart-query`` transport: ``BiomartClient``
(paced, retrying, completion-stamp-verifying ``_post``) for queries and the
introspection module's paced ``_get_metadata`` GET for the registry /
datasets / configuration endpoints. No new retry logic lives here;
``marshal`` reproduces the original's pybiomart/pandas CSV semantics.

Error-string behavior mirrors the original per tool: list_marts,
list_datasets, get_data and get_translation return ``"Error: <msg>"``
strings; the three config-introspection listings raise (the original had no
try/except there either, surfacing as an MCP tool error).
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers_common import Tier1Server, load_schemas, original_json
from mcp_servers_common.criteria import blank
from mcp_servers_common.gate import apply_gate_tier1

from . import marshal


@lru_cache(maxsize=1)
def _client():
    from biomart_query.client import BiomartClient
    return BiomartClient()


def _metadata(params: dict) -> str:
    from biomart_query.introspect import _get_metadata
    return _get_metadata(_client(), params)


@lru_cache(maxsize=64)
def _configuration(dataset: str) -> tuple[dict, dict]:
    """(attributes, filters) from the dataset configuration XML, cached —
    the original (pybiomart) cached the same document per dataset."""
    return marshal.parse_configuration(
        _metadata({"type": "configuration", "dataset": dataset}))


@lru_cache(maxsize=16)
def _translation_dict(dataset: str, from_attr: str, to_attr: str) -> dict:
    """Original semantics: one full two-column scan of the dataset, cached
    per (dataset, from_attr, to_attr) pair."""
    client = _client()
    xml = marshal.build_query_xml(dataset, [from_attr, to_attr], {}, {},
                                  header=False)
    body = client._post(xml)
    return marshal.translation_dict(client._parse_tsv(body, 2))



def list_marts(args: dict) -> str:
    try:
        return marshal.csv_text(
            marshal.marts_df(_metadata({"type": "registry"})))
    except Exception as exc:
        return f"Error: {exc}"


def list_datasets(args: dict) -> str:
    try:
        return marshal.csv_text(marshal.datasets_df(
            _metadata({"type": "datasets", "mart": args["mart"]})))
    except Exception as exc:
        return f"Error: {exc}"


def list_common_attributes(args: dict) -> str:
    attributes, _ = _configuration(args["dataset"])
    return marshal.common_attributes_csv(attributes)


def list_all_attributes(args: dict) -> str:
    attributes, _ = _configuration(args["dataset"])
    return marshal.all_attributes_csv(attributes)


def list_filters(args: dict) -> str:
    _, filters = _configuration(args["dataset"])
    return marshal.filters_csv(filters)


def get_data(args: dict) -> str:
    try:
        dataset = args["dataset"]
        filters = args.get("filters") or {}





        bad = sorted(
            k for k, v in filters.items()
            if blank(v) or (isinstance(v, (list, tuple))
                            and (not v or any(not str(x).strip()
                                              for x in v))))
        if bad:
            raise ValueError(
                f"filter(s) {', '.join(bad)} have empty values — an empty "
                "filter matches nothing; omit the filter instead")
        _, filter_types = _configuration(dataset)
        xml = marshal.build_query_xml(
            dataset, args["attributes"], filters,
            filter_types, header=True)
        return marshal.data_csv(_client()._post(xml))
    except Exception as exc:
        return f"Error: {exc}"


def get_translation(args: dict) -> str:
    try:
        mapping = _translation_dict(args["dataset"], args["from_attr"],
                                    args["to_attr"])
    except Exception as exc:
        return f"Error: {exc}"
    target = args["target"]
    if target not in mapping:
        return f"Error: Target '{target}' not found"
    return mapping[target]


def batch_translate(args: dict) -> str:


    mapping = _translation_dict(args["dataset"], args["from_attr"],
                                args["to_attr"])
    return original_json(
        marshal.batch_translate_response(mapping, args["targets"]))


HANDLERS = {
    "list_marts": list_marts,
    "list_datasets": list_datasets,
    "list_common_attributes": list_common_attributes,
    "list_all_attributes": list_all_attributes,
    "list_filters": list_filters,
    "get_data": get_data,
    "get_translation": get_translation,
    "batch_translate": batch_translate,
}


def build_server() -> Tier1Server:
    return Tier1Server("biomart", load_schemas(__package__), HANDLERS)


def main() -> None:



    t1 = build_server()
    apply_gate_tier1(t1)
    t1.run()


if __name__ == "__main__":
    main()
