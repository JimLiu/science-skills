"""mcp-genomes server — genome annotation retrieval over Ensembl + UCSC.

Tier-2 domain server (net-new; no tooluniverse twin). Retrieval is done by
the fleet packages ``ensembl-rest`` (rest.ensembl.org, 1-based inclusive
coordinates, GRCh38 for human) and ``ucsc-tracks`` (api.genome.ucsc.edu,
0-based half-open coordinates, ``chr``-prefixed names); this layer is
marshalling, summarisation and honest capping only.

Listing honesty: Ensembl endpoints return complete result sets (the API
itself rejects oversized regions), so ``n_total`` is the true count and
caps are flagged via ``*_truncated``. UCSC truncation flags come from the
API's own ``itemsReturned``/``maxItemsLimit`` fields.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)

mcp = FastMCP("mcp-genomes")





_STABLE_ID_RE = re.compile(r"^(ENS[A-Z]*[GTPER]\d{6,}|LRG_\d+)",
                           re.IGNORECASE)


DEFAULT_SPECIES = "homo_sapiens"
DEFAULT_GENOME = "hg38"


_IMPACT_RANK = {"HIGH": 0, "MODERATE": 1, "LOW": 2, "MODIFIER": 3}



_MAX_CONSERVATION_SPAN = 100_000



_TFBS_TRACKS = {"hg38": "encRegTfbsClustered",
                "hg19": "wgEncodeRegTfbsClusteredV3"}




@lru_cache(maxsize=1)
def _ensembl():
    from ensembl_rest import EnsemblRest
    return EnsemblRest()


@lru_cache(maxsize=1)
def _ucsc():
    from ucsc_tracks import UcscClient
    return UcscClient()


def _cap_text_payload(result: dict, field: str, max_bytes: int) -> dict:
    """Omit a huge text field instead of blowing the MCP transport limit.

    Metadata/sha256/length always survive; the caller can re-request with
    a larger max_bytes or fetch from the upstream site directly.
    """
    text = result.get(field)
    if isinstance(text, str) and len(text.encode()) > max_bytes:
        size = len(text.encode())
        result = dict(result)
        del result[field]
        result[f"{field}_omitted"] = (
            f"{field} is {size} bytes > max_bytes={max_bytes}; metadata, "
            f"length and sha256 are included — re-call with a larger "
            f"max_bytes to get the full text")
    return result


def _require_cap(n: int, param: str) -> int:
    """Output caps must be >= 1: a negative cap silently tail-drops rows
    (Python slice) or means 'no limit' upstream (UCSC maxItemsOutput=-1),
    and UCSC rejects 0 outright."""
    n = int(n)
    if n < 1:
        raise ValueError(f"{param} must be >= 1 (got {n})")
    return n


def _resolve_gene_id(symbol: str, species: str) -> dict:
    """Symbol -> /lookup/symbol record (no retries: the caller chains a
    second upstream request and must not stack retry budgets)."""
    rec = _ensembl().lookup_symbol(species, symbol, max_retries=0)
    if rec is None:
        raise ValueError(f"no Ensembl gene found for symbol {symbol!r} "
                         f"in species {species!r}")
    return rec




@mcp.tool(annotations=READ_ONLY)
def ensembl_lookup(query: str, species: str = DEFAULT_SPECIES,
                   expand: bool = False) -> dict:
    """Look up an Ensembl gene/transcript/protein by stable ID or a gene
    by symbol; returns the core annotation record (location, biotype,
    canonical transcript, description).

    Args:
        query: Ensembl stable ID (``ENSG00000157764``, ``ENST...``,
            ``ENSP...`` — versioned IDs accepted) or a gene symbol/alias
            (``BRAF``). True stable IDs (``ENS`` + optional species code
            + feature letter + a >= 6-digit block, or ``LRG_N``) are
            routed to the ID endpoint; everything else — including
            symbols that merely start with "ENS", like ``ENSA`` or
            ``ENSAP1`` — to the symbol endpoint.
        species: Ensembl species name for symbol lookups, e.g.
            ``homo_sapiens`` (default), ``mus_musculus``. Ignored for
            stable IDs (they are species-unique).
        expand: when true, include the child feature tree (a gene's
            transcripts, their exons/translation). Adds bulk; default off.

    Returns ``{found, query, species, record}``; ``record`` is null when
    nothing matches, else the upstream lookup dict — for a gene:
    ``{id, display_name, description, biotype, object_type,
    seq_region_name, start, end, strand, assembly_name (GRCh38 for
    human), canonical_transcript, version, ...}`` with 1-based inclusive
    coordinates.
    """
    q = query.strip()
    if _STABLE_ID_RE.match(q):
        rec = _ensembl().lookup_id(q, expand=expand)
    else:
        rec = _ensembl().lookup_symbol(species, q, expand=expand)
    return {"found": rec is not None, "query": q, "species": species,
            "record": rec}


@mcp.tool(annotations=READ_ONLY)
def ensembl_xrefs(stable_id: str, external_db: str | None = None) -> dict:
    """External cross-references of an Ensembl stable ID — the bridge from
    Ensembl gene/transcript IDs to HGNC, NCBI (EntrezGene), UniProt,
    OMIM, RefSeq, Expression Atlas and others.

    Args:
        stable_id: Ensembl stable ID (``ENSG00000157764``, ``ENST...``).
        external_db: optional exact upstream database-name filter, e.g.
            ``HGNC``, ``EntrezGene``, ``Uniprot_gn``, ``MIM_GENE``,
            ``RefSeq_mRNA``. Omit for all databases.

    Returns ``{stable_id, external_db, n_xrefs, xrefs}`` — the COMPLETE
    list (never truncated), sorted by (dbname, primary_id); each row:
    ``{dbname, db_display_name, primary_id, display_id, description,
    synonyms, info_type}``. Unknown IDs return ``n_xrefs: 0`` (the
    upstream's 400 "ID ... not found" is mapped to an empty list, like
    an ID with no xrefs).
    """
    rows = _ensembl().xrefs_id(stable_id, external_db=external_db)
    rows = sorted(rows, key=lambda r: (str(r.get("dbname", "")),
                                       str(r.get("primary_id", ""))))
    return {"stable_id": stable_id, "external_db": external_db,
            "n_xrefs": len(rows), "xrefs": rows}


def _summarize_vep_result(raw: dict, max_consequences: int) -> dict:
    tx = list(raw.get("transcript_consequences") or [])
    tx.sort(key=lambda t: (_IMPACT_RANK.get(t.get("impact"), 9),
                           t.get("gene_id") or "",
                           t.get("transcript_id") or ""))
    genes: dict[str, dict] = {}
    for t in tx:
        gid = t.get("gene_id") or "?"
        g = genes.setdefault(gid, {"gene_id": t.get("gene_id"),
                                   "gene_symbol": t.get("gene_symbol"),
                                   "worst_impact": t.get("impact"),
                                   "n_transcripts": 0})
        g["n_transcripts"] += 1
        if _IMPACT_RANK.get(t.get("impact"), 9) < \
                _IMPACT_RANK.get(g["worst_impact"], 9):
            g["worst_impact"] = t.get("impact")
    capped = tx[:max_consequences]
    coloc = []
    for cv in raw.get("colocated_variants") or []:
        coloc.append({k: cv.get(k) for k in
                      ("id", "allele_string", "clin_sig", "clin_sig_allele",
                       "somatic", "phenotype_or_disease", "start", "end")
                      if cv.get(k) is not None})
    return {
        "input": raw.get("input"),
        "assembly_name": raw.get("assembly_name"),
        "seq_region_name": raw.get("seq_region_name"),
        "start": raw.get("start"), "end": raw.get("end"),
        "strand": raw.get("strand"),
        "allele_string": raw.get("allele_string"),
        "most_severe_consequence": raw.get("most_severe_consequence"),
        "genes": sorted(genes.values(),
                        key=lambda g: (_IMPACT_RANK.get(g["worst_impact"], 9),
                                       g["gene_id"] or "")),
        "n_transcript_consequences": len(tx),
        "transcript_consequences_truncated": len(tx) > len(capped),
        "transcript_consequences": capped,
        "n_regulatory_feature_consequences":
            len(raw.get("regulatory_feature_consequences") or []),
        "n_motif_feature_consequences":
            len(raw.get("motif_feature_consequences") or []),
        "colocated_variants": coloc,
    }


@mcp.tool(annotations=READ_ONLY)
def ensembl_vep_variant(variant_id: str | None = None,
                        region: str | None = None,
                        allele: str | None = None,
                        species: str = DEFAULT_SPECIES,
                        max_consequences: int = 25) -> dict:
    """Predict variant consequences with Ensembl VEP — most-severe-first
    summary of the (often huge) per-transcript consequence list.

    Pass EITHER ``variant_id`` OR ``region`` + ``allele``:

    Args:
        variant_id: known-variant ID — dbSNP rsID (``rs7412``), COSMIC
            (``COSV52986153``) or HGMD ID.
        region: GRCh38 1-based inclusive region ``chrom:start-end``, e.g.
            ``7:140753336-140753336`` (SNV: start == end; insertion:
            start = end + 1). An explicit strand suffix ``:1``/``:-1`` is
            accepted (default forward).
        allele: variant allele on the forward strand for the region
            route, e.g. ``T`` (or ``-`` for a deletion).
        species: Ensembl species name (default ``homo_sapiens``).
        max_consequences: cap on returned per-transcript consequence rows
            (default 25). The FULL count is always in
            ``n_transcript_consequences`` and the rows kept are the most
            severe (HIGH > MODERATE > LOW > MODIFIER) —
            ``transcript_consequences_truncated`` flags the cap.

    Returns ``{query, n_results, results}``; each result:
    ``{input, assembly_name, seq_region_name, start, end, strand,
    allele_string, most_severe_consequence, genes: [{gene_id,
    gene_symbol, worst_impact, n_transcripts}...],
    n_transcript_consequences, transcript_consequences_truncated,
    transcript_consequences: [{transcript_id, gene_id, gene_symbol,
    consequence_terms, impact, biotype, (for coding: amino_acids, codons,
    protein_start/end, sift_*, polyphen_*)}...],
    n_regulatory_feature_consequences, n_motif_feature_consequences,
    colocated_variants: [{id, allele_string, clin_sig, somatic, ...}]}``.
    Unknown rsIDs raise with the upstream message.
    """
    if (variant_id is None) == (region is None and allele is None):
        raise ValueError("pass exactly one of: variant_id, or "
                         "region + allele")
    max_consequences = _require_cap(max_consequences, "max_consequences")
    if variant_id is not None:
        raws = _ensembl().vep_id(species, variant_id.strip())
        query: dict = {"variant_id": variant_id.strip(), "species": species}
    else:
        if region is None or allele is None:
            raise ValueError("the region route needs both region and allele")
        raws = _ensembl().vep_region(species, region.strip(), allele.strip())
        query = {"region": region.strip(), "allele": allele.strip(),
                 "species": species}
    results = [_summarize_vep_result(r, max_consequences) for r in raws]
    return {"query": query, "n_results": len(results), "results": results}


@mcp.tool(annotations=READ_ONLY)
def ensembl_homology(gene_symbol: str | None = None,
                     gene_id: str | None = None,
                     homology_type: str = "orthologues",
                     target_species: str | None = None,
                     target_taxon: int | None = None,
                     species: str = DEFAULT_SPECIES,
                     max_homologies: int = 200) -> dict:
    """Orthologues or paralogues of a gene from Ensembl Compara
    (condensed rows — no alignments/sequences).

    Args:
        gene_symbol: gene symbol (e.g. ``BRAF``) — resolved to a stable
            ID in ``species`` first. Pass exactly one of gene_symbol /
            gene_id.
        gene_id: Ensembl gene ID (e.g. ``ENSG00000157764``).
        homology_type: ``orthologues`` (default), ``paralogues`` or
            ``projections``.
        target_species: restrict to one species (``mus_musculus``).
        target_taxon: restrict to an NCBI taxon subtree, e.g. ``9443``
            (Primates). Combinable with target_species (OR semantics
            upstream).
        species: source species of the gene (default ``homo_sapiens``).
        max_homologies: output row cap (default 200; a human gene has
            ~200+ vertebrate orthologues). ``n_total`` always carries the
            complete count and ``homologies_truncated`` flags the cap.

    Returns ``{gene_id, gene_symbol, species, homology_type,
    target_species, target_taxon, n_total, homologies_truncated,
    homologies}``; rows sorted by (species, id):
    ``{type (ortholog_one2one/ortholog_one2many/within_species_paralog/
    ...), species, id, protein_id, taxonomy_level, method_link_type}``.
    Quirk: the upstream /homology/symbol route stalls — this tool always
    resolves symbols itself and queries by stable ID.
    """
    if (gene_symbol is None) == (gene_id is None):
        raise ValueError("pass exactly one of gene_symbol / gene_id")
    max_homologies = _require_cap(max_homologies, "max_homologies")
    symbol = gene_symbol
    if gene_id is None:
        rec = _resolve_gene_id(gene_symbol, species)
        gene_id = rec["id"]
        symbol = rec.get("display_name", gene_symbol)
    rows = _ensembl().homology_id(species, gene_id, homology_type,
                                  target_species=target_species,
                                  target_taxon=target_taxon)
    rows = sorted(rows, key=lambda r: (str(r.get("species", "")),
                                       str(r.get("id", ""))))
    capped = rows[:max_homologies]
    return {"gene_id": gene_id, "gene_symbol": symbol, "species": species,
            "homology_type": homology_type,
            "target_species": target_species, "target_taxon": target_taxon,
            "n_total": len(rows),
            "homologies_truncated": len(rows) > len(capped),
            "homologies": capped}


@mcp.tool(annotations=READ_ONLY)
def ensembl_sequence(stable_id: str | None = None,
                     region: str | None = None,
                     species: str = DEFAULT_SPECIES,
                     seq_type: str = "genomic",
                     max_bytes: int = 400_000) -> dict:
    """Fetch sequence from Ensembl — by stable ID (gene/transcript/
    protein) or by genomic region.

    Pass EITHER ``stable_id`` OR ``region``:

    Args:
        stable_id: Ensembl stable ID (``ENSG...``/``ENST...``/``ENSP...``).
        region: 1-based inclusive region ``chrom:start..end`` or
            ``chrom:start-end`` (GRCh38 for human), e.g.
            ``7:140753300-140753400``. Max 10 Mb upstream.
        species: species for the region route (default ``homo_sapiens``);
            ignored for stable IDs.
        seq_type: for the ID route — ``genomic`` (default; the unspliced
            span for genes/transcripts), ``cdna``, ``cds`` or ``protein``
            (``protein`` only for ENST/ENSP). Ignored for regions: the
            output then reports ``seq_type: "genomic"`` regardless of
            what was passed (the region route always returns genomic
            DNA).
        max_bytes: payload guard (default 400000). Sequences larger than
            this have the ``seq`` field omitted — ``length``,
            ``sha256`` and metadata are always returned; re-call with a
            larger max_bytes for the full text (a whole gene's genomic
            span can be megabases — prefer cdna/cds/protein or
            sub-regions).

    Returns ``{found, query, seq_type, id, description, molecule,
    length, sha256, seq}`` — ``length`` is in the unit implied by
    ``molecule`` (bases for dna, residues for protein); ``seq`` is
    replaced by ``seq_omitted`` when capped. ``found: false`` with null
    fields for unknown stable IDs; malformed/oversized regions raise
    with the upstream message.
    """
    if (stable_id is None) == (region is None):
        raise ValueError("pass exactly one of stable_id / region")
    if stable_id is not None:
        raw = _ensembl().sequence_id(stable_id.strip(), seq_type=seq_type)
        query: dict = {"stable_id": stable_id.strip()}
    else:
        raw = _ensembl().sequence_region(species, region.strip())
        query = {"region": region.strip(), "species": species}
        seq_type = "genomic"
    if raw is None:
        return {"found": False, "query": query, "seq_type": seq_type,
                "id": None, "description": None, "molecule": None,
                "length": None, "sha256": None, "seq": None}
    seq = raw.get("seq") or ""
    result = {"found": True, "query": query, "seq_type": seq_type,
              "id": raw.get("id"), "description": raw.get("desc"),
              "molecule": raw.get("molecule"), "length": len(seq),
              "sha256": hashlib.sha256(seq.encode()).hexdigest(),
              "seq": seq}
    return _cap_text_payload(result, "seq", max_bytes)


@mcp.tool(annotations=READ_ONLY)
def ensembl_overlap_region(region: str, feature: str = "gene",
                           species: str = DEFAULT_SPECIES,
                           max_features: int = 500) -> dict:
    """List Ensembl features overlapping a genomic region — genes,
    transcripts, regulatory features (enhancers/promoters), repeats,
    variants, karyotype bands.

    Args:
        region: 1-based inclusive region ``chrom:start-end`` (GRCh38 for
            human), e.g. ``7:140719327-140925199``. Upstream rejects
            spans > 5 Mb (HTTP 400) — split larger queries.
        feature: feature type — ``gene`` (default), ``transcript``,
            ``exon``, ``cds``, ``regulatory`` (Ensembl Regulatory Build:
            enhancers, promoters, CTCF sites...), ``motif``,
            ``repeat``, ``variation``, ``structural_variation``,
            ``band``, ``simple``, ``misc``.
        species: Ensembl species name (default ``homo_sapiens``).
        max_features: output row cap (default 500). ``n_total`` always
            carries the complete overlap count and ``features_truncated``
            flags the cap.

    Returns ``{region, species, feature, n_total, features_truncated,
    features}`` sorted by (start, id). Row shape varies by feature type —
    genes: ``{id, external_name, biotype, description, start, end,
    strand, canonical_transcript, ...}``; regulatory: ``{id, description
    (enhancer/promoter/...), start, end, extended_start/end, ...}``.
    Empty regions return ``n_total: 0``.
    """
    max_features = _require_cap(max_features, "max_features")
    rows = _ensembl().overlap_region(species, region.strip(), feature)
    rows = sorted(rows, key=lambda r: (r.get("start") or 0,
                                       str(r.get("id", ""))))
    capped = rows[:max_features]
    return {"region": region.strip(), "species": species,
            "feature": feature, "n_total": len(rows),
            "features_truncated": len(rows) > len(capped),
            "features": capped}




@mcp.tool(annotations=READ_ONLY)
def ucsc_list_tracks(genome: str = DEFAULT_GENOME,
                     filter_text: str | None = None,
                     max_tracks: int = 200) -> dict:
    """List data tracks available in a UCSC Genome Browser assembly
    (leaf tracks only — the queryable ones), optionally filtered.

    Args:
        genome: UCSC genome/db name — ``hg38`` (default), ``hg19``,
            ``mm39``, ``danRer11``, ... (~220 assemblies).
        filter_text: case-insensitive substring matched against track
            name, short label and long label — e.g. ``phyloP``,
            ``TFBS``, ``ClinVar``. Omit to list everything (hg38 has
            ~24k leaf tracks; you almost always want a filter).
        max_tracks: output row cap (default 200). ``n_total`` carries
            the full match count; ``tracks_truncated`` flags the cap.

    Returns ``{genome, filter_text, n_total, tracks_truncated, tracks}``
    sorted by track name; each row: ``{track, short_label, long_label,
    type (wig/bigWig/bed N/bigBed/factorSource/...), group, parent}``.
    Use the ``track`` value with ``ucsc_track_data``. Quirk: the first
    call per genome downloads the full ~17 MB listing and caches it for
    the process, so repeat filters are fast.
    """
    max_tracks = _require_cap(max_tracks, "max_tracks")
    tracks = _ucsc().list_tracks(genome)
    needle = (filter_text or "").lower()
    rows = []
    for name, meta in tracks.items():
        if not isinstance(meta, dict):
            continue
        hay = " ".join((name, str(meta.get("shortLabel", "")),
                        str(meta.get("longLabel", "")))).lower()
        if needle and needle not in hay:
            continue
        rows.append({"track": name,
                     "short_label": meta.get("shortLabel"),
                     "long_label": meta.get("longLabel"),
                     "type": meta.get("type"),
                     "group": meta.get("group"),
                     "parent": meta.get("parent")})
    rows.sort(key=lambda r: r["track"])
    capped = rows[:max_tracks]
    return {"genome": genome, "filter_text": filter_text,
            "n_total": len(rows),
            "tracks_truncated": len(rows) > len(capped),
            "tracks": capped}


def _extract_track_rows(payload: dict, track: str, chrom: str) -> list:
    """Locate the row list in a /getData/track payload — shared by
    ucsc_track_data / ucsc_conservation / ucsc_tfbs_clusters (the two
    summary tools previously missed this hardening, so
    a composite track keyed by subtrack silently summarized zero rows —
    e.g. a confident coverage_fraction: 0.0 — instead of raising).
    """
    rows = payload.get(track)
    if rows is None:


        lists = [v for v in payload.values() if isinstance(v, list)]
        rows = lists[0] if len(lists) == 1 else None
    if isinstance(rows, dict):
        rows = rows.get(chrom, [])
    if rows is None:



        if int(payload.get("itemsReturned") or 0) > 0:
            raise RuntimeError(
                f"unrecognised UCSC payload shape for track {track!r}: "
                f"itemsReturned={payload.get('itemsReturned')} but no "
                f"single row list under the track name")
        rows = []
    return rows


@mcp.tool(annotations=READ_ONLY)
def ucsc_track_data(track: str, chrom: str, start: int, end: int,
                    genome: str = DEFAULT_GENOME,
                    max_rows: int = 1000) -> dict:
    """Fetch raw rows of any UCSC Genome Browser track in a region —
    the generic escape hatch behind ``ucsc_conservation`` /
    ``ucsc_tfbs_clusters`` (gene tracks, ClinVar, GWAS catalog, CpG
    islands, repeats, ...).

    Args:
        track: UCSC track name as returned by ``ucsc_list_tracks``
            (e.g. ``knownGene``, ``cpgIslandExt``, ``clinvarMain``).
        chrom: ``chr``-prefixed chromosome name (``chr7``, ``chrX``) —
            UCSC requires the prefix, unlike Ensembl.
        start: region start, 0-based half-open (UCSC convention; an
            Ensembl 1-based start is ``start - 1`` here).
        end: region end (exclusive).
        genome: UCSC genome/db name (default ``hg38``).
        max_rows: row cap passed to the API as ``maxItemsOutput``
            (default 1000). ``truncated`` reflects the API's own
            ``maxItemsLimit`` flag — no silent truncation.

    Returns ``{genome, track, chrom, start, end, track_type,
    items_returned, truncated, rows}`` — ``rows`` in upstream shape
    (BED-like tracks: ``{chrom, chromStart, chromEnd, name, score,
    ...}``; wiggle tracks: ``{start, end, value}``). Unknown tracks
    raise with the upstream "can not find track" message. Quirk: for
    some huge tracks the API caps output itself and points at
    ``dataDownloadUrl`` — that URL is echoed when present.
    """
    max_rows = _require_cap(max_rows, "max_rows")
    payload = _ucsc().track_data(genome, track, chrom, int(start), int(end),
                                 max_items=max_rows)
    rows = _extract_track_rows(payload, track, chrom)
    out = {"genome": genome, "track": track, "chrom": chrom,
           "start": int(start), "end": int(end),
           "track_type": payload.get("trackType"),
           "items_returned": payload.get("itemsReturned", len(rows)),
           "truncated": bool(payload.get("maxItemsLimit")),
           "rows": rows}
    if payload.get("dataDownloadUrl"):
        out["data_download_url"] = payload["dataDownloadUrl"]
    return out


@mcp.tool(annotations=READ_ONLY)
def ucsc_conservation(chrom: str, start: int, end: int,
                      genome: str = DEFAULT_GENOME,
                      track: str = "phyloP100way",
                      include_values: bool = False,
                      max_values: int = 2000) -> dict:
    """Evolutionary conservation summary for a region from UCSC
    phyloP / phastCons tracks (base-wise scores over multi-species
    alignments).

    Args:
        chrom: ``chr``-prefixed chromosome (``chr7``).
        start: region start, 0-based half-open (UCSC convention).
        end: region end (exclusive). Span capped at 100000 bp — split
            larger regions.
        genome: UCSC genome/db (default ``hg38``).
        track: conservation track (default ``phyloP100way`` — 100-way
            vertebrate phyloP; positive = conserved, negative =
            fast-evolving, exon ~ >2, strongly constrained ~ >5).
            Alternatives on hg38: ``phastCons100way`` (0..1
            conservation probability), ``phyloP30way``,
            ``phastCons30way``, ``phyloP447way`` (Zoonomia+),
            ``phyloP470way``; hg19: ``phyloP100wayAll`` /
            ``phastCons100way``.
        include_values: when true, also return per-base rows
            ``{start, end, value}`` (capped at ``max_values`` rows,
            ``values_truncated`` flags the cap). Default false: summary
            stats only.
        max_values: per-base row cap for ``include_values`` (default
            2000).

    Returns ``{genome, track, chrom, start, end, span_bp,
    n_bases_covered, coverage_fraction, mean, min, max}`` (+ ``values``,
    ``values_truncated`` when requested). Stats are weighted by each
    row's base span, clipped to the requested window; bases without
    alignment data are simply uncovered (coverage_fraction < 1), not
    zero-scored. Non-score tracks (BED-like, no per-base ``value``)
    raise — use ``ucsc_track_data`` for those; an upstream-truncated
    row list also raises rather than summarising a prefix.
    """
    start, end = int(start), int(end)
    max_values = _require_cap(max_values, "max_values")
    span = end - start
    if span <= 0:
        raise ValueError("end must be > start (0-based half-open)")
    if span > _MAX_CONSERVATION_SPAN:
        raise ValueError(f"span {span} bp exceeds the "
                         f"{_MAX_CONSERVATION_SPAN} bp cap — split the "
                         f"region into consecutive windows")
    payload = _ucsc().track_data(genome, track, chrom, start, end)
    if payload.get("maxItemsLimit"):


        raise RuntimeError(
            f"UCSC truncated the {track!r} listing for this span "
            f"(itemsReturned={payload.get('itemsReturned')}) — the summary "
            f"would be incomplete; query a smaller region")
    rows = _extract_track_rows(payload, track, chrom)
    covered = 0
    total = 0.0
    vmin = vmax = None
    kept: list[dict] = []
    for r in rows:
        if "value" not in r:
            raise ValueError(
                f"track {track!r} (type {payload.get('trackType')!r}) "
                f"returns rows without per-base values — "
                f"ucsc_conservation needs a wiggle/bigWig score track "
                f"(phyloP*/phastCons*); use ucsc_track_data for "
                f"BED-like tracks")


        rs, re_ = max(start, int(r.get("start", 0))), min(end, int(r.get("end", 0)))
        w = re_ - rs
        if w <= 0:
            continue
        v = float(r["value"])
        covered += w
        total += v * w
        vmin = v if vmin is None or v < vmin else vmin
        vmax = v if vmax is None or v > vmax else vmax
        kept.append({"start": rs, "end": re_, "value": v})
    out = {"genome": genome, "track": track, "chrom": chrom,
           "start": start, "end": end, "span_bp": span,
           "n_bases_covered": covered,
           "coverage_fraction": round(covered / span, 6),
           "mean": round(total / covered, 6) if covered else None,
           "min": vmin, "max": vmax}
    if include_values:



        capped = kept[:max_values]
        out["values"] = capped
        out["values_truncated"] = len(kept) > len(capped)
        out["n_value_rows"] = len(kept)
    return out


@mcp.tool(annotations=READ_ONLY)
def ucsc_tfbs_clusters(chrom: str, start: int, end: int,
                       genome: str = DEFAULT_GENOME,
                       max_rows: int = 1000) -> dict:
    """ENCODE transcription-factor binding site clusters overlapping a
    region (ChIP-seq peak clusters across hundreds of cell types) —
    which TFs bind where.

    Args:
        chrom: ``chr``-prefixed chromosome (``chr7``).
        start: region start, 0-based half-open (UCSC convention).
        end: region end (exclusive).
        genome: ``hg38`` (default; track ``encRegTfbsClustered``,
            ENCODE 3) or ``hg19`` (``wgEncodeRegTfbsClusteredV3``).
            Other assemblies have no ENCODE TFBS track and raise.
        max_rows: row cap (API ``maxItemsOutput``, default 1000);
            ``truncated`` reflects the API's own ``maxItemsLimit`` flag.

    Returns ``{genome, track, chrom, start, end, items_returned,
    truncated, n_factors, factors, clusters}`` — ``clusters`` sorted by
    (chromStart, name): ``{name (TF symbol, e.g. CTCF), chrom,
    chromStart, chromEnd, score (0-1000 cluster strength), sourceCount
    (number of supporting experiments)}``; ``factors`` is the distinct
    TF list. Score >= ~600 and high sourceCount ~ robust binding.
    """
    track = _TFBS_TRACKS.get(genome)
    if track is None:
        raise ValueError(f"no ENCODE TFBS cluster track known for genome "
                         f"{genome!r} — supported: "
                         f"{sorted(_TFBS_TRACKS)}")
    max_rows = _require_cap(max_rows, "max_rows")
    payload = _ucsc().track_data(genome, track, chrom, int(start), int(end),
                                 max_items=max_rows)
    rows = _extract_track_rows(payload, track, chrom)
    clusters = [{"name": r.get("name"), "chrom": r.get("chrom"),
                 "chromStart": r.get("chromStart"),
                 "chromEnd": r.get("chromEnd"), "score": r.get("score"),
                 "sourceCount": r.get("sourceCount")} for r in rows]
    clusters.sort(key=lambda r: (r["chromStart"] or 0, str(r["name"])))
    factors = sorted({c["name"] for c in clusters if c["name"]})
    return {"genome": genome, "track": track, "chrom": chrom,
            "start": int(start), "end": int(end),
            "items_returned": payload.get("itemsReturned", len(clusters)),
            "truncated": bool(payload.get("maxItemsLimit")),
            "n_factors": len(factors), "factors": factors,
            "clusters": clusters}


@mcp.tool(annotations=READ_ONLY)
def ucsc_chrom_sizes(genome: str = DEFAULT_GENOME,
                     filter_text: str | None = None,
                     max_chroms: int = 100) -> dict:
    """Chromosome/contig names and sizes of a UCSC assembly — for
    validating coordinates and iterating regions.

    Args:
        genome: UCSC genome/db name (default ``hg38``).
        filter_text: case-insensitive substring filter on the name —
            e.g. ``chr1``; omit for all. Tip: hg38 has 711 sequences,
            mostly alt/random/unplaced contigs; the primary chromosomes
            sort first (largest-first ordering).
        max_chroms: output row cap (default 100). ``n_total`` carries
            the full (post-filter) count; ``chroms_truncated`` flags the
            cap.

    Returns ``{genome, filter_text, chrom_count (assembly-wide, from
    the API), n_total, chroms_truncated, chromosomes: [{name,
    size_bp}...]}`` sorted by size descending (so chr1..chr22/X/Y lead
    on human).
    """
    max_chroms = _require_cap(max_chroms, "max_chroms")
    payload = _ucsc().chromosomes(genome)
    chroms = payload.get("chromosomes") or {}
    rows = [{"name": k, "size_bp": v} for k, v in chroms.items()]
    needle = (filter_text or "").lower()
    if needle:
        rows = [r for r in rows if needle in r["name"].lower()]
    rows.sort(key=lambda r: (-r["size_bp"], r["name"]))
    capped = rows[:max_chroms]
    return {"genome": genome, "filter_text": filter_text,
            "chrom_count": payload.get("chromCount"),
            "n_total": len(rows),
            "chroms_truncated": len(rows) > len(capped),
            "chromosomes": capped}


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
