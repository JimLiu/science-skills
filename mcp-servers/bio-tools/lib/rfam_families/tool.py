"""High-level tool surface mirroring the 9 tooluniverse/rfam MCP methods."""
from __future__ import annotations

import re
import urllib.parse

from .client import RfamClient, NotFound
from .records import (
    family_record, parse_regions, parse_cm_header,
    parse_stockholm_seq_names, parse_fasta_seq_names,
    sort_structure_mapping, sha256_text,
)

_ACC_RE = re.compile(r"^RF\d{5}$")


def _seg(value) -> str:
    """Encode one tool argument as a single URL path segment."""
    return urllib.parse.quote(str(value), safe="")


class RfamFamilies:
    """Structured access to Rfam family-level data.

    All methods accept either an Rfam accession (``RF00005``) or a family id
    (``tRNA``) — the upstream /family routes resolve both.
    """

    def __init__(self, client: RfamClient | None = None):
        self.client = client or RfamClient()


    def get_family(self, family: str) -> dict:
        """Family metadata record (flattened from the /family JSON)."""
        payload = self.client.get_json(f"/family/{_seg(family)}")
        rec = family_record(payload)
        rec["raw"] = payload.get("rfam", payload)
        return rec


    def get_alignment(self, family: str, fmt: str = "stockholm") -> dict:
        """Seed alignment. ``fmt`` is 'stockholm' or 'fasta' (aligned gapped).

        Returns the raw text plus sequence names, count, and sha256 so agents
        can verify integrity without re-downloading.
        """
        if fmt == "stockholm":
            text = self.client.get_text(f"/family/{_seg(family)}/alignment")
            names = parse_stockholm_seq_names(text)
        elif fmt == "fasta":
            text = self.client.get_text(f"/family/{_seg(family)}/alignment/fasta")
            names = parse_fasta_seq_names(text)
        else:
            raise ValueError(f"unsupported alignment format: {fmt!r}")
        return {"family": family, "format": fmt, "num_sequences": len(names),
                "sequence_names": names, "sha256": sha256_text(text),
                "alignment": text}


    def get_cm(self, family: str) -> dict:
        """Infernal covariance model file + parsed header fields."""
        text = self.client.get_text(f"/family/{_seg(family)}/cm")
        header = parse_cm_header(text)
        return {"family": family, "header": header, "size_bytes": len(text.encode()),
                "sha256": sha256_text(text), "cm": text}


    def get_tree(self, family: str) -> dict:
        """Seed phylogenetic tree (NHX/Newick text)."""
        text = self.client.get_text(f"/family/{_seg(family)}/tree")
        n_leaves = text.count(":") and len(re.findall(r"[(,]\s*[^(),:]+:", text))
        return {"family": family, "num_leaf_labels": n_leaves,
                "sha256": sha256_text(text), "tree": text}


    def sequence_regions(self, family: str) -> dict:
        """All full-region hits for the family (TSV route, parsed).

        Note: upstream 403s this route for very large families (e.g. RF00005);
        the error is surfaced as RfamApiError.
        """
        text = self.client.get_text(f"/family/{_seg(family)}/regions")
        parsed = parse_regions(text)
        return {"family": family,
                "declared_count": parsed["declared_count"],
                "num_regions": len(parsed["regions"]),
                "regions": parsed["regions"]}


    def structure_mapping(self, family: str) -> dict:
        """PDB residue mappings for the family, deterministically sorted."""
        payload = self.client.get_json(f"/family/{_seg(family)}/structures")
        rows = sort_structure_mapping(payload.get("mapping", []))
        pdb_ids = sorted({str(r.get("pdb_id")) for r in rows})
        return {"family": family, "num_mappings": len(rows),
                "num_pdb_ids": len(pdb_ids), "pdb_ids": pdb_ids,
                "mapping": rows}


    def acc_to_id(self, accession: str) -> str:
        """RFxxxxx -> family id (e.g. RF00005 -> 'tRNA')."""
        return self.client.get_text(f"/family/{_seg(accession)}/id").strip()

    def id_to_acc(self, family_id: str) -> str:
        """family id -> RFxxxxx accession (e.g. 'tRNA' -> RF00005)."""
        text = self.client.get_text(f"/family/{_seg(family_id)}/acc").strip()
        if not _ACC_RE.match(text):
            raise NotFound(404, f"/family/{family_id}/acc",
                           f"no accession resolved for {family_id!r}")
        return text


    def search_sequence(self, sequence: str, max_wait_s: float = 300.0,
                        poll_interval_s: float = 5.0) -> dict:
        """Async single-sequence cmscan search: submit, poll, return hits.

        Returns ``{"hits": {family_id: [hit, ...]}, "num_hits": n,
        "families": [...]}.`` Raises SearchUnavailable when the upstream job
        backend is down (observed 2026-06-08), TimeoutError past max_wait_s.
        """
        sub = self.client.submit_search(sequence)
        result_url = sub.get("resultURL")
        if not result_url:
            raise RuntimeError(f"submission response missing resultURL: {sub}")
        res = self.client.poll_search(result_url, max_wait_s=max_wait_s,
                                      poll_interval_s=poll_interval_s)
        hits = res.get("hits", {}) or {}
        families = sorted(hits.keys())
        num = sum(len(v) for v in hits.values())
        return {"job_id": sub.get("jobId"), "num_hits": num,
                "families": families, "hits": hits,
                "search_sequence": res.get("searchSequence")}
