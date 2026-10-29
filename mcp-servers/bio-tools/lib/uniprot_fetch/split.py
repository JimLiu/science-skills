"""Split multi-record UniProtKB responses back into per-accession records."""
from __future__ import annotations


def fasta_accession(header: str) -> str:
    """Primary accession from a UniProt FASTA header line (``>sp|P04637|P53_HUMAN ...``)."""
    parts = header[1:].split("|")
    if len(parts) >= 3 and parts[0] in ("sp", "tr"):
        return parts[1]

    return header[1:].split()[0]


def split_fasta(text: str) -> dict[str, str]:
    """Split a multi-record FASTA string into ``{primary_accession: record_text}``.

    Record text is preserved as returned by the server (original line wrapping),
    with a single trailing newline.
    """
    records: dict[str, str] = {}
    header: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if header is not None:
            records[fasta_accession(header)] = "\n".join([header] + lines) + "\n"

    for line in text.splitlines():
        if line.startswith(">"):
            flush()
            header = line
            lines = []
        elif line:
            lines.append(line)
    flush()
    return records


def split_flatfile(text: str) -> dict[str, str]:
    """Split concatenated UniProtKB flat-file text into ``{primary_accession: entry_text}``.

    Entries are terminated by a line that is exactly ``//``.
    """
    records: dict[str, str] = {}
    entry_lines: list[str] = []
    for line in text.splitlines():
        entry_lines.append(line)
        if line.strip() == "//":
            entry = "\n".join(entry_lines) + "\n"
            acc = flatfile_primary_accession(entry)
            if acc:
                records[acc] = entry
            entry_lines = []
    return records


def flatfile_primary_accession(entry_text: str) -> str | None:
    """Primary accession = first accession on the first ``AC`` line of the entry."""
    for line in entry_text.splitlines():
        if line.startswith("AC   "):
            first = line[5:].strip().split(";")[0].strip()
            return first or None
    return None
