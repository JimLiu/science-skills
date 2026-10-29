"""High-level Ensembl REST operations used by the mcp-genomes server.

Each method wraps exactly one REST endpoint and returns the parsed upstream
JSON (lists stay lists); summarising/capping is the tier-2 server's job.
The ``/homology/symbol`` route is deliberately NOT used — it stalls for
tens of seconds upstream — symbols are resolved to stable IDs via
``lookup_symbol`` first and homology always goes through ``/homology/id``.
"""
from __future__ import annotations

from urllib.parse import quote

from .client import EnsemblApiError, EnsemblClient


def _seg(value) -> str:
    """Percent-encode one URL path segment (model-controlled ids/regions
    must not be able to splice extra path components into the route)."""
    return quote(str(value), safe="")


def _is_not_found(exc: EnsemblApiError) -> bool:
    if exc.status != 400:
        return False
    msg = str(exc).lower()
    return "no valid lookup found" in msg or "not found" in msg


class EnsemblRest:
    """Thin endpoint layer over :class:`EnsemblClient`."""

    def __init__(self, client: EnsemblClient | None = None):
        self.client = client or EnsemblClient()


    def lookup_symbol(self, species: str, symbol: str, expand: bool = False,
                      max_retries: int | None = None):
        """GET /lookup/symbol/:species/:symbol -> record dict | None."""
        try:
            return self.client.get(
                f"/lookup/symbol/{_seg(species)}/{_seg(symbol)}",
                params={"expand": int(expand)}, max_retries=max_retries)
        except EnsemblApiError as exc:
            if _is_not_found(exc):
                return None
            raise

    def lookup_id(self, stable_id: str, expand: bool = False,
                  max_retries: int | None = None):
        """GET /lookup/id/:id -> record dict | None."""
        try:
            return self.client.get(f"/lookup/id/{_seg(stable_id)}",
                                   params={"expand": int(expand)},
                                   max_retries=max_retries)
        except EnsemblApiError as exc:
            if _is_not_found(exc):
                return None
            raise


    def xrefs_id(self, stable_id: str, external_db: str | None = None):
        """GET /xrefs/id/:id -> list of cross-reference dicts ([] for
        unknown IDs — upstream 400s "ID ... not found", mapped like the
        lookup/sequence siblings)."""
        params = {}
        if external_db:
            params["external_db"] = external_db
        try:
            return self.client.get(f"/xrefs/id/{_seg(stable_id)}",
                                   params=params)
        except EnsemblApiError as exc:
            if _is_not_found(exc):
                return []
            raise


    def vep_id(self, species: str, variant_id: str):
        """GET /vep/:species/id/:id -> list of VEP result dicts."""
        return self.client.get(f"/vep/{_seg(species)}/id/{_seg(variant_id)}")

    def vep_region(self, species: str, region: str, allele: str):
        """GET /vep/:species/region/:region/:allele -> list of VEP dicts."""
        return self.client.get(f"/vep/{_seg(species)}/region/{_seg(region)}/{_seg(allele)}")


    def homology_id(self, species: str, gene_id: str, homology_type: str,
                    target_species: str | None = None,
                    target_taxon: int | None = None):
        """GET /homology/id/:species/:id (condensed, no alignments) ->
        list of homology dicts."""
        params: dict = {"format": "condensed", "type": homology_type}
        if target_species:
            params["target_species"] = target_species
        if target_taxon:
            params["target_taxon"] = target_taxon
        try:
            data = self.client.get(
                f"/homology/id/{_seg(species)}/{_seg(gene_id)}",
                params=params).get("data") or []
        except EnsemblApiError as exc:




            if _is_not_found(exc):
                return []
            raise
        return data[0].get("homologies", []) if data else []


    def sequence_id(self, stable_id: str, seq_type: str = "genomic"):
        """GET /sequence/id/:id -> {id, seq, molecule, ...} | None."""
        try:
            return self.client.get(f"/sequence/id/{_seg(stable_id)}",
                                   params={"type": seq_type})
        except EnsemblApiError as exc:
            if _is_not_found(exc):
                return None
            raise

    def sequence_region(self, species: str, region: str):
        """GET /sequence/region/:species/:region -> {id, seq, ...}."""
        return self.client.get(f"/sequence/region/{_seg(species)}/{_seg(region)}")


    def overlap_region(self, species: str, region: str, feature: str):
        """GET /overlap/region/:species/:region -> complete feature list
        (Ensembl itself rejects spans > 5 Mb with HTTP 400)."""
        return self.client.get(f"/overlap/region/{_seg(species)}/{_seg(region)}",
                               params={"feature": feature})
