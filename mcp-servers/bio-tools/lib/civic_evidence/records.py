"""GraphQL field selections + record canonicalization for civic-evidence.

One shared ``canonicalize(obj) -> bytes`` implements the contract's
canonicalization rules:

  * JSON with sorted keys, UTF-8, no insignificant whitespace variation
  * unordered collections sorted with a stable key (aliases, HGVS strings,
    ClinVar ids, variant types, therapies, phenotypes, assertion back-refs)
  * no volatile fields are ever selected (no events/revisions/comments/
    timestamps/flags), so nothing has to be dropped after the fact

Scientific content (names, coordinates, identifiers, descriptions, evidence
attributes) is never rewritten.
"""
from __future__ import annotations

import json







GENE_FIELDS = """
  id
  name
  entrezId
  fullName
  featureAliases
  description
  link
"""

VARIANT_CORE_FIELDS = """
  id
  name
  link
  variantAliases
  variantTypes { id name soid }
  feature { id name }
  singleVariantMolecularProfileId
  ... on GeneVariant {
    alleleRegistryId
    clinvarIds
    hgvsDescriptions
    coordinates {
      chromosome
      start
      stop
      referenceBases
      variantBases
      referenceBuild
      ensemblVersion
      representativeTranscript
    }
  }
"""

EVIDENCE_FIELDS = """
  id
  name
  status
  evidenceLevel
  evidenceType
  evidenceDirection
  significance
  evidenceRating
  variantOrigin
  therapyInteractionType
  description
  link
  disease { id name doid displayName }
  therapies { id name ncitId }
  molecularProfile { id name }
  source { id sourceType citationId citation }
  phenotypes { id hpoId name }
"""

ASSERTION_FIELDS = """
  id
  name
  status
  assertionType
  assertionDirection
  significance
  ampLevel
  summary
  description
  link
  variantOrigin
  therapyInteractionType
  regulatoryApproval
  fdaCompanionTest
  nccnGuideline { id name }
  nccnGuidelineVersion
  acmgCodes { id code }
  clingenCodes { id code }
  disease { id name doid displayName }
  therapies { id name ncitId }
  molecularProfile { id name }
  phenotypes { id hpoId name }
  evidenceItemsCount
"""

MOLECULAR_PROFILE_FIELDS = """
  id
  name
  rawName
  link
  description
  molecularProfileScore
  isComplex
  isMultiVariant
  molecularProfileAliases
  variants { id name feature { id name } }
  evidenceCountsByStatus { acceptedCount submittedCount rejectedCount }
"""

DISEASE_FIELDS = """
  id
  name
  displayName
  doid
  diseaseUrl
  diseaseAliases
  link
"""

THERAPY_FIELDS = """
  id
  name
  ncitId
  therapyUrl
  therapyAliases
  link
"""





_LIST_SORT_KEYS = {

    "variantTypes": lambda d: (d.get("soid") or "", d.get("id") or 0),
    "therapies": lambda d: (d.get("id") or 0),
    "phenotypes": lambda d: (d.get("id") or 0),
    "acmgCodes": lambda d: (d.get("code") or ""),
    "clingenCodes": lambda d: (d.get("code") or ""),
    "variants": lambda d: (d.get("id") or 0),
}

_STRING_LIST_FIELDS = {
    "featureAliases", "variantAliases", "hgvsDescriptions", "clinvarIds",
    "molecularProfileAliases", "diseaseAliases", "therapyAliases",
}


def normalize(obj, parent_key: str | None = None):
    """Recursively sort unordered collections so output is order-stable.

    Connection node lists (``records``) are NOT sorted here — they are returned
    in the API's stable sort order (id-keyed where the tool requests it) and
    order is part of what the gate verifies.
    """
    if isinstance(obj, dict):
        return {k: normalize(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [normalize(v, parent_key) for v in obj]
        if parent_key in _STRING_LIST_FIELDS:
            return sorted(items, key=lambda s: (s is None, s))
        if parent_key in _LIST_SORT_KEYS and all(isinstance(i, dict) for i in items):
            return sorted(items, key=_LIST_SORT_KEYS[parent_key])
        return items
    return obj


def canonicalize(obj) -> bytes:
    """Stable byte representation used by the gate and the tests."""
    return json.dumps(normalize(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
