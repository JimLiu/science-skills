"""pybiomart-parity marshalling for the ORIGINAL biomart connector.

The original bundled server (operon ``core/mcp-sources/biomart/biomart_mcp.py``)
was FastMCP + pybiomart; its observable outputs are **CSV strings** produced
by pandas ``DataFrame.to_csv(index=False)`` over pybiomart's registry /
datasets / dataset-configuration parses, plus the raw query TSV round-tripped
through ``pandas.read_csv``. This module reproduces those parses and CSV
semantics exactly (pandas included — its type inference and NA handling are
part of the original's observable format) without pybiomart itself.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from io import StringIO
from typing import Mapping, Sequence

import pandas as pd


COMMON_ATTRIBUTES = [
    "ensembl_gene_id",
    "external_gene_name",
    "hgnc_symbol",
    "hgnc_id",
    "gene_biotype",
    "ensembl_transcript_id",
    "ensembl_peptide_id",
    "ensembl_exon_id",
    "description",
    "chromosome_name",
    "start_position",
    "end_position",
    "strand",
    "band",
    "transcript_start",
    "transcript_end",
    "transcription_start_site",
    "transcript_length",
]


ALL_ATTRIBUTES_EXCLUDES = ("_homolog_", "dbass", "affy_", "agilent_")


DATASET_COLNAMES = ["type", "name", "display_name", "unknown", "unknown2",
                    "unknown3", "unknown4", "virtual_schema", "unknown5"]


def csv_text(df: pd.DataFrame) -> str:
    """``DataFrame.to_csv(index=False).replace("\\r", "")`` — exactly what the
    original returned for every CSV tool."""
    return df.to_csv(index=False).replace("\r", "")



def marts_df(registry_xml: str) -> pd.DataFrame:
    """pybiomart ``Server.list_marts()``: MartURLLocation nodes in document
    order, columns name/display_name."""
    root = ET.fromstring(registry_xml)
    rows = [(n.attrib["name"], n.attrib["displayName"])
            for n in root.findall("MartURLLocation")]
    return pd.DataFrame.from_records(rows, columns=["name", "display_name"])



def datasets_df(datasets_tsv: str) -> pd.DataFrame:
    """pybiomart ``Mart.list_datasets()``: the raw TSV read with the fixed
    9-column schema, projected to name/display_name, server row order kept."""
    df = pd.read_csv(StringIO(datasets_tsv), sep="\t", header=None,
                     names=DATASET_COLNAMES)
    return df[["name", "display_name"]]



def parse_configuration(config_xml: str) -> tuple[dict, dict]:
    """pybiomart ``Dataset._fetch_configuration()``.

    Returns ``(attributes, filters)``:
    ``attributes``: internalName -> (displayName, description), iterated
    AttributePage -> AttributeDescription in document order (duplicate names
    keep the first position, last value — dict semantics, as pybiomart).
    ``filters``: internalName -> type ('' when absent). Container filters
    appear as themselves; their <Option> children are NOT expanded (so e.g.
    ``hgnc_symbol`` is not in this dict even though the martservice accepts
    it as a filter — see the get_data wart fix in the README).
    """
    root = ET.fromstring(config_xml)
    attributes: dict[str, tuple[str, str]] = {}
    for page in root.iter("AttributePage"):
        for desc in page.iter("AttributeDescription"):
            a = desc.attrib
            attributes[a["internalName"]] = (a.get("displayName", ""),
                                             a.get("description", ""))
    filters: dict[str, str] = {}
    for node in root.iter("FilterDescription"):
        a = node.attrib
        filters[a["internalName"]] = a.get("type", "")
    return attributes, filters


def attributes_df(attributes: Mapping[str, tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame.from_records(
        [(name, disp, desc) for name, (disp, desc) in attributes.items()],
        columns=["name", "display_name", "description"])


def common_attributes_csv(attributes: Mapping[str, tuple[str, str]]) -> str:
    df = attributes_df(attributes)
    return csv_text(df[df["name"].isin(COMMON_ATTRIBUTES)])


def all_attributes_csv(attributes: Mapping[str, tuple[str, str]]) -> str:
    df = attributes_df(attributes)
    for pattern in ALL_ATTRIBUTES_EXCLUDES:
        df = df[~df["name"].str.contains(pattern, na=False, regex=False)]
    return csv_text(df)


def filters_csv(filters: Mapping[str, str]) -> str:
    """Original columns name,type,description — description is always empty
    (pybiomart never populated it)."""
    df = pd.DataFrame.from_records(
        [(name, ftype, "") for name, ftype in filters.items()],
        columns=["name", "type", "description"])
    return csv_text(df)



def build_query_xml(dataset: str, attributes: Sequence[str],
                    filters: Mapping[str, object] | None,
                    filter_types: Mapping[str, str], *,
                    header: bool, unique_rows: bool = True,
                    virtual_schema: str = "default") -> str:
    """pybiomart ``Dataset.query()`` XML semantics:

    * boolean-type filters (config type == 'boolean') use the
      ``excluded="0|1"`` attribute; accepted values are True/'included'/'only'
      (include) and False/'excluded' (exclude);
    * list/tuple values are comma-joined;
    * everything else is ``str()``-ed into ``value=``;
    * ``uniqueRows=1`` (pybiomart's only_unique default).

    Unlike pybiomart, attribute/filter names are NOT validated against the
    configuration (wart fix — the martservice itself is the authority), and
    ``completionStamp="1"`` is requested so the fleet client can verify the
    response arrived complete.
    """
    query = ET.Element("Query", attrib={
        "virtualSchemaName": virtual_schema,
        "formatter": "TSV",
        "header": "1" if header else "0",
        "uniqueRows": "1" if unique_rows else "0",
        "datasetConfigVersion": "0.6",
        "completionStamp": "1",
    })
    ds = ET.SubElement(query, "Dataset",
                       attrib={"name": dataset, "interface": "default"})
    for name, value in (filters or {}).items():
        el = ET.SubElement(ds, "Filter", attrib={"name": name})
        if filter_types.get(name) == "boolean":
            if value is True or (isinstance(value, str)
                                 and value.lower() in ("included", "only")):
                el.set("excluded", "0")
            elif value is False or (isinstance(value, str)
                                    and value.lower() == "excluded"):
                el.set("excluded", "1")
            else:
                raise ValueError(
                    f"Invalid value for boolean filter ({value})")
        elif isinstance(value, (list, tuple)):
            el.set("value", ",".join(map(str, value)))
        else:
            el.set("value", str(value))
    for attr in attributes:
        ET.SubElement(ds, "Attribute", attrib={"name": attr})
    return ('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE Query>'
            + ET.tostring(query, encoding="unicode"))


def data_csv(tsv_body: str) -> str:
    """Round-trip the query TSV (display-name header row included) through
    pandas, exactly as the original did: type inference, NA handling and
    quoting are pandas'."""
    df = pd.read_csv(StringIO(tsv_body + "\n"), sep="\t")
    return csv_text(df)


def translation_dict(rows: Sequence[Sequence[str]]) -> dict[str, str]:
    """Original: ``dict(zip(df.iloc[:, 0], df.iloc[:, 1]))`` — duplicate
    source ids keep the last row.

    Rows whose SOURCE column is blank are dropped (census fast-follow,
    #4141): the full two-column scan near-certainly contains them (e.g.
    hgnc_symbol is empty for thousands of ENSG ids), and the '' key they
    created answered a blank translation target with an ARBITRARY dataset
    value presented as success. A blank key is unreachable by any
    legitimate target; dropping it changes nothing else."""
    return {row[0]: row[1] for row in rows if row[0].strip()}


def batch_translate_response(mapping: Mapping[str, str],
                             targets: Sequence[str]) -> dict:
    translations: dict[str, str] = {}
    not_found: list[str] = []
    for target in targets:
        if target in mapping:
            translations[target] = mapping[target]
        else:
            not_found.append(target)
    return {
        "translations": translations,
        "not_found": not_found,
        "found_count": len(translations),
        "not_found_count": len(not_found),
    }
