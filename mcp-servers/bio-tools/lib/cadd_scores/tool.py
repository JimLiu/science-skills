"""High-level CADD score retrieval: variant / position / range queries.

Mirrors the three tooluniverse/cadd MCP methods with two corrections the
MCP shape lacks:

* the CADD version string is ALWAYS explicit and validated -- it embeds the
  genome build (``GRCh38-v1.7``); the upstream API silently returns ``[]``
  (HTTP 200) for unknown version strings such as a bare ``v1.7``;
* the 100 bp range cap is enforced client-side -- upstream does NOT reject
  oversized ranges, it just serves them, so an unguarded caller can issue
  arbitrarily heavy queries.
"""
from __future__ import annotations

import re

from .client import CaddClient
from .records import (normalize_position_response, normalize_range_response,
                      sort_records)




KNOWN_VERSIONS = ("GRCh38-v1.7", "GRCh38-v1.6", "GRCh37-v1.7", "GRCh37-v1.6")
DEFAULT_VERSION = "GRCh38-v1.7"
VERSION_RE = re.compile(r"^GRCh3[78]-v\d+\.\d+$")
MAX_RANGE_BP = 100
VALID_CHROMS = {str(i) for i in range(1, 23)} | {"X", "Y"}
VALID_BASES = {"A", "C", "G", "T"}


class CaddEmptyResult(RuntimeError):
    """The API returned an empty list (HTTP 200, no rows).

    Causes observed live: position outside the scored genome, a contig the
    release does not score, or an unknown version string. The upstream API
    does not distinguish these.
    """


class CaddRefMismatch(RuntimeError):
    """The reference allele at the queried position differs from ``ref``."""

    def __init__(self, chrom, pos, expected_ref, actual_ref, version):
        super().__init__(
            f"{version} {chrom}:{pos}: query ref={expected_ref!r} but the "
            f"reference allele is {actual_ref!r} (wrong build or typo?)")
        self.actual_ref = actual_ref


class CaddAltNotFound(RuntimeError):
    """No row for the requested alt allele at the position."""


def _validate_version(version: str) -> str:
    if not VERSION_RE.match(version):
        raise ValueError(
            f"invalid CADD version {version!r}: must look like 'GRCh38-v1.7' "
            f"(build prefix REQUIRED -- a bare 'v1.7' silently returns no "
            f"rows upstream). Known-good: {KNOWN_VERSIONS}")
    return version


def _validate_chrom(chrom) -> str:
    c = str(chrom).removeprefix("chr").upper()
    if c == "M":
        c = "MT"
    if c not in VALID_CHROMS:
        raise ValueError(f"invalid chromosome {chrom!r}: expected 1-22, X, Y "
                         f"(CADD scores nuclear SNVs only)")
    return c


def _validate_pos(pos) -> int:
    p = int(pos)
    if p < 1:
        raise ValueError(f"position must be >= 1, got {pos!r}")
    return p


def _validate_base(b, name: str) -> str:
    s = str(b).upper()
    if s not in VALID_BASES:
        raise ValueError(f"{name} must be one of A/C/G/T, got {b!r}")
    return s


class CaddScores:
    """CADD deleteriousness scores from the official REST API.

    Parameters
    ----------
    client : CaddClient, optional
        Inject a configured client (throttle, retries, instrumentation).
        A default polite client (0.5 s min interval) is created if omitted.
    """

    def __init__(self, client: CaddClient | None = None):
        self.client = client or CaddClient()



    def position_scores(self, chrom, pos, version: str = DEFAULT_VERSION) -> dict:
        """All possible substitutions at one genomic position.

        Returns ``{"query": {...}, "records": [<=3 score records]}``.
        Raises CaddEmptyResult if the API returns no rows.
        """
        version = _validate_version(version)
        chrom = _validate_chrom(chrom)
        pos = _validate_pos(pos)
        payload = self.client.get_json(f"{version}/{chrom}:{pos}")
        records = normalize_position_response(payload)
        if not records:
            raise CaddEmptyResult(
                f"no CADD rows for {version} {chrom}:{pos} (position outside "
                f"scored genome, unscored contig, or unknown version)")
        return {"query": {"type": "position", "version": version,
                          "chrom": chrom, "pos": pos},
                "records": records}

    def variant_score(self, chrom, pos, ref, alt, version: str = DEFAULT_VERSION) -> dict:
        """Score for one specific SNV (ref>alt at chrom:pos).

        Implemented as a position query filtered to the requested alt, with
        the reference allele verified against the query's ``ref`` (catches
        wrong-build coordinates, which otherwise return a syntactically
        valid but scientifically wrong score).
        """
        ref = _validate_base(ref, "ref")
        alt = _validate_base(alt, "alt")
        if ref == alt:
            raise ValueError("ref and alt must differ")
        res = self.position_scores(chrom, pos, version=version)
        records = res["records"]
        actual_ref = records[0]["ref"]
        if actual_ref != ref:
            raise CaddRefMismatch(res["query"]["chrom"], res["query"]["pos"],
                                  ref, actual_ref, version)
        for rec in records:
            if rec["alt"] == alt:
                return {"query": {"type": "variant", "version": version,
                                  "chrom": res["query"]["chrom"],
                                  "pos": res["query"]["pos"],
                                  "ref": ref, "alt": alt},
                        "record": rec}
        raise CaddAltNotFound(
            f"no row for alt={alt!r} at {version} "
            f"{res['query']['chrom']}:{res['query']['pos']} "
            f"(alts present: {[r['alt'] for r in records]})")

    def range_scores(self, chrom, start, end, version: str = DEFAULT_VERSION) -> dict:
        """Scores for every SNV in [start, end] (inclusive; <= 100 bp).

        The 100 bp cap is enforced HERE: upstream serves oversized ranges
        without complaint, so the cap is a client-side contract matching
        the mirrored MCP method.
        """
        version = _validate_version(version)
        chrom = _validate_chrom(chrom)
        start = _validate_pos(start)
        end = _validate_pos(end)
        if end < start:
            raise ValueError(f"end ({end}) must be >= start ({start})")
        span = end - start + 1
        if span > MAX_RANGE_BP:
            raise ValueError(f"range spans {span} bp; maximum is {MAX_RANGE_BP} bp "
                             f"(split into consecutive windows)")
        payload = self.client.get_json(f"{version}/{chrom}:{start}-{end}")
        records = normalize_range_response(payload)
        if not records:
            raise CaddEmptyResult(
                f"no CADD rows for {version} {chrom}:{start}-{end}")
        positions = sorted({r["pos"] for r in records})
        return {"query": {"type": "range", "version": version, "chrom": chrom,
                          "start": start, "end": end, "span_bp": span},
                "n_records": len(records),
                "n_positions_scored": len(positions),
                "records": sort_records(records)}
