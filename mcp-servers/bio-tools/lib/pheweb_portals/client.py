"""pheweb-portals: registry-structured retrieval from public PheWeb PheWAS
portals (FinnGen, BioBank Japan).

PheWeb instances share route conventions but differ in version, genome build
and which JSON endpoints they expose, so each instance is a registry row
declaring its base URL, build and supported capabilities. Adding an instance
is one registry entry; tools refuse capabilities an instance doesn't have
instead of 404-guessing.

Quirks handled here:
  * the client identifies honestly (bio-tools-pheweb UA). An earlier
    vendored version spoofed a browser UA citing "FinnGen 502s non-browser
    UAs" — that claim did NOT reproduce (2026-06-12 probes: honest,
    python-requests, and empty UAs all 200) and the spoof was removed.
    If FinnGen ever does block, the
    fix is an operator conversation, not a spoof;
  * pheweb.jp (BBJ) is GRCh37 while FinnGen is GRCh38 — the registry pins
    the build and every tool output echoes it;
  * responses carry no totals; variant/gene listings return every phenotype
    row at once (FinnGen: ~2470), so tools cap with an explicit flag.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

from mcp_servers_common.ratelimit import retry_after_seconds








USER_AGENT = "bio-tools-pheweb/0.1 (anthropic-experimental/bio-tools)"




INSTANCES: dict[str, dict[str, Any]] = {
    "finngen": {
        "label": "FinnGen R12",
        "base_url": "https://r12.finngen.fi",
        "genome_build": "GRCh38",
        "capabilities": ("variant", "gene", "phenotypes", "autocomplete"),
        "notes": "~500k Finnish biobank participants, 2470 endpoints; "
                 "variant IDs are chrom-pos-ref-alt on GRCh38.",
    },
    "bbj": {
        "label": "BioBank Japan (pheweb.jp)",
        "base_url": "https://pheweb.jp",
        "genome_build": "GRCh37",
        "capabilities": ("variant", "autocomplete"),
        "notes": "~260 Japanese GWAS; variant IDs are chrom-pos-ref-alt on "
                 "GRCh37/hg19 (NOT GRCh38). No gene or full-phenotype-list "
                 "JSON endpoints.",
    },
}


class PhewebApiError(RuntimeError):
    """Unrecoverable API or transport error."""


class NotFound(PhewebApiError):
    """HTTP 404 — unknown variant/gene/route on this instance."""


class UnsupportedCapability(PhewebApiError):
    """The registry says this instance does not expose that endpoint."""


@dataclass
class TransportStats:
    requests: int = 0
    bytes_downloaded: int = 0


class PhewebClient:
    """Throttled, bounded-retry GET-only client for one PheWeb instance."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str, min_interval_s: float = 0.5,
                 timeout_s: float = 30.0, max_retries: int = 2,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": USER_AGENT})
        self.stats = TransportStats()
        self._last_request_t = 0.0

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_request_t
        if dt < self.min_interval_s:
            time.sleep(self.min_interval_s - dt)

    def get_json(self, path: str, params: dict | None = None):
        url = self.base_url + path
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_s)
            except requests.RequestException as exc:
                self._last_request_t = time.monotonic()
                last_err = exc
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 4))
                continue
            self._last_request_t = time.monotonic()
            self.stats.requests += 1
            self.stats.bytes_downloaded += len(resp.content)
            if resp.status_code == 404:
                raise NotFound(url)
            if resp.status_code in self.RETRY_STATUSES:
                last_err = PhewebApiError(f"HTTP {resp.status_code} for {url}")
                delay = retry_after_seconds(resp.headers.get("Retry-After", ""),
                                            min(2 ** attempt, 4), cap=5.0)
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                continue
            if resp.status_code != 200:
                raise PhewebApiError(
                    f"HTTP {resp.status_code} for {url}: {resp.text[:300]}")
            try:
                return resp.json()
            except json.JSONDecodeError as exc:
                raise PhewebApiError(
                    f"non-JSON body from {url} (content-type "
                    f"{resp.headers.get('content-type')!r})") from exc
        raise PhewebApiError(
            f"giving up on {url} after {self.max_retries} attempts: {last_err!r}")


def _seg(value: str) -> str:
    """Percent-encode one REST path segment (model-supplied identifiers must
    not traverse/inject into the request path)."""
    return urllib.parse.quote(str(value), safe="")


def normalize_variant_id(variant: str) -> str:
    """Accept ``chrom-pos-ref-alt``, ``chrom:pos:ref:alt`` or
    ``chrom_pos_ref_alt`` (with or without a ``chr`` prefix) and return the
    PheWeb route form ``chrom-pos-ref-alt``."""
    v = variant.strip().replace(":", "-").replace("_", "-").replace("/", "-")
    if v.lower().startswith("chr"):
        v = v[3:]
    parts = v.split("-")
    if len(parts) != 4 or not parts[1].isdigit():
        raise ValueError(
            f"variant {variant!r} is not chrom-pos-ref-alt (e.g. "
            f"19-44908822-C-T)")
    return "-".join(parts)


class PhewebPortals:
    """Registry-aware multi-instance PheWeb access."""

    def __init__(self, clients: dict[str, PhewebClient] | None = None):
        self._clients = clients or {}

    @staticmethod
    def _instance_row(instance: str) -> dict[str, Any]:
        """Registry lookup — the single owner of instance-name validation."""
        try:
            return INSTANCES[instance]
        except KeyError:
            raise KeyError(
                f"unknown PheWeb instance {instance!r}; known: "
                f"{sorted(INSTANCES)}") from None

    def _client(self, instance: str) -> PhewebClient:
        row = self._instance_row(instance)
        if instance not in self._clients:
            self._clients[instance] = PhewebClient(row["base_url"])
        return self._clients[instance]

    def _require(self, instance: str, capability: str) -> None:
        row = self._instance_row(instance)
        if capability not in row["capabilities"]:
            raise UnsupportedCapability(
                f"instance {instance!r} ({row['label']}) has "
                f"no {capability!r} endpoint; capabilities: "
                f"{list(row['capabilities'])}")



    def variant_phenos(self, instance: str, variant: str) -> dict[str, Any]:
        """PheWAS rows for one variant. Returns {variant_meta, rows} with
        rows normalized across instance versions."""
        vid = normalize_variant_id(variant)
        self._require(instance, "variant")
        payload = self._client(instance).get_json(
            f"/api/variant/{_seg(vid)}")


        if "results" in payload:
            rows = payload.get("results") or []
            var = payload.get("variant") or {}
            annotation = var.get("annotation") or {}
            meta = {"chrom": str(var.get("chr", "")), "pos": var.get("pos"),
                    "ref": var.get("ref"), "alt": var.get("alt"),
                    "rsids": annotation.get("rsids"),
                    "gnomad": _lean_gnomad(annotation.get("gnomad")),
                    "nearest_genes": None}
        else:
            rows = payload.get("phenos") or []
            meta = {"chrom": str(payload.get("chrom", "")),
                    "pos": payload.get("pos"), "ref": payload.get("ref"),
                    "alt": payload.get("alt"),
                    "rsids": payload.get("rsids") or None, "gnomad": None,
                    "nearest_genes": payload.get("nearest_genes")}
        return {"variant_meta": meta,
                "rows": [_lean_assoc_row(r) for r in rows]}

    def gene_phenos(self, instance: str, gene: str) -> list[dict[str, Any]]:
        """Best-association-per-phenotype rows for a gene region."""
        self._require(instance, "gene")
        payload = self._client(instance).get_json(
            f"/api/gene_phenos/{_seg(gene.strip())}")
        rows = payload.get("phenotypes") or [] if isinstance(payload, dict) else payload
        out = []
        for row in rows:
            lean = _lean_assoc_row(row.get("assoc") or {})
            var = row.get("variant") or {}
            lean["variant"] = {
                "chrom": str(var.get("chr", "")), "pos": var.get("pos"),
                "ref": var.get("ref"), "alt": var.get("alt"),
                "varid": var.get("varid"),
                "rsids": (var.get("annotation") or {}).get("rsids"),
            }
            out.append(lean)
        return out

    def phenotypes(self, instance: str) -> list[dict[str, Any]]:
        """Complete phenotype catalogue of an instance."""
        self._require(instance, "phenotypes")
        rows = self._client(instance).get_json("/api/phenos")
        return [{"phenocode": r.get("phenocode"),
                 "phenostring": r.get("phenostring"),
                 "category": r.get("category"),
                 "num_cases": r.get("num_cases"),
                 "num_controls": r.get("num_controls"),
                 "num_gw_significant": r.get("num_gw_significant")}
                for r in rows]

    def autocomplete(self, instance: str, query: str) -> list[dict[str, Any]]:
        """Phenotype/entity autocomplete search."""
        self._require(instance, "autocomplete")
        rows = self._client(instance).get_json("/api/autocomplete",
                                               params={"query": query})
        return [{"display": r.get("display"),
                 "phenocode": r.get("pheno") or r.get("value"),
                 "url": r.get("url")}
                for r in rows]


def _lean_gnomad(gnomad: dict | None) -> dict | None:
    if not isinstance(gnomad, dict):
        return gnomad
    keep = ("AF", "AF_fin", "AF_nfe", "AF_popmax", "filters", "rsid")
    lean = {k: gnomad[k] for k in keep if k in gnomad}
    return lean or None


def _lean_assoc_row(r: dict) -> dict[str, Any]:
    """Normalize FinnGen-fork and classic-pheweb association rows."""
    return {
        "phenocode": r.get("phenocode"),
        "phenostring": r.get("phenostring"),
        "category": r.get("category"),
        "pval": r.get("pval"),
        "mlogp": r.get("mlogp"),
        "beta": r.get("beta"),
        "sebeta": r.get("sebeta"),
        "af": r.get("af"),
        "maf": r.get("maf"),
        "maf_case": r.get("maf_case"),
        "maf_control": r.get("maf_control"),
        "n_cases": r.get("n_case", r.get("num_cases")),
        "n_controls": r.get("n_control", r.get("num_controls")),
        "n_samples": r.get("n_sample", r.get("num_samples")),
    }
