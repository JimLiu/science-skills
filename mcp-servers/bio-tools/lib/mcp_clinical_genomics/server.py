"""mcp-clinical-genomics — tier-2 domain MCP server (stdio).

Clinical genomics knowledge bases:
  * ClinGen curations (gene-disease validity, dosage sensitivity, clinical
    actionability, ERepo variant classifications) via ``clingen-curations``.
  * CIViC clinical evidence (genes/variants/evidence/assertions/molecular
    profiles/diseases/therapies) via ``civic-evidence`` — complete,
    count-verified Relay pagination.
  * Open Targets Platform GraphQL passthrough (thin local client — see
    ``open_targets.py``).

Replaces tooluniverse/clingen (8 methods), tooluniverse/civic (12 methods)
and knowledgebase ``bc_query_open_targets_graphql`` — see ``migration.json``.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from mcp_servers_common.guarded import GuardedFastMCP

from mcp_servers_common.criteria import blank, reject_blank
from mcp_servers_common.gate import apply_gate_fastmcp
from mcp.types import ToolAnnotations



READ_ONLY = ToolAnnotations(readOnlyHint=True)



mcp = GuardedFastMCP("mcp-clinical-genomics")



@lru_cache(maxsize=1)
def _clingen():
    from clingen_curations import ClinGenCurations
    return ClinGenCurations()


@lru_cache(maxsize=1)
def _civic():
    from civic_evidence import CivicEvidence
    return CivicEvidence()


@lru_cache(maxsize=1)
def _open_targets():
    from .open_targets import OpenTargetsClient
    return OpenTargetsClient()




@mcp.tool(annotations=READ_ONLY)
def clingen_gene_validity(gene: str | None = None) -> dict:
    """ClinGen gene-disease validity curations (classification of how strong
    the evidence is that variation in a gene causes a disease: Definitive,
    Strong, Moderate, Limited, Disputed, Refuted, No Known Disease
    Relationship).

    Args:
        gene: HGNC gene symbol (exact, case-insensitive match — e.g.
            "BRCA2"). Omit to list ALL 3,600+ validity curations.

    Returns total, records (gene_symbol, disease label + MONDO id, mode of
    inheritance, classification, SOP, expert panel, report date), source.
    """


    reject_blank(gene=gene)
    return _clingen().gene_validity(gene=gene)


@mcp.tool(annotations=READ_ONLY)
def clingen_dosage_sensitivity(gene: str | None = None,
                               include_regions: bool = False) -> dict:
    """ClinGen dosage sensitivity curations: haploinsufficiency and
    triplosensitivity assertions for genes (and optionally ISCA genomic
    regions such as recurrent CNV regions).

    Args:
        gene: HGNC gene symbol for exact, case-insensitive match, or an ISCA
            region ID (e.g. "ISCA-37390"). Omit for the full table.
        include_regions: include ISCA region records alongside gene records
            (bulk listing is genes-only by default).

    Returns total, records (symbol/id, record_type gene|region,
    haploinsufficiency + triplosensitivity assertions with numeric scores and
    labels, genomic location, report date), source.
    """



    reject_blank(gene=gene)
    return _clingen().dosage_sensitivity(gene=gene, include_regions=include_regions)


@mcp.tool(annotations=READ_ONLY)
def clingen_actionability(gene: str | None = None, context: str = "both") -> dict:
    """ClinGen clinical actionability curations: for disorders associated
    with a gene, whether early intervention in pre-symptomatic carriers is
    actionable (intervention/outcome pairs with severity, likelihood,
    effectiveness, nature-of-intervention scores and the total score).

    The gene filter genuinely filters (the legacy tooluniverse method's gene
    filter was a silent no-op that always returned the entire table) and
    matches any member of multi-gene topics (e.g. BRCA1,BRCA2 HBOC).

    Args:
        gene: HGNC gene symbol (exact, case-insensitive). Omit for all topics.
        context: "adult", "pediatric", or "both" (default).

    Returns one block per context with total + records (doc_id, genes,
    disease, outcome, intervention, component scores, total_score, report
    stage/date), plus source.
    """

    reject_blank(gene=gene)
    return _clingen().actionability(gene=gene, context=context)


@mcp.tool(annotations=READ_ONLY)
def clingen_variant_classifications(gene: str | None = None,
                                    caid: str | None = None,
                                    hgvs: str | None = None) -> dict:
    """ClinGen Evidence Repository (ERepo) expert-panel variant pathogenicity
    classifications (VCEP interpretations under ACMG criteria).

    Provide EXACTLY ONE of:
        gene: HGNC gene symbol (all classified variants in the gene —
            retrieval is complete, not capped at the first page).
        caid: ClinGen canonical allele ID (e.g. "CA114360").
        hgvs: HGVS expression (e.g. "NM_000277.2:c.1222C>T").

    Returns total, records (interpretation id, CAID, HGVS, gene, MONDO
    condition, classification e.g. Pathogenic/Likely Benign, ACMG criteria
    met, expert panel, evaluation date), query echo, source.
    """
    return _clingen().variant_classifications(gene=gene, caid=caid, hgvs=hgvs)







@mcp.tool(annotations=READ_ONLY)
def civic_search_genes(entrez_symbol: str) -> dict:
    """Find CIViC gene records by exact Entrez symbol (e.g. "BRAF").

    Returns total_count, pages_fetched, records (CIViC gene id, name, entrez
    id, aliases, description, source ids) — use the gene id with
    civic_gene_variants.
    """
    return _civic().search_genes(entrez_symbol)


@mcp.tool(annotations=READ_ONLY)
def civic_gene_variants(gene_id: int) -> dict:
    """All variants of one CIViC gene (by CIViC gene id), fully paginated —
    complete even for genes with hundreds of variants.

    Returns total_count, pages_fetched, records (variant id, name, feature,
    aliases, variant types) sorted by variant id.
    """
    return _civic().gene_variants(gene_id)


@mcp.tool(annotations=READ_ONLY)
def civic_get_variant(variant_id: int) -> dict:
    """One CIViC variant by its CIViC variant id (details incl. aliases,
    variant types, feature/gene linkage). Returns found=false if absent."""
    return _civic().get_variant(variant_id)


@mcp.tool(annotations=READ_ONLY)
def civic_search_variants(name: str, gene_id: int | None = None) -> dict:
    """Search CIViC variants by name substring (e.g. "V600"), optionally
    scoped to a CIViC gene id. Fully paginated; sorted by variant id."""


    if blank(name):
        raise ValueError("name must be non-empty")
    return _civic().search_variants(name, gene_id)


@mcp.tool(annotations=READ_ONLY)
def civic_get_evidence_item(evidence_id: int) -> dict:
    """One CIViC evidence item by id: clinical significance of a molecular
    profile in a disease/therapy context (evidence level A–E, type, direction,
    significance, rating, disease, therapies, source). Returns found=false
    if absent."""
    return _civic().get_evidence_item(evidence_id)


@mcp.tool(annotations=READ_ONLY)
def civic_search_evidence(disease_name: str | None = None,
                          therapy_name: str | None = None,
                          evidence_level: str | None = None,
                          evidence_type: str | None = None,
                          evidence_direction: str | None = None,
                          significance: str | None = None,
                          variant_origin: str | None = None,
                          evidence_rating: int | None = None,
                          status: str | None = None,
                          molecular_profile_name: str | None = None,
                          molecular_profile_id: int | None = None,
                          variant_id: int | None = None,
                          disease_id: int | None = None,
                          therapy_id: int | None = None,
                          phenotype_id: int | None = None,
                          source_id: int | None = None,
                          assertion_id: int | None = None) -> dict:
    """Search CIViC evidence items by any combination of filters; fully
    paginated and count-verified, sorted by ascending evidence id.

    Enum filters take CIViC GraphQL enum values verbatim:
    evidence_level "A".."E"; evidence_type PREDICTIVE|PROGNOSTIC|DIAGNOSTIC|
    PREDISPOSING|ONCOGENIC|FUNCTIONAL; evidence_direction SUPPORTS|
    DOES_NOT_SUPPORT; significance e.g. SENSITIVITYRESPONSE|RESISTANCE;
    variant_origin e.g. SOMATIC|RARE_GERMLINE; status ACCEPTED|SUBMITTED|
    REJECTED|ALL. Name filters are substrings (disease_name, therapy_name,
    molecular_profile_name); id filters are exact CIViC ids.

    Warning: with no filters this walks the ENTIRE evidence corpus (10k+
    items, ~100 paged requests) — provide at least one filter.
    """


    reject_blank(dict(locals()))
    filters = {k: v for k, v in locals().items() if v is not None}
    return _civic().search_evidence(**filters)


@mcp.tool(annotations=READ_ONLY)
def civic_get_assertion(assertion_id: int) -> dict:
    """One CIViC assertion by id: an expert-curated summary claim (AMP/ASCO/
    CAP tier, ACMG codes, FDA companion test flags) aggregating evidence
    items for a molecular profile in a disease/therapy context. Returns
    found=false if absent."""
    return _civic().get_assertion(assertion_id)


@mcp.tool(annotations=READ_ONLY)
def civic_search_assertions(disease_name: str | None = None,
                            therapy_name: str | None = None,
                            assertion_type: str | None = None,
                            assertion_direction: str | None = None,
                            significance: str | None = None,
                            amp_level: str | None = None,
                            status: str | None = None,
                            molecular_profile_name: str | None = None,
                            molecular_profile_id: int | None = None,
                            variant_id: int | None = None,
                            variant_name: str | None = None,
                            disease_id: int | None = None,
                            therapy_id: int | None = None,
                            phenotype_id: int | None = None,
                            evidence_id: int | None = None,
                            summary: str | None = None) -> dict:
    """Search CIViC assertions by any combination of filters; fully paginated
    and count-verified, sorted by ascending assertion id.

    assertion_type PREDICTIVE|PROGNOSTIC|DIAGNOSTIC|PREDISPOSING|ONCOGENIC;
    assertion_direction SUPPORTS|DOES_NOT_SUPPORT; amp_level e.g.
    TIER_I_LEVEL_A; status ACCEPTED|SUBMITTED|REJECTED|ALL. Name/summary
    filters are substrings; id filters exact. No filters = full corpus walk.
    """

    reject_blank(dict(locals()))
    filters = {k: v for k, v in locals().items() if v is not None}
    return _civic().search_assertions(**filters)


@mcp.tool(annotations=READ_ONLY)
def civic_get_molecular_profile(mp_id: int) -> dict:
    """One CIViC molecular profile by id (variant combination that evidence/
    assertions attach to), incl. parsed name and component variant ids.
    Returns found=false if absent."""
    return _civic().get_molecular_profile(mp_id)


@mcp.tool(annotations=READ_ONLY)
def civic_search_molecular_profiles(name: str) -> dict:
    """Search CIViC molecular profiles by name substring (e.g. "BRAF V600E").
    Fully paginated; sorted by id."""
    if blank(name):
        raise ValueError("name must be non-empty")
    return _civic().search_molecular_profiles(name)


@mcp.tool(annotations=READ_ONLY)
def civic_search_diseases(name: str) -> dict:
    """Search CIViC disease records by name substring (e.g. "melanoma").
    Returns DOIDs + display names; fully paginated; sorted by id."""
    if blank(name):
        raise ValueError("name must be non-empty")
    return _civic().search_diseases(name)


@mcp.tool(annotations=READ_ONLY)
def civic_search_therapies(name: str) -> dict:
    """Search CIViC therapy records by name substring (e.g. "vemurafenib").
    Returns NCIt ids + names; fully paginated; sorted by id."""
    if blank(name):
        raise ValueError("name must be non-empty")
    return _civic().search_therapies(name)




@mcp.tool(annotations=READ_ONLY)
def open_targets_graphql(query: str,
                         variables: dict[str, Any] | None = None) -> dict:
    """Run an arbitrary GraphQL query against the Open Targets Platform API
    (https://api.platform.opentargets.org/api/v4/graphql): targets, diseases,
    drugs, target-disease association scores, evidence, tractability, safety,
    known drugs, etc.

    Example: query='query($id: String!){target(ensemblId: $id){approvedSymbol
    associatedDiseases{count}}}', variables={"id": "ENSG00000157764"}.
    Introspection queries work for schema discovery.

    Common top-level fields (live-introspected; saves a schema round-trip —
    note ``knownDrugs`` was renamed to ``drugAndClinicalCandidates`` upstream):
      * Disease: id, name, description, synonyms, therapeuticAreas, ancestors,
        descendants, parents, children, phenotypes, associatedTargets,
        drugAndClinicalCandidates, evidences, otarProjects, similarEntities
      * Target: id, approvedSymbol, approvedName, biotype, functionDescriptions,
        genomicLocation, tractability, safetyLiabilities, geneticConstraint,
        pathways, geneOntology, expressions, interactions, associatedDiseases,
        drugAndClinicalCandidates, prioritisation, pharmacogenomics
      * Drug: id, name, drugType, description, synonyms, tradeNames,
        maximumClinicalStage, mechanismsOfAction, indications, drugWarnings,
        adverseEvents, pharmacogenomics, crossReferences

    Returns {data, attempts} plus an "errors" list when the server reports
    GraphQL errors. Transient "Internal server error" responses (a known
    platform quirk on valid queries) are retried up to 3 attempts before
    being surfaced.
    """
    return _open_targets().execute(query, variables)


def _ot_query(query: str, variables: dict, root: str) -> dict:
    """Execute an Open Targets query and return data[root], or {errors} when
    the server reported GraphQL errors / the root key resolved null."""
    result = _open_targets().execute(query, variables)
    data = result.get("data") or {}
    node = data.get(root)
    if node is None:
        return {"errors": result.get("errors")
                or [{"message": f"{root} not found for {variables}"}]}
    return node


_OT_DISEASE_DRUGS_Q = """\
query($id: String!) {
  disease(efoId: $id) {
    id name
    drugAndClinicalCandidates {
      count
      rows { id maxClinicalStage drug { id name drugType } }
    }
  }
}"""

_OT_DISEASE_TARGETS_Q = """\
query($id: String!, $size: Int!) {
  disease(efoId: $id) {
    id name
    associatedTargets(page: {size: $size, index: 0}) {
      count
      rows { score target { id approvedSymbol } }
    }
  }
}"""

_OT_DRUG_Q = """\
query($id: String!) {
  drug(chemblId: $id) {
    id name drugType maximumClinicalStage
    mechanismsOfAction {
      rows { mechanismOfAction actionType targets { id approvedSymbol } }
    }
  }
}"""


@mcp.tool(annotations=READ_ONLY)
def open_targets_disease_drugs(efo_id: str, size: int = 25) -> dict:
    """Known/investigational drugs for a disease (Open Targets Platform).

    Wraps ``Disease.drugAndClinicalCandidates`` (the upstream replacement for
    the removed ``knownDrugs`` field) — no introspection needed.

    Args:
        efo_id: disease ontology id as used by Open Targets (EFO/MONDO/etc.),
            e.g. "MONDO_0004992".
        size: cap on returned rows (client-side slice — the upstream field is
            not paginated). Default 25.

    Returns {id, name, drugAndClinicalCandidates:{count, rows:[{id,
    maxClinicalStage, drug:{id, name, drugType}}]}}, or {errors:[…]} on
    GraphQL error / unknown id.
    """
    node = _ot_query(_OT_DISEASE_DRUGS_Q, {"id": efo_id}, "disease")
    cand = node.get("drugAndClinicalCandidates")
    if cand and isinstance(cand.get("rows"), list):
        cand["rows"] = cand["rows"][:size]
    return node


@mcp.tool(annotations=READ_ONLY)
def open_targets_disease_targets(efo_id: str, size: int = 25) -> dict:
    """Top associated targets for a disease, ranked by Open Targets overall
    association score.

    Wraps ``Disease.associatedTargets(page:{size,index:0})`` — no
    introspection needed.

    Args:
        efo_id: disease ontology id (EFO/MONDO/etc.), e.g. "MONDO_0004992".
        size: page size (default 25; index fixed at 0).

    Returns {id, name, associatedTargets:{count, rows:[{score,
    target:{id, approvedSymbol}}]}}, or {errors:[…]} on GraphQL error /
    unknown id.
    """
    return _ot_query(_OT_DISEASE_TARGETS_Q, {"id": efo_id, "size": size},
                     "disease")


@mcp.tool(annotations=READ_ONLY)
def open_targets_drug(chembl_id: str) -> dict:
    """Drug details by ChEMBL id (Open Targets Platform).

    Wraps ``drug(chemblId:…)`` — no introspection needed.

    Args:
        chembl_id: ChEMBL molecule id, e.g. "CHEMBL1201583".

    Returns {id, name, drugType, maximumClinicalStage,
    mechanismsOfAction:{rows:[{mechanismOfAction, actionType,
    targets:[{id, approvedSymbol}]}]}}, or {errors:[…]} on GraphQL error /
    unknown id.
    """
    return _ot_query(_OT_DRUG_Q, {"id": chembl_id}, "drug")


def main() -> None:




    apply_gate_fastmcp(mcp)
    mcp.run()


if __name__ == "__main__":
    main()
