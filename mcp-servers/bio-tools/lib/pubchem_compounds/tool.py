"""PubChem retrieval methods (identifier resolution, properties, assays,
similarity, GHS safety) over :class:`PugRestClient`.

All methods return JSON-serializable Python structures with deterministic
ordering; marshalling/caps live in the tier-2 server.
"""

from __future__ import annotations

from .client import PUG_BASE, PUG_VIEW_BASE, NotFound, PubChemApiError, PugRestClient



PROPERTY_LIST = (
    "MolecularFormula,MolecularWeight,SMILES,ConnectivitySMILES,InChI,"
    "InChIKey,IUPACName,XLogP,ExactMass,TPSA,Charge,HBondDonorCount,"
    "HBondAcceptorCount,RotatableBondCount,HeavyAtomCount"
)

SEARCH_NAMESPACES = ("name", "smiles", "inchikey", "cid")


class PubChemCompounds:
    """High-level PubChem compound retrieval."""

    def __init__(self, client: PugRestClient | None = None):
        self.client = client or PugRestClient()



    def search_cids(self, query: str, namespace: str = "name") -> list[int]:
        """Resolve an identifier to PubChem CIDs (empty list when no match).

        Every namespace goes via POST form data so path-reserved characters
        (slashes in combination-drug names, ``#``/``+`` in SMILES...) are
        transmitted intact instead of being interpolated into the URL path.
        """
        if namespace not in SEARCH_NAMESPACES:
            raise ValueError(f"namespace must be one of {SEARCH_NAMESPACES}")
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        query = query.strip()
        try:
            payload = self.client.request_json(
                f"{PUG_BASE}/compound/{namespace}/cids/JSON",
                data={namespace: query})
        except NotFound:
            return []
        return list(payload.get("IdentifierList", {}).get("CID", []))



    def properties(self, cids: list[int]) -> list[dict]:
        """Computed properties for a batch of CIDs (one request).

        Rows come back in input order; unknown CIDs are absent from the
        table ([] when ALL are unknown — the API's batch-wide 404 is mapped
        to the same absent-row semantics, never raised).
        """
        if not cids:
            return []
        try:
            payload = self.client.request_json(
                f"{PUG_BASE}/compound/cid/property/{PROPERTY_LIST}/JSON",
                data={"cid": ",".join(str(c) for c in cids)})
        except NotFound:
            return []
        return list(payload.get("PropertyTable", {}).get("Properties", []))

    def synonyms(self, cids: list[int]) -> dict[int, list[str]]:
        """Synonym lists for a batch of CIDs (one request), keyed by CID.

        Unknown CIDs are absent from the result ({} when ALL are unknown).
        """
        if not cids:
            return {}
        try:
            payload = self.client.request_json(
                f"{PUG_BASE}/compound/cid/synonyms/JSON",
                data={"cid": ",".join(str(c) for c in cids)})
        except NotFound:
            return {}
        out: dict[int, list[str]] = {}
        for info in payload.get("InformationList", {}).get("Information", []):
            out[int(info["CID"])] = list(info.get("Synonym", []))
        return out



    def assay_summary(self, cid: int) -> list[dict]:
        """All bioassay summary rows for a CID (column names -> values).

        Returns [] when the compound has no assay data (PUGREST.NotFound).
        """
        try:
            payload = self.client.request_json(
                f"{PUG_BASE}/compound/cid/{int(cid)}/assaysummary/JSON")
        except NotFound:
            return []
        table = payload.get("Table", {})
        columns = table.get("Columns", {}).get("Column", [])
        rows = []
        for row in table.get("Row", []):
            cells = row.get("Cell", [])
            rows.append({col: cell for col, cell in zip(columns, cells)})
        return rows



    def similarity_cids(self, smiles: str, threshold: int = 90,
                        max_records: int = 50) -> list[int]:
        """Synchronous 2D Tanimoto similarity search (fastsimilarity_2d).

        Uses PubChem's fast synchronous route — no ListKey polling, so the
        call fits the tool budget. Results are capped server-side by
        MaxRecords (upstream orders by similarity/relevance).
        """
        if not smiles or not smiles.strip():
            raise ValueError("smiles must be non-empty")
        if not (0 < threshold <= 100):
            raise ValueError("threshold must be in (0, 100]")
        try:
            payload = self.client.request_json(
                f"{PUG_BASE}/compound/fastsimilarity_2d/smiles/cids/JSON",
                params={"Threshold": int(threshold),
                        "MaxRecords": int(max_records)},
                data={"smiles": smiles.strip()})
        except NotFound:
            return []
        return list(payload.get("IdentifierList", {}).get("CID", []))



    def ghs_classification(self, cid: int) -> dict | None:
        """GHS classification parsed from PUG-View (None when absent).

        Aggregates across all reporting sources: unique signal words,
        pictogram labels, hazard statements and precautionary codes, plus
        the reference (source) count.
        """
        try:
            payload = self.client.request_json(
                f"{PUG_VIEW_BASE}/data/compound/{int(cid)}/JSON",
                params={"heading": "GHS Classification"})
        except NotFound:
            return None
        record = payload.get("Record", {})
        section = _find_section(record.get("Section", []), "GHS Classification")
        if section is None:
            return None

        signals, pictograms, hazards, precautionary, notes = [], [], [], [], []
        for info in section.get("Information", []):
            name = info.get("Name", "")
            strings = info.get("Value", {}).get("StringWithMarkup", [])
            if name == "Signal":
                for s in strings:
                    _add_unique(signals, s.get("String", "").strip())
            elif name == "Pictogram(s)":
                for s in strings:
                    for markup in s.get("Markup", []):
                        _add_unique(pictograms, markup.get("Extra", "").strip())
            elif name == "GHS Hazard Statements":
                for s in strings:
                    _add_unique(hazards, s.get("String", "").strip())
            elif name == "Precautionary Statement Codes":
                for s in strings:
                    _add_unique(precautionary, s.get("String", "").strip())
            elif name == "Note":
                for s in strings:
                    _add_unique(notes, s.get("String", "").strip())
        n_references = len(record.get("Reference", []))
        return {
            "cid": int(cid),
            "record_title": record.get("RecordTitle"),
            "signals": signals,
            "pictograms": pictograms,
            "hazard_statements": hazards,
            "precautionary_statement_codes": precautionary,
            "notes": notes,
            "n_source_references": n_references,
        }


def _find_section(sections: list[dict], heading: str) -> dict | None:
    for sec in sections:
        if sec.get("TOCHeading") == heading:
            return sec
        found = _find_section(sec.get("Section", []), heading)
        if found is not None:
            return found
    return None


def _add_unique(acc: list[str], item: str) -> None:
    if item and item not in acc:
        acc.append(item)
