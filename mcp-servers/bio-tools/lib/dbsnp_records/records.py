"""Distill dbSNP Variation Services RefSNP JSON (~100 KB/rs) into lean,
JSON-able records (a few KB): canonical placements on GRCh38+GRCh37,
per-alt-allele frequency studies, ClinVar xrefs, and gene context."""
from __future__ import annotations

MAX_CITATIONS = 20


_NC_CHROM = {**{f"NC_{n:06d}": str(n) for n in range(1, 23)},
             "NC_000023": "X", "NC_000024": "Y", "NC_012920": "MT"}


def _chrom(seq_id: str) -> str | None:
    return _NC_CHROM.get((seq_id or "").split(".")[0])


def _assembly_placements(psd: dict) -> list[dict]:
    """The top-level chromosome placements (GRCh38 + GRCh37), 1-based."""
    out = []
    for p in psd.get("placements_with_allele") or []:
        traits = (p.get("placement_annot") or {}).get(
            "seq_id_traits_by_assembly") or []
        if not traits or not traits[0].get("is_chromosome"):
            continue
        assembly_full = traits[0].get("assembly_name") or ""
        alleles = [a.get("allele", {}).get("spdi", {})
                   for a in p.get("alleles") or []]
        alleles = [a for a in alleles if a]
        if not alleles:
            continue
        ref = alleles[0].get("deleted_sequence")
        alts = sorted({a.get("inserted_sequence") for a in alleles
                       if a.get("inserted_sequence")
                       != a.get("deleted_sequence")})
        out.append({
            "assembly": assembly_full.split(".")[0] or assembly_full,
            "assembly_full": assembly_full,
            "seq_id": p.get("seq_id"),
            "chrom": _chrom(p.get("seq_id")),
            "position": alleles[0].get("position", -1) + 1,
            "ref": ref,
            "alts": alts,
            "is_primary": bool(p.get("is_ptlp")),
        })
    out.sort(key=lambda r: (not r["is_primary"], r["assembly"]))
    return out


def _spdi_str(spdi: dict) -> str:
    return (f"{spdi.get('seq_id')}:{spdi.get('position')}:"
            f"{spdi.get('deleted_sequence')}:{spdi.get('inserted_sequence')}")


def _frequencies(annotation: dict) -> list[dict]:
    """Per-study allele frequencies for one alt allele, deduped by
    (study, version); ``af`` computed as allele_count/total_count."""
    rows, seen = [], set()
    for f in annotation.get("frequency") or []:
        key = (f.get("study_name"), f.get("study_version"))
        if key in seen:
            continue
        seen.add(key)
        ac, tc = f.get("allele_count"), f.get("total_count")
        rows.append({
            "study": f.get("study_name"),
            "study_version": f.get("study_version"),
            "allele_count": ac,
            "total_count": tc,
            "af": round(ac / tc, 6) if ac is not None and tc else None,
        })
    rows.sort(key=lambda r: (r["study"] or "", r["study_version"] or 0))
    return rows


def _clinvar(annotation: dict) -> list[dict]:
    rows = []
    for c in annotation.get("clinical") or []:
        rows.append({
            "rcv_accession": c.get("accession_version"),
            "clinical_significances": c.get("clinical_significances") or [],
            "review_status": c.get("review_status"),
            "last_evaluated_date": c.get("last_evaluated_date"),
            "disease_names": c.get("disease_names") or [],
        })
    rows.sort(key=lambda r: r["rcv_accession"] or "")
    return rows


def _genes(annotation: dict, mane_ids: set[str]) -> list[dict]:
    """Gene context from the GRCh38 assembly annotation: symbol, id,
    orientation, union of consequence SO terms, MANE Select HGVS."""
    out = []
    for asm in annotation.get("assembly_annotation") or []:
        for g in asm.get("genes") or []:
            consequences: set[str] = set()
            mane: list[dict] = []
            for rna in g.get("rnas") or []:
                for so in rna.get("sequence_ontology") or []:
                    consequences.add(so.get("name"))
                protein = rna.get("protein") or {}
                for so in protein.get("sequence_ontology") or []:
                    consequences.add(so.get("name"))
                if rna.get("id") in mane_ids:
                    pv = (protein.get("variant") or {}).get("spdi") or {}
                    mane.append({
                        "transcript_hgvs": rna.get("hgvs"),
                        "protein_spdi": _spdi_str(pv) if pv else None,
                    })
            out.append({
                "symbol": g.get("locus"),
                "gene_id": g.get("id"),
                "name": g.get("name"),
                "orientation": g.get("orientation"),
                "consequences": sorted(c for c in consequences if c),
                "mane_select": mane,
            })
    out.sort(key=lambda r: r["symbol"] or "")
    return out


def distill_refsnp(payload: dict) -> dict:
    """Full RefSNP JSON -> lean record.

    ``status`` is ``live`` (has primary_snapshot_data), ``merged`` (follow
    ``merged_into``), or ``no_data`` (withdrawn/unsupported rs numbers).
    Alleles are emitted per alt with SPDI/HGVS, frequency studies, ClinVar
    xrefs, and gene context. Citations are capped at MAX_CITATIONS with the
    true total in ``n_citations``.
    """
    rsid = f"rs{payload.get('refsnp_id')}"
    citations = payload.get("citations") or []
    base = {
        "rsid": rsid,
        "create_date": payload.get("create_date"),
        "last_update_date": payload.get("last_update_date"),
        "last_update_build_id": payload.get("last_update_build_id"),
        "n_citations": len(citations),
        "citations_pmids": citations[:MAX_CITATIONS],
        "citations_truncated": len(citations) > MAX_CITATIONS,
    }
    merged = payload.get("merged_snapshot_data") or {}
    if merged.get("merged_into"):
        return {**base, "status": "merged",
                "merged_into": [f"rs{m}" for m in merged["merged_into"]]}
    psd = payload.get("primary_snapshot_data")
    if not psd:
        return {**base, "status": "no_data"}
    placements = _assembly_placements(psd)
    mane_ids = set(payload.get("mane_select_ids") or [])
    primary = next((p for p in psd.get("placements_with_allele") or []
                    if p.get("is_ptlp")), None)
    alleles = []
    if primary:
        annotations = psd.get("allele_annotations") or []
        for i, entry in enumerate(primary.get("alleles") or []):
            spdi = entry.get("allele", {}).get("spdi") or {}
            if not spdi or spdi.get("deleted_sequence") == \
                    spdi.get("inserted_sequence"):
                continue
            annotation = annotations[i] if i < len(annotations) else {}
            alleles.append({
                "allele": spdi.get("inserted_sequence"),
                "ref": spdi.get("deleted_sequence"),
                "spdi": _spdi_str(spdi),
                "hgvs": entry.get("hgvs"),
                "frequencies": _frequencies(annotation),
                "clinvar": _clinvar(annotation),
                "genes": _genes(annotation, mane_ids),
            })
    return {
        **base,
        "status": "live",
        "variant_type": psd.get("variant_type"),
        "mane_select_ids": sorted(mane_ids),
        "placements": placements,
        "alleles": alleles,
    }
