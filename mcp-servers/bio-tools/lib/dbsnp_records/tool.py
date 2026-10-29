"""High-level dbSNP retrieval: batch rsID lookup + region rsID listing.

Honesty rules: region listings carry esearch's own ``count`` total plus a
``truncated`` flag; batch lookups report ``not_found`` explicitly and
``not_processed`` when the wall-clock deadline cuts the batch short (MCP
transport budget — one Variation Services request per rsID).
"""
from __future__ import annotations

import re
import time

from .client import DbsnpClient
from .records import distill_refsnp

MAX_BATCH_RSIDS = 20
MAX_REGION_RSIDS = 1000
MAX_REGION_SPAN = 1_000_000
_RSID_RE = re.compile(r"^rs(\d+)$", re.IGNORECASE)
_CHROMS = {str(n) for n in range(1, 23)} | {"X", "Y", "MT"}


def _rs_number(rsid: str) -> int:
    m = _RSID_RE.match(rsid.strip())
    if not m:
        raise ValueError(f"not an rsID: {rsid!r} (expected e.g. rs7412)")
    return int(m.group(1))


class DbsnpRecords:
    """RefSNP orchestration over a paced client."""

    def __init__(self, client: DbsnpClient) -> None:
        self.client = client

    def get_rsids(self, rsids: list[str], deadline_s: float = 40.0) -> dict:
        """Fetch + distill RefSNP records for a batch of rsIDs.

        One Variation Services request per rsID (paced). ``deadline_s``
        bounds total wall-clock: rsIDs not reached in time are returned in
        ``not_processed`` rather than blowing the transport budget.
        """
        cleaned = list(dict.fromkeys(r.strip() for r in rsids if r.strip()))
        if not cleaned:
            raise ValueError("no rsIDs given")
        if len(cleaned) > MAX_BATCH_RSIDS:
            raise ValueError(f"too many rsIDs ({len(cleaned)}); max "
                             f"{MAX_BATCH_RSIDS} per call")
        numbers = [(_rs_number(r), r) for r in cleaned]
        t0 = time.monotonic()
        records, not_found, not_processed = [], [], []
        for i, (num, raw) in enumerate(numbers):
            if time.monotonic() - t0 > deadline_s:
                not_processed.extend(r for _, r in numbers[i:])
                break
            payload = self.client.get_refsnp(num)
            if payload is None:
                not_found.append(f"rs{num}")
                continue
            records.append(distill_refsnp(payload))
        return {
            "n_requested": len(cleaned),
            "records": records,
            "not_found": not_found,
            "not_processed": not_processed,
        }

    def search_by_region(self, chrom: str, start: int, stop: int,
                         assembly: str = "GRCh38",
                         max_rsids: int = MAX_REGION_RSIDS) -> dict:
        """List rsIDs in a chromosome window via esearch db=snp.

        ``assembly`` picks the positional index: GRCh38 -> ``[CPOS]``,
        GRCh37 -> ``[CPOS_GRCH37]``. Returns esearch's own total plus a
        ``truncated`` flag when the rsID list is a capped prefix.
        """
        chrom = str(chrom).strip().upper().removeprefix("CHR")
        if chrom not in _CHROMS:
            raise ValueError(f"bad chromosome {chrom!r} (1-22, X, Y, MT)")
        start, stop = int(start), int(stop)
        if not (0 < start <= stop):
            raise ValueError("need 0 < start <= stop")
        if stop - start > MAX_REGION_SPAN:
            raise ValueError(f"region span {stop - start:,} bp exceeds "
                             f"{MAX_REGION_SPAN:,} bp — split into windows")
        field = {"GRCH38": "CPOS", "GRCH37": "CPOS_GRCH37"}.get(
            assembly.upper())
        if field is None:
            raise ValueError(f"assembly must be GRCh38 or GRCh37, "
                             f"got {assembly!r}")
        max_rsids = max(1, min(int(max_rsids), MAX_REGION_RSIDS))
        term = (f"{chrom}[CHR] AND {start}:{stop}[{field}] "
                "AND homo sapiens[ORGN]")
        result = self.client.esearch_snp(term, retmax=max_rsids)
        total = int(result.get("count", 0))
        rsids = [f"rs{u}" for u in result.get("idlist") or []]
        return {
            "chrom": chrom, "start": start, "stop": stop,
            "assembly": "GRCh37" if field == "CPOS_GRCH37" else "GRCh38",
            "term": term,
            "total": total,
            "n_returned": len(rsids),
            "truncated": total > len(rsids),
            "rsids": rsids,
        }
