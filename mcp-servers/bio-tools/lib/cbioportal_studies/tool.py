"""cbioportal-studies — public tool surface.

Retrieval against the cBioPortal public REST API (www.cbioportal.org/api):

    list_studies(keyword=None, cancer_type_id=None, max_records=500)
    get_study(study_id)
    mutations_in_gene(gene_symbol, study_id, max_records=100)
    mutation_frequency(gene_symbol, study_ids)
    cna_in_gene(gene_symbol, study_id, event_type=..., max_records=100)
    clinical_attributes(study_id, max_records=200)

Honesty contract: every listing is either a complete retrieval verified
against the API's own total-count, or carries an explicit ``truncated`` flag
with the true ``total`` alongside the capped rows. Nothing is silently
truncated.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .client import (PAGE_ALL, CBioPortalClient, CBioPortalError, NotFound,
                     seg)
from .records import (shape_clinical_attribute, shape_cna, shape_mutation,
                      shape_profile, shape_study_detail, shape_study_row)

MAX_FREQUENCY_STUDIES = 12
CNA_EVENT_TYPES = {"HOMDEL_AND_AMP", "HOMDEL", "AMP", "GAIN", "HETLOSS",
                   "DIPLOID", "ALL"}
TOP_PROTEIN_CHANGES = 25
SURVIVAL_ATTRIBUTE_PREFIXES = ("OS_", "DFS_", "PFS_", "DSS_")


def _cap(max_records: int) -> int:
    """Listing caps must be positive ints — a negative slice would silently
    tail-drop rows and report a bogus ``truncated`` flag."""
    if not isinstance(max_records, int) or isinstance(max_records, bool) \
            or max_records < 1:
        raise ValueError(
            f"max_records must be a positive integer (got {max_records!r})")
    return max_records


class CBioPortalStudies:
    def __init__(self, client: CBioPortalClient | None = None):
        self.client = client or CBioPortalClient()



    def list_studies(self, keyword: str | None = None,
                     cancer_type_id: str | None = None,
                     max_records: int = 500) -> dict[str, Any]:
        """All public studies matching ``keyword`` (server-side match against
        name/description/cancer type), optionally narrowed client-side to an
        exact ``cancerTypeId``. Retrieval is complete (single full-size page)
        and verified against the META total-count; output is capped at
        max_records with an explicit truncated flag."""
        max_records = _cap(max_records)
        params: dict[str, Any] = {"projection": "DETAILED",
                                  "pageSize": PAGE_ALL, "pageNumber": 0}
        if keyword:
            params["keyword"] = keyword
        api_total = self.client.meta_count(
            "/studies", {"keyword": keyword} if keyword else {})
        rows, _ = self.client.get("/studies", params)
        rows = rows or []
        if len(rows) != api_total:
            raise CBioPortalError(
                f"retrieval incomplete: {len(rows)} studies vs "
                f"total-count={api_total}")
        shaped = [shape_study_row(r) for r in rows]
        if cancer_type_id:
            want = cancer_type_id.strip().lower()
            shaped = [s for s in shaped
                      if (s["cancer_type_id"] or "").lower() == want]
        shaped.sort(key=lambda s: s["study_id"] or "")
        truncated = len(shaped) > max_records
        return {
            "keyword": keyword,
            "cancer_type_id": cancer_type_id,
            "api_total_for_keyword": api_total,
            "count": len(shaped),
            "truncated": truncated,
            "studies": shaped[:max_records],
        }

    def get_study(self, study_id: str) -> dict[str, Any]:
        """One study's detail + true sample/patient counts + molecular
        profiles. 4 paced requests."""
        sid = study_id.strip()
        raw, _ = self.client.get(f"/studies/{seg(sid)}",
                                 {"projection": "DETAILED"})
        n_samples = self.client.meta_count(f"/studies/{seg(sid)}/samples")
        n_patients = self.client.meta_count(f"/studies/{seg(sid)}/patients")
        profiles, _ = self.client.get(f"/studies/{seg(sid)}/molecular-profiles",
                                      {"projection": "SUMMARY"})
        shaped_profiles = sorted((shape_profile(p) for p in profiles or []),
                                 key=lambda p: p["molecular_profile_id"] or "")
        rec = shape_study_detail(raw)
        rec["sample_count"] = n_samples
        rec["patient_count"] = n_patients
        rec["molecular_profiles"] = shaped_profiles
        return rec



    def _gene(self, gene_symbol: str) -> dict[str, Any]:
        """Resolve a HUGO symbol (or Entrez id string) via /genes/{id}."""
        raw, _ = self.client.get(f"/genes/{seg(gene_symbol.strip().upper())}")
        return {"symbol": raw.get("hugoGeneSymbol"),
                "entrez_gene_id": raw.get("entrezGeneId")}

    def _profile_for(self, study_id: str, alteration_type: str,
                     datatype: str | None = None) -> dict[str, Any]:
        profiles, _ = self.client.get(
            f"/studies/{seg(study_id)}/molecular-profiles",
            {"projection": "SUMMARY"})
        for p in profiles or []:
            if p.get("molecularAlterationType") != alteration_type:
                continue
            if datatype and p.get("datatype") != datatype:
                continue
            return p
        available = sorted({p.get("molecularAlterationType")
                            for p in profiles or []})
        raise NotFound(
            f"study {study_id!r} has no {alteration_type}"
            f"{'/' + datatype if datatype else ''} molecular profile "
            f"(available alteration types: {available})")

    def _require_all_sample_list(self, study_id: str) -> str:
        """Validate {study}_all exists (unknown ids are silently dropped
        upstream, which would otherwise read as 'zero alterations')."""
        list_id = f"{study_id}_all"
        found, _ = self.client.post("/sample-lists/fetch", [list_id],
                                    {"projection": "SUMMARY"})
        if not any(sl.get("sampleListId") == list_id for sl in found or []):
            raise NotFound(
                f"study {study_id!r} has no '{list_id}' sample list; "
                "cannot scope an all-samples query")
        return list_id

    def _fetch_mutations(self, profile_id: str, sample_list_id: str,
                         entrez_gene_id: int) -> list[dict[str, Any]]:
        """All mutation rows for one gene (single full-size page)."""
        rows, _ = self.client.post(
            f"/molecular-profiles/{seg(profile_id)}/mutations/fetch",
            {"sampleListId": sample_list_id,
             "entrezGeneIds": [entrez_gene_id]},
            {"projection": "SUMMARY", "pageSize": PAGE_ALL, "pageNumber": 0})
        return rows or []



    def mutations_in_gene(self, gene_symbol: str, study_id: str,
                          max_records: int = 100) -> dict[str, Any]:
        """Every mutation in one gene across one study's samples, with
        complete aggregate counts and a capped+flagged row listing."""
        max_records = _cap(max_records)
        gene = self._gene(gene_symbol)
        sid = study_id.strip()
        profile = self._profile_for(sid, "MUTATION_EXTENDED")
        profile_id = profile["molecularProfileId"]
        list_id = self._require_all_sample_list(sid)
        body = {"sampleListId": list_id,
                "entrezGeneIds": [gene["entrez_gene_id"]]}
        total = self.client.meta_count(
            f"/molecular-profiles/{seg(profile_id)}/mutations/fetch", body=body)
        raw = self._fetch_mutations(profile_id, list_id,
                                    gene["entrez_gene_id"])
        if len(raw) != total:
            raise CBioPortalError(
                f"retrieval incomplete: {len(raw)} mutation rows vs "
                f"total-count={total}")
        shaped = sorted(
            (shape_mutation(r) for r in raw),
            key=lambda m: (m["start_position"] or 0,
                           m["protein_change"] or "", m["sample_id"] or ""))
        protein_changes = Counter(m["protein_change"] for m in shaped
                                  if m["protein_change"])
        type_counts = Counter(m["mutation_type"] for m in shaped
                              if m["mutation_type"])
        truncated = total > max_records
        return {
            "gene": gene,
            "study_id": sid,
            "molecular_profile_id": profile_id,
            "total_mutations": total,
            "mutated_sample_count": len({m["sample_id"] for m in shaped}),
            "mutation_type_counts": dict(type_counts.most_common()),
            "distinct_protein_changes": len(protein_changes),
            "top_protein_changes": dict(
                protein_changes.most_common(TOP_PROTEIN_CHANGES)),
            "truncated": truncated,
            "mutations": shaped[:max_records],
        }

    def mutation_frequency(self, gene_symbol: str,
                           study_ids: list[str]) -> dict[str, Any]:
        """Fraction of sequenced samples carrying >=1 mutation in the gene,
        per study. Bounded: 1..MAX_FREQUENCY_STUDIES studies per call."""
        ids = [s.strip() for s in study_ids if s and s.strip()]
        if not ids or len(ids) > MAX_FREQUENCY_STUDIES:
            raise ValueError(
                f"study_ids must list 1..{MAX_FREQUENCY_STUDIES} study ids "
                f"(got {len(ids)}); call repeatedly to cover more studies")
        gene = self._gene(gene_symbol)
        studies, _ = self.client.post("/studies/fetch", ids,
                                      {"projection": "DETAILED"})
        by_id = {s["studyId"]: s for s in studies or []}
        unknown = sorted(set(ids) - set(by_id))
        profiles, _ = self.client.post(
            "/molecular-profiles/fetch", {"studyIds": sorted(by_id)},
            {"projection": "SUMMARY"})
        mut_profile = {p["studyId"]: p["molecularProfileId"]
                       for p in profiles or []
                       if p.get("molecularAlterationType") == "MUTATION_EXTENDED"}
        wanted_lists = [f"{s}_all" for s in sorted(by_id)]
        found_lists, _ = self.client.post("/sample-lists/fetch", wanted_lists,
                                          {"projection": "SUMMARY"})
        have_list = {sl.get("sampleListId") for sl in found_lists or []}
        rows_out: list[dict[str, Any]] = []
        no_mutation_data: list[str] = []
        for sid in sorted(by_id):
            if sid not in mut_profile or f"{sid}_all" not in have_list:
                no_mutation_data.append(sid)
                continue
            raw = self._fetch_mutations(mut_profile[sid], f"{sid}_all",
                                        gene["entrez_gene_id"])
            mutated = len({r.get("sampleId") for r in raw})
            sequenced = by_id[sid].get("sequencedSampleCount") or 0
            rows_out.append({
                "study_id": sid,
                "study_name": by_id[sid].get("name"),
                "molecular_profile_id": mut_profile[sid],
                "mutation_count": len(raw),
                "mutated_samples": mutated,
                "sequenced_samples": sequenced,
                "frequency": (round(mutated / sequenced, 4)
                              if sequenced else None),
            })
        rows_out.sort(key=lambda r: (-(r["frequency"] or 0.0), r["study_id"]))
        return {
            "gene": gene,
            "count": len(rows_out),
            "frequencies": rows_out,
            "unknown_studies": unknown,
            "no_mutation_data": no_mutation_data,
        }



    def cna_in_gene(self, gene_symbol: str, study_id: str,
                    event_type: str = "HOMDEL_AND_AMP",
                    max_records: int = 100) -> dict[str, Any]:
        """Discrete copy-number events for one gene in one study, with
        complete per-level counts and a capped+flagged event listing."""
        max_records = _cap(max_records)
        ev = event_type.strip().upper()
        if ev not in CNA_EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {sorted(CNA_EVENT_TYPES)} "
                f"(got {event_type!r})")
        gene = self._gene(gene_symbol)
        sid = study_id.strip()
        profile = self._profile_for(sid, "COPY_NUMBER_ALTERATION", "DISCRETE")
        profile_id = profile["molecularProfileId"]
        list_id = self._require_all_sample_list(sid)
        path = f"/molecular-profiles/{seg(profile_id)}/discrete-copy-number/fetch"
        body = {"sampleListId": list_id,
                "entrezGeneIds": [gene["entrez_gene_id"]]}
        total = self.client.meta_count(
            path, {"discreteCopyNumberEventType": ev}, body=body)
        raw, _ = self.client.post(
            path, body, {"discreteCopyNumberEventType": ev,
                         "projection": "SUMMARY",
                         "pageSize": PAGE_ALL, "pageNumber": 0})
        raw = raw or []
        if len(raw) != total:
            raise CBioPortalError(
                f"retrieval incomplete: {len(raw)} CNA rows vs "
                f"total-count={total}")
        shaped = sorted((shape_cna(r) for r in raw),
                        key=lambda c: c["sample_id"] or "")
        level_counts = Counter(c["alteration_label"] or str(c["alteration"])
                               for c in shaped)
        truncated = total > max_records
        return {
            "gene": gene,
            "study_id": sid,
            "molecular_profile_id": profile_id,
            "event_type": ev,
            "total_events": total,
            "altered_sample_count": len({c["sample_id"] for c in shaped}),
            "alteration_counts": dict(level_counts.most_common()),
            "truncated": truncated,
            "events": shaped[:max_records],
        }



    def clinical_attributes(self, study_id: str,
                            max_records: int = 200) -> dict[str, Any]:
        """One study's clinical attribute catalogue (count-verified), with
        survival-attribute convenience flags."""
        max_records = _cap(max_records)
        sid = study_id.strip()
        path = f"/studies/{seg(sid)}/clinical-attributes"
        total = self.client.meta_count(path)
        raw, _ = self.client.get(path, {"projection": "SUMMARY",
                                        "pageSize": PAGE_ALL,
                                        "pageNumber": 0})
        raw = raw or []
        if len(raw) != total:
            raise CBioPortalError(
                f"retrieval incomplete: {len(raw)} attributes vs "
                f"total-count={total}")
        shaped = sorted((shape_clinical_attribute(a) for a in raw),
                        key=lambda a: a["attribute_id"] or "")
        ids = {a["attribute_id"] for a in shaped}
        survival = sorted(i for i in ids
                          if i and i.startswith(SURVIVAL_ATTRIBUTE_PREFIXES))
        truncated = total > max_records
        return {
            "study_id": sid,
            "total_attributes": total,
            "patient_level_count": sum(1 for a in shaped
                                       if a["level"] == "patient"),
            "sample_level_count": sum(1 for a in shaped
                                      if a["level"] == "sample"),
            "survival_attributes": survival,
            "has_overall_survival": "OS_STATUS" in ids and "OS_MONTHS" in ids,
            "truncated": truncated,
            "attributes": shaped[:max_records],
        }
