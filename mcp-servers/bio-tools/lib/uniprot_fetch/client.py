"""Batched UniProtKB retrieval via the REST search endpoint."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable

from .http import InstrumentedSession
from .split import split_fasta, split_flatfile

BASE = "https://rest.uniprot.org/uniprotkb"
SEARCH_URL = f"{BASE}/search"
STREAM_URL = f"{BASE}/stream"

_SPLITTERS: dict[str, Callable[[str], dict[str, str]]] = {
    "fasta": split_fasta,
    "txt": split_flatfile,
}












ACCESSION_RE = re.compile(
    r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"
    r"(?:-[0-9]+)?",
    re.IGNORECASE | re.ASCII,
)


def _checked(accessions: Iterable[str]) -> list[str]:
    """Return ``accessions`` as a list, raising ValueError on any value that is
    not a well-formed UniProtKB accession (see ACCESSION_RE above)."""
    accs = list(accessions)
    bad = [a for a in accs if not (isinstance(a, str) and ACCESSION_RE.fullmatch(a))]
    if bad:
        shown = ", ".join(repr(b) for b in bad[:5])
        more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
        raise ValueError(
            f"not valid UniProtKB accessions: {shown}{more} — accession values "
            "are interpolated into a UniProt search query, so only the "
            "documented accession format is accepted (e.g. 'P04637', "
            "'A0A024R161', isoform 'P04637-2')")
    return accs


class UniProtClient:
    """Batched UniProtKB client.

    Instead of one request per accession per format (the legacy pattern), accessions are
    combined into ``accession:`` OR-queries against ``/uniprotkb/search`` (``size=500``,
    cursor pagination via the ``Link`` header, gzip transfer encoding), and the
    multi-record response is split back into per-accession records returned in the
    original input order. Accessions that return no record are reported explicitly.

    Accession values are validated against the documented UniProtKB format before
    query construction (see ACCESSION_RE); anything else raises ValueError.
    """

    def __init__(
        self,
        session: InstrumentedSession | None = None,
        batch_size: int = 100,
        page_size: int = 500,
        endpoint: str = "search",
    ):
        if endpoint not in ("search", "stream"):
            raise ValueError("endpoint must be 'search' or 'stream'")
        self.session = session or InstrumentedSession()
        self.batch_size = batch_size
        self.page_size = page_size
        self.endpoint = endpoint



    @staticmethod
    def _query_for(accessions: Iterable[str]) -> str:
        return " OR ".join(f"accession:{a}" for a in _checked(accessions))

    def _search_paged(self, query: str, fmt: str, fields: str | None = None) -> str:
        """Run one query, following cursor pagination; return the concatenated body text."""
        if self.endpoint == "stream":
            params = {"query": query, "format": fmt}
            if fields:
                params["fields"] = fields
            text, _ = self.session.get_text(STREAM_URL, params=params)
            return text

        params: dict | None = {"query": query, "format": fmt, "size": str(self.page_size)}
        if fields:
            params["fields"] = fields
        chunks: list[str] = []
        url = SEARCH_URL
        while True:
            text, resp = self.session.get_text(url, params=params)
            chunks.append(text)
            nxt = resp.links.get("next", {}).get("url")
            if not nxt:
                break
            url, params = nxt, None
        return "".join(chunks)

    def _fetch_split(self, accessions: list[str], fmt: str) -> tuple[dict[str, str], list[str]]:
        accessions = _checked(accessions)
        splitter = _SPLITTERS[fmt]
        merged: dict[str, str] = {}
        for i in range(0, len(accessions), self.batch_size):
            chunk = accessions[i : i + self.batch_size]
            merged.update(splitter(self._search_paged(self._query_for(chunk), fmt)))
        ordered = {a: merged[a] for a in accessions if a in merged}
        missing = [a for a in accessions if a not in merged]
        return ordered, missing



    def fetch_fasta(self, accessions: list[str]) -> tuple[dict[str, str], list[str]]:
        """FASTA per accession. Returns ``(records_in_input_order, missing_accessions)``."""
        return self._fetch_split(list(accessions), "fasta")

    def fetch_flatfile(self, accessions: list[str]) -> tuple[dict[str, str], list[str]]:
        """Full flat-file (txt) per accession. Returns ``(records, missing)``."""
        return self._fetch_split(list(accessions), "txt")

    def fetch_fields(self, accessions: list[str], fields: list[str], fmt: str = "tsv") -> str:
        """Field-selected retrieval (``tsv`` or ``json``) for when full entries are not needed."""
        accessions = _checked(accessions)
        out = [
            self._search_paged(
                self._query_for(accessions[i : i + self.batch_size]), fmt, fields=",".join(fields)
            )
            for i in range(0, len(accessions), self.batch_size)
        ]
        return "".join(out)

    def fetch_all(
        self,
        accessions: list[str],
        outdir: str | Path | None = None,
        formats: tuple[str, ...] = ("fasta", "txt"),
    ) -> dict:
        """Fetch the requested formats for all accessions; optionally write per-accession files.

        Returns a manifest::

            {"accessions": [...], "formats": [...],
             "records": {fmt: {acc: text}}, "missing": {fmt: [...]},
             "http": {"requests": n, "bytes_downloaded": n, "retries": n}}
        """
        accessions = _checked(accessions)
        records: dict[str, dict[str, str]] = {}
        missing: dict[str, list[str]] = {}
        for fmt in formats:
            if fmt not in _SPLITTERS:
                raise ValueError(f"unsupported format: {fmt!r} (supported: fasta, txt)")
            records[fmt], missing[fmt] = self._fetch_split(accessions, fmt)

        if outdir is not None:
            outdir = Path(outdir)
            for fmt in formats:
                d = outdir / fmt
                d.mkdir(parents=True, exist_ok=True)
                for acc in accessions:
                    if acc in records[fmt]:
                        (d / f"{acc}.{fmt}").write_text(records[fmt][acc], encoding="utf-8")

        return {
            "accessions": accessions,
            "formats": list(formats),
            "records": records,
            "missing": missing,
            "http": {
                "requests": self.session.stats.requests,
                "bytes_downloaded": self.session.stats.bytes_downloaded,
                "retries": self.session.stats.retries,
            },
        }
