"""Record marshalling for OpenAlex entities.

OpenAlex raw records are huge (topics, concepts, SDGs, per-year counts...).
The fleet returns lean, stable records; the headline marshalling step is
``reconstruct_abstract`` — OpenAlex ships abstracts only as an inverted
index ``{word: [positions...]}`` and the plain text must be rebuilt.
"""
from __future__ import annotations

import re

_OPENALEX_URL = re.compile(r"^https?://openalex\.org/(?P<id>[WASIPFC]\d+)$",
                           re.IGNORECASE)
_DOI_URL = re.compile(r"^https?://(dx\.)?doi\.org/(?P<doi>10\..+)$",
                      re.IGNORECASE)
_ISSN = re.compile(r"^\d{4}-\d{3}[\dXx]$")
_ORCID = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dXx]$")


def reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Rebuild plain abstract text from OpenAlex's inverted index.

    The index maps each word to the list of token positions where it
    occurs; sorting positions and emitting the word at each one restores
    the original token order. Returns None when the index is absent
    (OpenAlex omits abstracts it may not redistribute).
    """
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def _short_id(url_or_id: str | None) -> str | None:
    """https://openalex.org/W123 -> W123 (pass bare IDs through)."""
    if not url_or_id:
        return None
    return url_or_id.rsplit("/", 1)[-1]


def _url_id(m: "re.Match[str]", want: str) -> str | None:
    """Entity-typed URL branch: _OPENALEX_URL matches
    all seven entity letters, but each normalizer must only accept its own
    type — a concept URL (C…) passed as a source would otherwise return a
    confident api_total=0 instead of the ValueError the bare id raises."""
    ident = m.group("id").upper()
    return ident if ident.startswith(want) else None


def _checked_doi(doi: str, original: str) -> str:
    """OpenAlex splits filter values on ',' and '|' with no escaping:
    a comma becomes a bogus second filter (errors a
    valid DOI) and a pipe becomes an OR that _resolve_doi_work silently
    collapses to the most-cited claimant — reject both outright."""
    if "," in doi or "|" in doi:
        raise ValueError(
            f"unsupported DOI {original!r}: ',' and '|' cannot be expressed "
            f"in an OpenAlex filter query")
    return doi


def normalize_work_id(work_id: str) -> str:
    """Normalize a work reference to an API path segment.

    Accepts a bare OpenAlex ID (``W2981137429``), an OpenAlex URL, a bare
    DOI (``10.1038/s41586-019-1711-4``) or a DOI URL. DOIs are passed to
    the API as ``doi:...`` aliases.
    """
    wid = work_id.strip()
    m = _OPENALEX_URL.match(wid)
    if m:
        ident = _url_id(m, "W")
        if ident:
            return ident
        raise ValueError(
            f"unrecognized work id {work_id!r} — that openalex.org URL is "
            f"not a W (work) entity")
    m = _DOI_URL.match(wid)
    if m:
        return f"doi:{_checked_doi(m.group('doi'), work_id)}"



    cand = wid[4:].strip() if wid.lower().startswith("doi:") else wid
    if cand.startswith("10.") and "/" in cand:
        return f"doi:{_checked_doi(cand, work_id)}"
    if re.fullmatch(r"[Ww]\d+", wid):
        return wid.upper()
    raise ValueError(
        f"unrecognized work id {work_id!r} — pass an OpenAlex W-id, an "
        f"openalex.org URL, or a DOI")


def normalize_author_id(author_id: str) -> str:
    """Normalize an author reference (A-id, OpenAlex URL, or ORCID)."""
    aid = author_id.strip()
    m = _OPENALEX_URL.match(aid)
    if m:
        ident = _url_id(m, "A")
        if ident:
            return ident
        raise ValueError(
            f"unrecognized author id {author_id!r} — that openalex.org URL "
            f"is not an A (author) entity")
    if re.fullmatch(r"[Aa]\d+", aid):
        return aid.upper()



    cand = aid
    if cand.lower().startswith("orcid:"):
        cand = cand[6:].strip()
    cand = re.sub(r"^https?://orcid\.org/", "", cand)
    if _ORCID.fullmatch(cand):
        return f"orcid:{cand}"
    raise ValueError(
        f"unrecognized author id {author_id!r} — pass an OpenAlex A-id, an "
        f"openalex.org URL, or an ORCID")


def normalize_source_id(source_id: str) -> str:
    """Normalize a source reference (S-id, OpenAlex URL, or ISSN)."""
    sid = source_id.strip()
    m = _OPENALEX_URL.match(sid)
    if m:
        ident = _url_id(m, "S")
        if ident:
            return ident
        raise ValueError(
            f"unrecognized source id {source_id!r} — that openalex.org URL "
            f"is not an S (source) entity")
    if re.fullmatch(r"[Ss]\d+", sid):
        return sid.upper()



    cand = sid
    if cand.lower().startswith("issn:"):
        cand = cand[5:].strip()
    if _ISSN.match(cand):
        return f"issn:{cand.upper()}"
    raise ValueError(
        f"unrecognized source id {source_id!r} — pass an OpenAlex S-id, an "
        f"openalex.org URL, or an ISSN like 1087-0156")


def _lean_authorship(a: dict) -> dict:
    author = a.get("author") or {}
    return {
        "author_id": _short_id(author.get("id")),
        "name": author.get("display_name"),
        "orcid": author.get("orcid"),
        "position": a.get("author_position"),
        "is_corresponding": a.get("is_corresponding"),
        "institutions": [i.get("display_name")
                         for i in a.get("institutions") or []],
    }







OPEN_ABSTRACT_LICENSES = frozenset({"cc-by", "cc-by-sa", "cc0", "public-domain"})


def work_license(w: dict) -> str | None:
    """The work's own license key (e.g. 'cc-by'), or None when undeclared.

    Prefers primary_location, falls back to best_oa_location. Normalizes
    the long URL form (https://openalex.org/licenses/cc-by) to the bare key.
    """
    for loc_key in ("primary_location", "best_oa_location"):
        lic = ((w.get(loc_key) or {}).get("license") or "").strip().lower()
        if lic:
            return lic.rsplit("/", 1)[-1]
    return None


def lean_work(w: dict, with_abstract: bool = True) -> dict:
    """Flatten one raw OpenAlex work into a lean, stable record."""
    ids = w.get("ids") or {}
    primary = w.get("primary_location") or {}
    source = primary.get("source") or {}
    oa = w.get("open_access") or {}
    best_oa = w.get("best_oa_location") or {}
    topic = w.get("primary_topic") or {}
    rec = {
        "openalex_id": _short_id(w.get("id")),
        "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
        "pmid": _short_id(ids.get("pmid")),
        "title": w.get("title") or w.get("display_name"),
        "publication_year": w.get("publication_year"),
        "publication_date": w.get("publication_date"),
        "type": w.get("type"),
        "language": w.get("language"),
        "is_retracted": w.get("is_retracted"),
        "authors": [_lean_authorship(a) for a in w.get("authorships") or []],
        "source": {
            "source_id": _short_id(source.get("id")),
            "display_name": source.get("display_name"),
            "issn_l": source.get("issn_l"),
            "type": source.get("type"),
        } if source else None,
        "biblio": w.get("biblio"),
        "cited_by_count": w.get("cited_by_count"),
        "fwci": w.get("fwci"),
        "referenced_works_count": w.get("referenced_works_count"),
        "open_access": {
            "is_oa": oa.get("is_oa"),
            "oa_status": oa.get("oa_status"),
            "oa_url": oa.get("oa_url"),
        },
        "best_oa_pdf_url": best_oa.get("pdf_url"),
        "primary_topic": topic.get("display_name"),
        "keywords": [k.get("display_name")
                     for k in w.get("keywords") or []] or None,
    }
    if with_abstract:
        lic = work_license(w)
        if lic in OPEN_ABSTRACT_LICENSES:
            rec["abstract"] = reconstruct_abstract(
                w.get("abstract_inverted_index"))
            rec["abstract_license"] = lic
        else:






            rec["abstract"] = None




            rec["abstract_license"] = lic
            rec["abstract_policy"] = (
                "omitted: work license is "
                + (repr(lic) if lic else "not declared")
                + " — not verified-open, so the abstract is not "
                "reconstructed; read it at the DOI / landing page")
    return rec


def lean_author(a: dict) -> dict:
    """Flatten one raw OpenAlex author record."""
    stats = a.get("summary_stats") or {}
    return {
        "author_id": _short_id(a.get("id")),
        "name": a.get("display_name"),
        "orcid": a.get("orcid"),
        "works_count": a.get("works_count"),
        "cited_by_count": a.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "affiliations": [{
            "institution": (i.get("institution") or {}).get("display_name"),
            "years": i.get("years"),
        } for i in (a.get("affiliations") or [])[:10]],
        "last_known_institutions": [
            i.get("display_name")
            for i in a.get("last_known_institutions") or []],
        "top_topics": [t.get("display_name")
                       for t in (a.get("topics") or [])[:5]],
    }


def lean_source(s: dict) -> dict:
    """Flatten one raw OpenAlex source (journal/repository) record."""
    stats = s.get("summary_stats") or {}
    return {
        "source_id": _short_id(s.get("id")),
        "display_name": s.get("display_name"),
        "type": s.get("type"),
        "issn_l": s.get("issn_l"),
        "issn": s.get("issn"),
        "host_organization": s.get("host_organization_name"),
        "country_code": s.get("country_code"),
        "homepage_url": s.get("homepage_url"),
        "is_oa": s.get("is_oa"),
        "is_in_doaj": s.get("is_in_doaj"),
        "is_core": s.get("is_core"),
        "apc_usd": s.get("apc_usd"),
        "works_count": s.get("works_count"),
        "cited_by_count": s.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "two_year_mean_citedness": stats.get("2yr_mean_citedness"),
        "first_publication_year": s.get("first_publication_year"),
        "last_publication_year": s.get("last_publication_year"),
        "top_topics": [t.get("display_name")
                       for t in (s.get("topics") or [])[:5]],
    }
