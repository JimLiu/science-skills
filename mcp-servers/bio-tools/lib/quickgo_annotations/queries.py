"""Query specification for annotation retrieval (gene products + aspect/evidence filters)."""
from __future__ import annotations

from dataclasses import dataclass, field


ASPECTS = ("biological_process", "molecular_function", "cellular_component")





EVIDENCE_PRESETS: dict[str, dict] = {

    "experimental_manual": {"evidenceCode": "ECO:0000269", "evidenceCodeUsage": "descendants"},

    "automatic_iea": {"evidenceCode": "ECO:0000501", "evidenceCodeUsage": "descendants"},
}


@dataclass(frozen=True)
class AnnotationQuery:
    """A declarative spec for one annotation retrieval.

    gene_product_id : UniProt accession, e.g. "P04637" (the "UniProtKB:" prefix
        is added automatically).
    aspect : one of ASPECTS, or None for all aspects.
    evidence : a preset name from EVIDENCE_PRESETS, an explicit ECO code
        (e.g. "ECO:0000314"), or None for all evidence.
    evidence_usage : "descendants" or "exact"; only used when `evidence` is a
        raw ECO code (presets carry their own usage).
    taxon_id : optional NCBI taxon restriction (int).
    """
    gene_product_id: str
    aspect: str | None = None
    evidence: str | None = None
    evidence_usage: str = "descendants"
    taxon_id: int | None = None

    def __post_init__(self) -> None:
        if self.aspect is not None and self.aspect not in ASPECTS:
            raise ValueError(f"aspect must be one of {ASPECTS} or None, got {self.aspect!r}")
        if self.evidence is not None and self.evidence not in EVIDENCE_PRESETS \
                and not self.evidence.startswith("ECO:"):
            raise ValueError(
                f"evidence must be a preset {sorted(EVIDENCE_PRESETS)}, an ECO code "
                f"('ECO:...'), or None; got {self.evidence!r}. "
                "(Three-letter GO evidence codes like 'IDA' are NOT accepted because the "
                "QuickGO API silently ignores its goEvidence parameter.)")

    @property
    def accession(self) -> str:
        return self.gene_product_id.split(":", 1)[-1]

    def to_params(self) -> dict:
        """Filter parameters shared by /annotation/search and /annotation/downloadSearch."""
        params: dict = {"geneProductId": f"UniProtKB:{self.accession}"}
        if self.aspect is not None:
            params["aspect"] = self.aspect
        if self.evidence is not None:
            if self.evidence in EVIDENCE_PRESETS:
                params.update(EVIDENCE_PRESETS[self.evidence])
            else:
                params["evidenceCode"] = self.evidence
                params["evidenceCodeUsage"] = self.evidence_usage
        if self.taxon_id is not None:
            params["taxonId"] = str(self.taxon_id)
            params["taxonUsage"] = "exact"
        return params

    def label(self) -> str:
        parts = [self.accession,
                 self.aspect or "all_aspects",
                 self.evidence or "all_evidence"]
        return "|".join(parts)
