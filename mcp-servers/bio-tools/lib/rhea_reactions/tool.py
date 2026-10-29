"""Rhea retrieval methods (reaction search + full reaction records) over
:class:`RheaSparqlClient`.

Searches return honest totals: every capped listing runs a companion
COUNT(*) query so ``api_total`` is the true match count even when ``limit``
truncates the rows.
"""

from __future__ import annotations

import re

from .client import RheaSparqlClient

_RHEA_RE = re.compile(r"^(?:RHEA:)?(\d+)$", re.IGNORECASE)
_CHEBI_RE = re.compile(r"^(?:CHEBI:)?(\d+)$", re.IGNORECASE)
_EC_RE = re.compile(r"^\d+\.\d+\.\d+\.n?\d+$")


class NotFound(RuntimeError):
    """No Rhea reaction with the requested accession."""


def normalize_rhea_id(rhea_id: str | int) -> str:
    """Accept ``RHEA:10280`` / ``10280`` / 10280; return ``RHEA:10280``."""
    m = _RHEA_RE.match(str(rhea_id).strip())
    if not m:
        raise ValueError(f"not a Rhea ID: {rhea_id!r}")
    return f"RHEA:{m.group(1)}"


def _sparql_escape(text: str) -> str:
    """Escape for a single-line SPARQL string literal — closes the whole
    control-character class, not just quote/backslash: raw LF/CR are
    grammar-forbidden inside '"…"', and the remaining
    C0 controls are stripped (they never occur in legitimate search text).
    """
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "".join(ch for ch in out if ch >= " " or ch in "\\")


class RheaReactions:
    """High-level Rhea reaction retrieval."""

    def __init__(self, client: RheaSparqlClient | None = None):
        self.client = client or RheaSparqlClient()



    def search_by_text(self, text: str, limit: int) -> dict:
        """Master reactions whose chemical equation contains ``text``
        (case-insensitive substring)."""
        needle = _sparql_escape(text.strip().lower())
        where = f"""\
  ?r rdfs:subClassOf rh:Reaction ; rh:accession ?accession ;
     rh:equation ?equation ; rh:status ?status .
  FILTER(CONTAINS(LCASE(STR(?equation)), "{needle}"))"""
        return self._run_search(where, limit)

    def search_by_chebi(self, chebi_id: str | int, limit: int) -> dict:
        """Master reactions with a participant mapped to the given ChEBI ID
        (small molecules via rh:chebi, generic compounds via their reactive
        part, polymers via their underlying ChEBI)."""
        m = _CHEBI_RE.match(str(chebi_id).strip())
        if not m:
            raise ValueError(f"not a ChEBI ID: {chebi_id!r}")
        uri = f"<http://purl.obolibrary.org/obo/CHEBI_{m.group(1)}>"
        where = f"""\
  ?r rdfs:subClassOf rh:Reaction ; rh:accession ?accession ;
     rh:equation ?equation ; rh:status ?status ;
     rh:side/rh:contains/rh:compound ?c .
  {{ ?c rh:chebi {uri} }}
  UNION {{ ?c rh:reactivePart/rh:chebi {uri} }}
  UNION {{ ?c rh:underlyingChebi {uri} }}"""
        return self._run_search(where, limit, distinct=True)

    def search_by_ec(self, ec: str, limit: int) -> dict:
        """Master reactions linked to an EC (enzyme classification) number."""
        ec = ec.strip()
        if not _EC_RE.match(ec):
            raise ValueError(f"not a full EC number: {ec!r}")
        where = f"""\
  ?r rdfs:subClassOf rh:Reaction ; rh:accession ?accession ;
     rh:equation ?equation ; rh:status ?status ;
     rh:ec <http://purl.uniprot.org/enzyme/{ec}> ."""
        return self._run_search(where, limit)

    def _run_search(self, where: str, limit: int, distinct: bool = False) -> dict:
        if not (1 <= limit <= 500):
            raise ValueError("limit must be in [1, 500]")
        kw = "DISTINCT " if distinct else ""
        rows = self.client.select(
            f"SELECT {kw}?accession ?equation ?status WHERE {{\n{where}\n}} "
            f"ORDER BY ?accession LIMIT {limit}")
        count_rows = self.client.select(
            f"SELECT (COUNT(DISTINCT ?accession) AS ?n) WHERE {{\n{where}\n}}")
        total = int(count_rows[0]["n"]) if count_rows else 0
        reactions = [{"rhea_id": r.get("accession"),
                      "equation": r.get("equation"),
                      "status": _localname(r.get("status"))}
                     for r in rows]
        return {"api_total": total, "n_returned": len(reactions),
                "truncated": total > len(reactions), "reactions": reactions}



    def get_reaction(self, rhea_id: str | int) -> dict:
        """Full record for one master (undirected) Rhea reaction.

        Directional/bidirectional accessions (e.g. RHEA:10281) are resolved
        to scalars too, but participants are only attached to master IDs —
        the returned ``directional_reactions``/``bidirectional_reaction``
        fields give the ID family.
        """
        acc = normalize_rhea_id(rhea_id)
        preds = self.client.select(
            f'SELECT ?p ?o WHERE {{ ?r rh:accession "{acc}" . ?r ?p ?o . }}')
        if not preds:
            raise NotFound(f"no Rhea reaction {acc}")

        record: dict = {"rhea_id": acc, "equation": None, "status": None,
                        "is_transport": None, "is_chemically_balanced": None,
                        "ec_numbers": [], "pubmed_ids": [],
                        "directional_reactions": [],
                        "bidirectional_reaction": None}
        for row in preds:
            p = _localname(row["p"])
            o = row["o"]
            if p == "equation":
                record["equation"] = o
            elif p == "status":
                record["status"] = _localname(o)
            elif p == "isTransport":
                record["is_transport"] = o == "true"
            elif p == "isChemicallyBalanced":
                record["is_chemically_balanced"] = o == "true"
            elif p == "ec":
                record["ec_numbers"].append(o.rsplit("/", 1)[-1])
            elif p == "citation":
                record["pubmed_ids"].append(o.rsplit("/", 1)[-1])
            elif p == "directionalReaction":
                record["directional_reactions"].append(
                    "RHEA:" + o.rsplit("/", 1)[-1])
            elif p == "bidirectionalReaction":
                record["bidirectional_reaction"] = "RHEA:" + o.rsplit("/", 1)[-1]
        record["ec_numbers"].sort()
        record["pubmed_ids"].sort()
        record["directional_reactions"].sort()

        parts = self.client.select(f"""\
SELECT ?side ?coefProp ?cacc ?cname WHERE {{
  ?r rh:accession "{acc}" ; rh:side ?side .
  ?side ?coefProp ?part . ?coefProp rdfs:subPropertyOf rh:contains .
  ?part rh:compound ?c . ?c rh:accession ?cacc .
  OPTIONAL {{ ?c rh:name ?cname }}
}}""")
        left, right = [], []
        for row in parts:
            side = row.get("side", "")
            entry = {"compound_accession": row.get("cacc"),
                     "name": row.get("cname"),
                     "coefficient": _coefficient(row.get("coefProp", ""))}
            if side.endswith("_L"):
                left.append(entry)
            elif side.endswith("_R"):
                right.append(entry)
        key = lambda e: (e["compound_accession"] or "", e["name"] or "")
        record["left_side"] = sorted(left, key=key)
        record["right_side"] = sorted(right, key=key)
        return record


def _localname(uri: str | None) -> str | None:
    if uri is None:
        return None
    return uri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _coefficient(coef_prop_uri: str) -> str:

    local = _localname(coef_prop_uri) or ""
    return local.removeprefix("contains") or "1"
