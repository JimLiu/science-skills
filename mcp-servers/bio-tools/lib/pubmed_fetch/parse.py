"""Parse PubMed XML (PubmedArticle elements) into structured JSON-able records.

Record schema (all keys always present):
    pmid        str
    title       str | None     (ArticleTitle, inline markup flattened to text)
    journal     str | None     (Journal/Title)
    year        int | None     (JournalIssue/PubDate/Year, else first 4-digit year in MedlineDate)
    doi         str | None     (ArticleIdList ArticleId[@IdType="doi"], else ELocationID[@EIdType="doi"])
    abstract    str | None     (AbstractText sections joined; labelled sections as "LABEL: text")
    mesh_terms  list[str]      (MeshHeading DescriptorName texts, document order)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _text(el: ET.Element | None) -> str | None:
    """Flatten an element's full text content (including inline markup like <i>, <sup>)."""
    if el is None:
        return None
    txt = "".join(el.itertext()).strip()
    return txt or None


def _parse_year(article_el: ET.Element) -> int | None:
    pubdate = article_el.find("MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if pubdate is None:
        return None
    year = pubdate.findtext("Year")
    if year and year.strip().isdigit():
        return int(year.strip())
    medline_date = pubdate.findtext("MedlineDate") or ""
    m = _YEAR_RE.search(medline_date)
    return int(m.group(1)) if m else None


def _parse_doi(article_el: ET.Element) -> str | None:
    for aid in article_el.findall("PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi" and (aid.text or "").strip():
            return aid.text.strip()
    for eloc in article_el.findall("MedlineCitation/Article/ELocationID"):
        if eloc.get("EIdType") == "doi" and (eloc.text or "").strip():
            return eloc.text.strip()
    return None


def _parse_abstract(article_el: ET.Element) -> str | None:
    sections = []
    for abst in article_el.findall("MedlineCitation/Article/Abstract/AbstractText"):
        text = _text(abst)
        if not text:
            continue
        label = (abst.get("Label") or "").strip()
        sections.append(f"{label}: {text}" if label else text)
    return "\n".join(sections) or None


def _parse_mesh(article_el: ET.Element) -> list[str]:
    terms = []
    for heading in article_el.findall("MedlineCitation/MeshHeadingList/MeshHeading"):
        name = _text(heading.find("DescriptorName"))
        if name:
            terms.append(name)
    return terms


def parse_article(article_el: ET.Element) -> dict:
    """Parse a single <PubmedArticle> element into a structured record."""
    if article_el.tag == "PubmedArticleSet":

        children = list(article_el)
        if len(children) != 1:
            raise ValueError("parse_article expects a single article element")
        article_el = children[0]
    return {
        "pmid": (article_el.findtext("MedlineCitation/PMID")
                 or article_el.findtext(".//PMID") or "").strip(),
        "title": _text(article_el.find("MedlineCitation/Article/ArticleTitle")),
        "journal": _text(article_el.find("MedlineCitation/Article/Journal/Title")),
        "year": _parse_year(article_el),
        "doi": _parse_doi(article_el),
        "abstract": _parse_abstract(article_el),
        "mesh_terms": _parse_mesh(article_el),
    }


def parse_articleset(xml_text: str) -> list[dict]:
    """Parse a full PubmedArticleSet XML document into a list of structured records."""
    root = ET.fromstring(xml_text)
    if root.tag != "PubmedArticleSet":
        raise ValueError(f"expected PubmedArticleSet root, got {root.tag}")
    return [parse_article(child) for child in root if child.find(".//PMID") is not None]
