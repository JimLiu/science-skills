"""Parse KEGG flat-file records.

KEGG flat files use a 12-column keyword field; continuation lines start with
12 spaces, and a record is terminated by a line containing only '///'. A
batched /get response is the concatenation of such records.

``parse_fields`` is a faithful but flat field split (nested sub-keywords inside
REFERENCE blocks, e.g. AUTHORS/TITLE/JOURNAL, surface as their own keys; they
are not used by the structured record). ``parse_entry`` extracts the compact
structured record exposed by the tool: entry id/type, names, gene symbols,
definition/description, organism, formula, pathway list, orthology list.
"""

from __future__ import annotations

from typing import Dict, List

FIELD_WIDTH = 12
TERMINATOR = "///"


def split_flat(text: str) -> List[str]:
    """Split a (possibly batched) KEGG /get response into per-entry flat-file texts.

    Each returned chunk keeps its terminating '///' line and ends with a newline.
    KEGG's batched /get response inserts one blank line between consecutive records
    (after '///', before the next ENTRY); that separator belongs to the batch
    envelope, not to either record, so leading blank lines are not attributed to
    the following record.
    """
    chunks: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if not current and not line.strip():

            continue
        current.append(line)
        if line.strip() == TERMINATOR:
            chunks.append("\n".join(current) + "\n")
            current = []
    if any(line.strip() for line in current):

        chunks.append("\n".join(current) + "\n")
    return chunks


def parse_fields(entry_text: str) -> Dict[str, List[str]]:
    """Parse top-level flat-file fields into an order-preserving {KEYWORD: [value lines]}."""
    fields: Dict[str, List[str]] = {}
    current_key: str | None = None
    for line in entry_text.splitlines():
        if line.strip() == TERMINATOR:
            break
        if not line.strip():
            continue
        keyword = line[:FIELD_WIDTH].strip()
        value = line[FIELD_WIDTH:].rstrip()
        if keyword:
            current_key = keyword
            fields.setdefault(current_key, [])
        if current_key is None:
            continue
        fields[current_key].append(value)
    return fields


def _parse_id_name_lines(lines: List[str]) -> List[Dict[str, str]]:
    """Parse 'ID  description' value lines (PATHWAY, ORTHOLOGY, ...) into dicts."""
    out: List[Dict[str, str]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        out.append({"id": parts[0], "name": parts[1].strip() if len(parts) == 2 else ""})
    return out


def parse_entry(entry_text: str) -> dict:
    """Parse one KEGG flat-file record into a compact structured dict."""
    fields = parse_fields(entry_text)



    entry_tokens = fields.get("ENTRY", [""])[0].split()
    entry_id = entry_tokens[0] if entry_tokens else ""
    entry_type = entry_tokens[1] if len(entry_tokens) > 1 else ""

    names: List[str] = []
    for line in fields.get("NAME", []):
        for piece in line.split(";"):
            piece = piece.strip()


            if piece.startswith("(RefSeq)"):
                piece = piece[len("(RefSeq)"):].strip()
            if piece:
                names.append(piece)

    symbols: List[str] = []
    for line in fields.get("SYMBOL", []):
        symbols.extend(s.strip() for s in line.split(",") if s.strip())

    def joined(key: str) -> str | None:
        value = " ".join(ln.strip() for ln in fields.get(key, [])).strip()
        return value or None

    definition = joined("DEFINITION") or joined("DESCRIPTION")

    return {
        "entry": entry_id,
        "entry_id": entry_id,
        "entry_type": entry_type,
        "name": names,
        "symbol": symbols,
        "definition": definition,
        "organism": joined("ORGANISM"),
        "formula": joined("FORMULA"),
        "pathway": _parse_id_name_lines(fields.get("PATHWAY", [])),
        "orthology": _parse_id_name_lines(fields.get("ORTHOLOGY", [])),
    }
