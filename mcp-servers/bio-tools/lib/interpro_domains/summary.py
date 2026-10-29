"""Build deterministic per-protein domain-architecture summaries from raw InterPro API results.

The raw API result for ``/entry/interpro/protein/uniprot/<acc>/`` is a list of objects:

    {"metadata": {accession, name, source_database, type, integrated, member_databases,
                  go_terms},
     "proteins": [{accession, protein_length, source_database, organism, in_alphafold,
                   entry_protein_locations: [{fragments: [{start, end, dc-status}],
                                              representative, model, score}]}]}

The summary keeps the scientifically relevant content (entry accession, name, type,
member-database signatures, and the entry's locations on the protein) and drops
presentation/bookkeeping fields.  Ordering is fully deterministic:

* entries sorted by (type, accession)
* member-db signatures sorted by (database, signature accession)
* locations sorted by (start of first fragment, end of last fragment)
* fragments within a location sorted by (start, end)

Default-valued location fields are OMITTED from the summary to keep the machine-readable
output compact; an absent key always means the default value:

* ``model``  absent  -> null   (the API reports null for integrated InterPro entries)
* ``score``  absent  -> null
* ``representative`` absent -> false
* fragment ``dc_status`` absent -> "CONTINUOUS"

Non-default values (e.g. a discontinuous fragment status, ``representative: true``) are
always emitted, so no information is lost.
"""

from __future__ import annotations


TYPE_ORDER = [
    "family",
    "domain",
    "repeat",
    "homologous_superfamily",
    "conserved_site",
    "active_site",
    "binding_site",
    "ptm",
]


def _type_rank(entry_type: str) -> int:
    try:
        return TYPE_ORDER.index(entry_type)
    except ValueError:
        return len(TYPE_ORDER)


def _fragment_dict(frag: dict) -> dict:
    out = {"start": frag.get("start"), "end": frag.get("end")}
    dc_status = frag.get("dc-status")
    if dc_status not in (None, "CONTINUOUS"):
        out["dc_status"] = dc_status
    return out


def _sorted_locations(entry_protein_locations: list | None) -> list[dict]:
    locations = []
    for loc in entry_protein_locations or []:
        fragments = sorted(
            (_fragment_dict(frag) for frag in loc.get("fragments", [])),
            key=lambda f: (f["start"] if f["start"] is not None else -1,
                           f["end"] if f["end"] is not None else -1),
        )
        loc_out = {"fragments": fragments}
        if loc.get("representative"):
            loc_out["representative"] = True
        if loc.get("model") is not None:
            loc_out["model"] = loc.get("model")
        if loc.get("score") is not None:
            loc_out["score"] = loc.get("score")
        locations.append(loc_out)
    locations.sort(
        key=lambda l: (
            l["fragments"][0]["start"] if l["fragments"] and l["fragments"][0]["start"] is not None else -1,
            l["fragments"][-1]["end"] if l["fragments"] and l["fragments"][-1]["end"] is not None else -1,
        )
    )
    return locations


def _member_signatures(member_databases: dict | None) -> list[dict]:
    signatures = []
    for db, sigs in (member_databases or {}).items():
        for sig_acc, sig_name in (sigs or {}).items():
            signatures.append({"database": db, "accession": sig_acc, "name": sig_name})
    signatures.sort(key=lambda s: (s["database"], s["accession"]))
    return signatures


def build_summary(accession: str, count: int, results: list[dict]) -> dict:
    """Build the deterministic architecture summary for one protein.

    Parameters
    ----------
    accession:
        The UniProt accession as queried (kept verbatim, upper-cased for display).
    count:
        The API's ``count`` field (number of matching InterPro entries).
    results:
        Concatenated ``results`` lists from all pages.
    """
    protein_length = None
    entries = []
    for item in results:
        meta = item.get("metadata", {})

        protein_match = None
        for prot in item.get("proteins", []):
            if prot.get("accession", "").upper() == accession.upper():
                protein_match = prot
                break
        if protein_match is None and item.get("proteins"):
            protein_match = item["proteins"][0]
        if protein_match is not None and protein_length is None:
            protein_length = protein_match.get("protein_length")
        entries.append(
            {
                "accession": meta.get("accession"),
                "name": meta.get("name"),
                "type": meta.get("type"),
                "member_db_signatures": _member_signatures(meta.get("member_databases")),
                "locations": _sorted_locations(
                    (protein_match or {}).get("entry_protein_locations")
                ),
            }
        )
    entries.sort(key=lambda e: (_type_rank(e["type"]), e["accession"] or ""))
    return {
        "protein": accession.upper(),
        "protein_length": protein_length,
        "entry_count": count,
        "entries": entries,
    }


def summary_to_text(summary: dict) -> str:
    """Render a summary as a compact, human/agent-readable text block (deterministic)."""
    lines = [
        f"protein\t{summary['protein']}",
        f"protein_length\t{summary['protein_length']}",
        f"interpro_entry_count\t{summary['entry_count']}",
    ]
    for e in summary["entries"]:
        sigs = ",".join(f"{s['database']}:{s['accession']}" for s in e["member_db_signatures"])
        locs = ";".join(
            "|".join(f"{f['start']}-{f['end']}" for f in loc["fragments"]) for loc in e["locations"]
        )
        lines.append(f"{e['accession']}\t{e['type']}\t{e['name']}\t{sigs}\t{locs}")
    return "\n".join(lines) + "\n"
