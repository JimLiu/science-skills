"""High-level tool surface mirroring the 8 tooluniverse/clingen MCP methods.

Caching: the bulk tables (validity, dosage, actionability per context) are
fetched once per ClinGenCurations instance and filtered client-side — the
upstream list endpoints ignore (validity) or only partially support
(actionability) server-side filtering, and the full tables are <1 MB each.
ERepo queries are per-gene/per-variant server-side (matchLimit=none for
complete retrieval) and cached per query key.

Count verification: /api/validity and /api/dosage carry a ``total`` field —
every fetch asserts total == len(rows) (raises ClinGenApiError otherwise).
The actionability flat table and ERepo light context carry no count field;
their completeness is established in the gate by independent formulations
(see bench/run_gate.py).
"""
from __future__ import annotations

from .client import ClinGenClient, ClinGenApiError, SEARCH_BASE, ACTIONABILITY_BASE, EREPO_BASE
from .records import (validity_record, dosage_record, actionability_record,
                      erepo_record)


class ClinGenCurations:
    def __init__(self, client: ClinGenClient | None = None):
        self.client = client or ClinGenClient()
        self._validity_rows: list | None = None
        self._dosage_rows: list | None = None
        self._actionability: dict[str, dict] = {}
        self._erepo_cache: dict[tuple, list] = {}



    def _validity_table(self) -> list:
        if self._validity_rows is None:
            d = self.client.get_json(f"{SEARCH_BASE}/api/validity")
            if d["total"] != len(d["rows"]):
                raise ClinGenApiError(
                    f"validity count mismatch: total={d['total']} rows={len(d['rows'])}")
            self._validity_rows = [validity_record(r) for r in d["rows"]]
        return self._validity_rows

    def _dosage_table(self) -> list:
        if self._dosage_rows is None:
            d = self.client.get_json(f"{SEARCH_BASE}/api/dosage")
            if d["total"] != len(d["rows"]):
                raise ClinGenApiError(
                    f"dosage count mismatch: total={d['total']} rows={len(d['rows'])}")
            self._dosage_rows = [dosage_record(r) for r in d["rows"]]
        return self._dosage_rows

    def _actionability_table(self, context: str) -> dict:
        if context not in ("Adult", "Pediatric"):
            raise ValueError("context must be 'Adult' or 'Pediatric'")
        if context not in self._actionability:
            d = self.client.get_json(
                f"{ACTIONABILITY_BASE}/ac/{context}/api/summ", params={"flavor": "flat"})
            if not isinstance(d, dict) or "columns" not in d or "rows" not in d:
                raise ClinGenApiError(
                    f"unexpected actionability payload shape for {context}: "
                    f"{type(d).__name__} with keys "
                    f"{list(d)[:5] if isinstance(d, dict) else 'n/a'}")
            self._actionability[context] = d
        return self._actionability[context]



    def gene_validity(self, gene: str | None = None) -> dict:
        """Gene-disease validity curations; all 3,600+ or filtered to one gene.

        Exact, case-insensitive gene-symbol match (the MCP's substring match
        is a footgun: 'BRCA1' would also hit hypothetical 'BRCA10').
        """
        records = self._validity_table()
        if gene:
            g = gene.strip().upper()
            records = [r for r in records if r["gene_symbol"].upper() == g]
        records = sorted(records, key=lambda r: (r["gene_symbol"], r["assertion_id"] or ""))
        return {"total": len(records), "records": records,
                "source": "ClinGen Gene-Disease Validity (search.clinicalgenome.org/api/validity)"}

    def dosage_sensitivity(self, gene: str | None = None,
                           include_regions: bool = False) -> dict:
        """Dosage sensitivity (haploinsufficiency / triplosensitivity) curations.

        ``include_regions=True`` adds ISCA region records (recurrent CNV
        regions etc.) to the gene records, or — with ``gene`` unset — returns
        them in the bulk listing. A region ID (ISCA-…) can also be passed as
        ``gene`` to fetch one region record.
        """
        records = self._dosage_table()
        if not include_regions and gene is None:
            records = [r for r in records if r["record_type"] == "gene"]
        if gene:
            g = gene.strip().upper()
            records = [r for r in records
                       if r["symbol"].upper() == g or (r["id"] or "").upper() == g]
        records = sorted(records, key=lambda r: (r["record_type"], r["symbol"], r["id"] or ""))
        return {"total": len(records), "records": records,
                "source": "ClinGen Dosage Sensitivity (search.clinicalgenome.org/api/dosage)"}

    def actionability(self, gene: str | None = None, context: str = "both") -> dict:
        """Clinical actionability curations (assertion rows incl. scores).

        ``context``: 'adult', 'pediatric', or 'both'. Gene filtering matches
        any member of multi-gene topics (e.g. BRCA1,BRCA2 HBOC docs).
        """
        ctx_map = {"adult": ["Adult"], "pediatric": ["Pediatric"],
                   "both": ["Adult", "Pediatric"]}
        if context not in ctx_map:
            raise ValueError("context must be 'adult', 'pediatric' or 'both'")
        out = {}
        for ctx in ctx_map[context]:
            d = self._actionability_table(ctx)
            records = [actionability_record(d["columns"], row) for row in d["rows"]]
            if gene:
                g = gene.strip().upper()
                records = [r for r in records
                           if g in [x.upper() for x in r["genes"]]]
            records.sort(key=lambda r: (r["doc_id"] or "", r["outcome"] or "",
                                        r["intervention"] or ""))
            out[ctx.lower()] = {"total": len(records), "records": records}
        out["source"] = ("ClinGen Clinical Actionability "
                         "(actionability.clinicalgenome.org flat summaries)")
        return out

    def variant_classifications(self, gene: str | None = None,
                                caid: str | None = None,
                                hgvs: str | None = None) -> dict:
        """VCEP variant pathogenicity classifications from the ClinGen
        Evidence Repository (erepo.genome.network).

        Exactly one of ``gene``, ``caid``, ``hgvs`` must be given (the bulk
        12k-variant dump is deliberately not exposed here — use the ERepo TSV
        download for corpus work). Retrieval is complete: matchLimit=none.
        """
        keys = [("gene", gene), ("caid", caid), ("hgvs", hgvs)]
        given = [(k, v) for k, v in keys if v]
        if len(given) != 1:
            raise ValueError("provide exactly one of gene=, caid=, hgvs=")
        param, value = given[0]
        cache_key = (param, value)
        if cache_key not in self._erepo_cache:
            params = {param: value, "matchMode": "exact", "matchLimit": "none"}
            d = self.client.get_json(f"{EREPO_BASE}/classifications", params=params)
            interps = d.get("variantInterpretations") or []
            records = [erepo_record(i) for i in interps]
            records.sort(key=lambda r: r["interpretation_id"] or "")
            self._erepo_cache[cache_key] = records
        records = self._erepo_cache[cache_key]
        return {"total": len(records), "records": records,
                "query": {param: value},
                "source": "ClinGen Evidence Repository (erepo.genome.network/evrepo/api)"}
