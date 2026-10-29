"""Structured section extraction from JATS full-text XML (Europe PMC fullTextXML).

Extracted per article:
  * title
  * abstract (plain text, structured-abstract section titles kept inline)
  * top-level body sections in document order: sec_type, title, IMRaD class,
    paragraph count, character count, plain text
  * figure captions and table captions (label + caption text, document order)
  * reference count (//back ref-list ref elements)
"""
from __future__ import annotations

import re
import unicodedata

from lxml import etree




_WS_RE = re.compile(r"\s+")


def _text(elem: etree._Element | None, exclude_tags: tuple[str, ...] = ()) -> str:
    """Whitespace-normalized text content of an element (all descendants),
    optionally excluding subtrees by tag name."""
    if elem is None:
        return ""
    if exclude_tags:
        elem = _copy_without(elem, exclude_tags)
    return _WS_RE.sub(" ", "".join(elem.itertext())).strip()


def _copy_without(elem: etree._Element, exclude_tags: tuple[str, ...]) -> etree._Element:
    import copy

    clone = copy.deepcopy(elem)
    for tag in exclude_tags:
        for node in clone.findall(f".//{tag}"):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
    return clone


def normalize_abstract(text: str) -> str:
    """Canonical form used to compare abstracts across routes (JATS XML vs the
    /search endpoint's abstractText, which may carry HTML tags, different
    casing of structured-abstract labels and different punctuation/whitespace).

    Steps: strip markup tags, NFKC-normalize unicode, lowercase, drop every
    non-alphanumeric character. No words are dropped or rewritten.
    """
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    return re.sub(r"[^a-z0-9]+", "", text)





_IMRAD_RULES: list[tuple[str, str]] = [
    (r"^(introduction|background)\b", "introduction"),
    (r"^(materials?\s+and\s+methods?|methods?|methodology|experimental\s+procedures?|"
     r"star\s*★?\s*methods|online\s+methods|patients?\s+and\s+methods?)\b", "methods"),
    (r"^results?\s+and\s+discussion\b", "results_and_discussion"),
    (r"^results?\b|^findings\b", "results"),
    (r"^discussion\b", "discussion"),
    (r"^(conclusions?|concluding\s+remarks|summary)\b", "conclusion"),
]

_SEC_TYPE_MAP = {
    "intro": "introduction",
    "introduction": "introduction",
    "methods": "methods",
    "materials|methods": "methods",
    "materials-and-methods": "methods",
    "results": "results",
    "results|discussion": "results_and_discussion",
    "discussion": "discussion",
    "conclusions": "conclusion",
    "conclusion": "conclusion",
    "supplementary-material": "supplementary",
}


def classify_section(title: str, sec_type: str | None) -> str:
    """Map a top-level body section to an IMRaD class.

    @sec-type wins when it maps cleanly; otherwise the section title is
    matched against conservative regexes; everything else is 'other'.
    """
    if sec_type:
        mapped = _SEC_TYPE_MAP.get(sec_type.strip().lower())
        if mapped:
            return mapped
    t = (title or "").strip().lower()
    t = re.sub(r"^[0-9ivx]+[.):\s]+\s*", "", t)
    for pattern, label in _IMRAD_RULES:
        if re.search(pattern, t):
            return label
    return "other"





def extract_sections(xml_bytes: bytes, include_section_text: bool = True) -> dict:
    """Parse JATS XML and return the structured article representation."""
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False,
                             no_network=True, load_dtd=False)
    root = etree.fromstring(xml_bytes, parser=parser)
    if root is None:
        raise ValueError("could not parse JATS XML")

    front_meta = root.find(".//front/article-meta")


    title_el = root.find(".//front/article-meta/title-group/article-title")
    title = _text(title_el)



    abstract_text = ""
    if front_meta is not None:
        abstracts = front_meta.findall("abstract")
        main = [a for a in abstracts if not a.get("abstract-type")]
        chosen = main[0] if main else (abstracts[0] if abstracts else None)
        if chosen is not None:



            import copy as _copy

            chosen = _copy.deepcopy(chosen)
            for child in list(chosen):
                if child.tag == "title" and _text(child).strip().lower() == "abstract":
                    chosen.remove(child)



            for kwd_sec in chosen.findall(".//sec[@sec-type='kwd-group']"):
                parent = kwd_sec.getparent()
                if parent is not None:
                    parent.remove(kwd_sec)
            abstract_text = _text(chosen, exclude_tags=("kwd-group",))


    sections = []
    body = root.find(".//body")
    if body is not None:
        exclude = ("table-wrap", "fig", "ref-list")
        for i, sec in enumerate(body.findall("sec")):
            sec_type = sec.get("sec-type")
            sec_title = _text(sec.find("title"))
            sec_plain = _text(sec, exclude_tags=exclude)
            paragraphs = sec.findall(".//p")
            entry = {
                "index": i,
                "sec_type": sec_type,
                "title": sec_title,
                "imrad": classify_section(sec_title, sec_type),
                "n_paragraphs": len(paragraphs),
                "n_chars": len(sec_plain),
            }
            if include_section_text:
                entry["text"] = sec_plain
            sections.append(entry)


    figure_captions = []
    for fig in root.iter("fig"):
        figure_captions.append({
            "id": fig.get("id"),
            "label": _text(fig.find("label")),
            "caption": _text(fig.find("caption")),
        })
    table_captions = []
    for tw in root.iter("table-wrap"):
        table_captions.append({
            "id": tw.get("id"),
            "label": _text(tw.find("label")),
            "caption": _text(tw.find("caption")),
        })




    n_references = sum(
        1 for ref in root.iter("ref")
        if any(anc.tag == "ref-list" for anc in ref.iterancestors())
    )

    return {
        "title": title,
        "abstract": abstract_text,
        "sections": sections,
        "section_inventory": [
            {"title": s["title"], "imrad": s["imrad"]} for s in sections
        ],
        "figure_captions": figure_captions,
        "table_captions": table_captions,
        "n_figures": len(figure_captions),
        "n_tables": len(table_captions),
        "n_references": n_references,
    }
