"""Record normalization and canonicalization for Drugs@FDA applications.

One application record == one Drugs@FDA application (NDA/ANDA/BLA). The raw
payload nests ``products`` (one per product number / strength) and
``submissions`` (regulatory history). Normalization gives every record the
same keys with explicit ``None``s, collapses whitespace, ISO-normalizes
dates, and stable-sorts the nested collections so canonical output is
deterministic.

Canonicalization rules (shared by gate, bench, and tests):

- top-level records sorted by ``application_number``;
- ``products`` sorted by ``product_number``; ``submissions`` sorted by
  (submission_type, submission_number, submission_status_date);
- ``active_ingredients`` sorted by (name, strength);
- list-valued openfda fields sorted; keys sorted in serialized JSON;
- dates ``YYYYMMDD`` -> ``YYYY-MM-DD``;
- no scientific content (numbers, names, statuses, dates) dropped or
  rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

_DATE_RE = re.compile(r"^\d{8}$")
_WS_RE = re.compile(r"\s+")



OPENFDA_FIELDS = (
    "generic_name",
    "brand_name",
    "manufacturer_name",
    "substance_name",
    "unii",
    "rxcui",
    "product_ndc",
    "pharm_class_epc",
    "pharm_class_moa",
    "pharm_class_cs",
    "pharm_class_pe",
)

PRODUCT_FIELDS = (
    "product_number",
    "brand_name",
    "dosage_form",
    "route",
    "marketing_status",
    "te_code",
    "reference_drug",
    "reference_standard",
)

SUBMISSION_FIELDS = (
    "submission_type",
    "submission_number",
    "submission_status",
    "submission_status_date",
    "submission_class_code",
    "submission_class_code_description",
    "review_priority",
)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        v = _WS_RE.sub(" ", value).strip()
        if _DATE_RE.match(v):
            return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
        return v
    return value


def normalize_application(raw: dict) -> dict:
    """Normalize one raw Drugs@FDA result into a flat, deterministic record."""
    openfda = raw.get("openfda") or {}

    products = []
    for p in raw.get("products") or []:
        rec = {f: _clean(p.get(f)) for f in PRODUCT_FIELDS}
        rec["active_ingredients"] = sorted(
            (
                {"name": _clean(ai.get("name")), "strength": _clean(ai.get("strength"))}
                for ai in p.get("active_ingredients") or []
            ),
            key=lambda ai: (ai["name"] or "", ai["strength"] or ""),
        )
        products.append(rec)
    products.sort(key=lambda p: (p["product_number"] or "", p["brand_name"] or ""))

    submissions = []
    for s in raw.get("submissions") or []:
        submissions.append({f: _clean(s.get(f)) for f in SUBMISSION_FIELDS})
    submissions.sort(
        key=lambda s: (
            s["submission_type"] or "",
            s["submission_number"] or "",
            s["submission_status_date"] or "",
        )
    )

    record = {
        "application_number": _clean(raw.get("application_number")),
        "sponsor_name": _clean(raw.get("sponsor_name")),
        "products": products,
        "submissions": submissions,
    }
    for f in OPENFDA_FIELDS:
        values = openfda.get(f)
        record[f"openfda_{f}"] = sorted(_clean(v) for v in values) if values else None
    return record


def canonical_json(records: list[dict]) -> str:
    """Deterministic JSON for a list of normalized application records."""
    ordered = sorted(records, key=lambda r: r["application_number"] or "")
    return json.dumps(ordered, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_of(records: list[dict]) -> str:
    return hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()


def to_tsv(records: list[dict], max_products: Optional[int] = None) -> str:
    """Token-lean product-level TSV rendering (one row per product)."""
    cols = (
        "application_number",
        "sponsor_name",
        "product_number",
        "brand_name",
        "active_ingredients",
        "strength",
        "dosage_form",
        "route",
        "marketing_status",
        "te_code",
    )
    lines = ["\t".join(cols)]
    for r in sorted(records, key=lambda r: r["application_number"] or ""):
        prods = r["products"][:max_products] if max_products else r["products"]
        for p in prods:
            ais = p["active_ingredients"]
            lines.append(
                "\t".join(
                    str(x) if x is not None else ""
                    for x in (
                        r["application_number"],
                        r["sponsor_name"],
                        p["product_number"],
                        p["brand_name"],
                        "; ".join(ai["name"] or "" for ai in ais),
                        "; ".join(ai["strength"] or "" for ai in ais),
                        p["dosage_form"],
                        p["route"],
                        p["marketing_status"],
                        p["te_code"],
                    )
                )
            )
    return "\n".join(lines)
