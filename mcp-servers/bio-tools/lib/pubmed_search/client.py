"""pubmed-search: PubMed discovery utilities over NCBI E-utilities + PMC ID Converter.

Covers the four mcp-pubmed methods NOT covered by the record-retrieval tool
``pubmed-fetch``:

- ``search``            (esearch, full retstart walk, count-verified)
- ``citmatch``          (ecitmatch, batched pipe-delimited citation matching)
- ``convert_ids``       (PMC ID Converter: PMID <-> PMCID <-> DOI)
- ``copyright_status``  (efetch db=pubmed CopyrightInformation + db=pmc <permissions>)

Politeness: a configurable sleep before EVERY request (default 0.5 s => <= 2 req/s),
tool/email identification, retries with exponential backoff.

KNOWN UPSTREAM GOTCHA: under per-IP rate limiting, eutils can return HTTP 200 with a
JSON body ``{"error": "API rate limit exceeded", ...}`` on ANY endpoint. All retry
logic here therefore inspects response BODIES, not just status codes.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from mcp_servers_common.ratelimit import pace

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"

ESEARCH_RETSTART_CEILING = 10_000
IDCONV_BATCH = 200
EFETCH_BATCH = 200

_SORT_MAP = {

    "relevance": "relevance",
    "pub_date": "pub_date",
    "author": "Author",
    "journal_name": "JournalName",
    "title": "title",
}


class PubMedSearchError(Exception):
    """Raised on upstream errors, exhausted retries, or integrity-check failures."""


def _is_rate_limit_body(text: str) -> bool:
    """Detect NCBI's rate-limit-inside-HTTP-200 failure mode.

    Example body: {"error":"API rate limit exceeded","api-key":"1.2.3.4",...}
    """
    head = text[:1000].lower()
    return "api rate limit exceeded" in head or "too many requests" in head


class PubMedSearch:
    def __init__(
        self,
        email: str,
        tool: str = "bio-tools-pubmed-search",
        api_key: str | None = None,
        sleep_s: float = 0.5,
        max_retries: int = 4,
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        self.email = email
        self.tool = tool
        self.api_key = api_key
        self.sleep_s = sleep_s
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", f"{tool}/1.0 (mailto:{email})"
        )

        self.request_count = 0
        self.bytes_downloaded = 0
        self.request_log: list[dict[str, Any]] = []



    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "PubMedSearch":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _ident(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        out.setdefault("tool", self.tool)
        out.setdefault("email", self.email)
        if self.api_key:
            out.setdefault("api_key", self.api_key)
        return out

    def _request(self, url: str, params: dict[str, Any]) -> requests.Response:
        """POST with politeness sleep, body-aware rate-limit retry, backoff.

        POST (params in the body) like the sibling pubmed_fetch client —
        NCBI E-utilities accept it, and it keeps api_key out of the URL so
        transport-error reprs can never embed the shared secret.
        """
        last_err: str = "no attempt made"
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(self.sleep_s * (2**attempt))


            pace(url, self.sleep_s)
            try:
                resp = self.session.post(
                    url, data=self._ident(params), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_err = f"transport error: {exc!r}"
                continue
            self.request_count += 1
            body = resp.text
            self.bytes_downloaded += len(resp.content)
            self.request_log.append(
                {"url": url, "status": resp.status_code, "bytes": len(resp.content)}
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {resp.status_code}"
                continue

            if _is_rate_limit_body(body):
                last_err = "rate-limit body in HTTP %d response" % resp.status_code
                continue
            if resp.status_code != 200:
                raise PubMedSearchError(
                    f"HTTP {resp.status_code} from {url}: {body[:300]}"
                )
            return resp
        raise PubMedSearchError(
            f"exhausted {self.max_retries} retries for {url}: {last_err}"
        )



    def search_page(
        self,
        term: str,
        retstart: int = 0,
        retmax: int = 20,
        datetype: str | None = None,
        mindate: str | None = None,
        maxdate: str | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Single bounded esearch page — no full walk, no ceiling raise.

        Returns ``{"count": int, "pmids": [str, ...], "query_translation"}``
        where ``pmids`` is just this page. Total hit count comes from the
        page's own ``Count`` field, so arbitrarily large result sets are fine
        (esearch itself cannot page past retstart+retmax=10,000).

        Operon vendored-copy addition: the tier-1
        mcp-pubmed drop-in needs hosted-connector parity (first page +
        total_count/has_more) — ``search()``'s count-verified full walk
        raises on >10k-hit queries, which includes the schema's own examples.
        """
        if not term or not term.strip():
            raise PubMedSearchError("empty search term")
        params: dict[str, Any] = {
            "db": "pubmed", "term": term, "retmode": "json",
            "retstart": retstart, "retmax": retmax,
        }
        if datetype:
            params["datetype"] = datetype
        if mindate:
            params["mindate"] = mindate
        if maxdate:
            params["maxdate"] = maxdate
        if sort:
            params["sort"] = _SORT_MAP.get(sort, sort)
        resp = self._request(f"{EUTILS_BASE}/esearch.fcgi", params)
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise PubMedSearchError(f"non-JSON esearch response: {exc}") from exc
        res = payload.get("esearchresult")
        if res is None:
            raise PubMedSearchError(f"malformed esearch payload: {str(payload)[:300]}")
        if "ERROR" in res:
            raise PubMedSearchError(f"esearch error: {res['ERROR']}")
        return {
            "count": int(res["count"]),
            "pmids": res.get("idlist", []),
            "query_translation": res.get("querytranslation"),
        }

    def search(
        self,
        term: str,
        datetype: str | None = None,
        mindate: str | None = None,
        maxdate: str | None = None,
        sort: str | None = None,
        page_size: int = 1000,
        max_records: int = ESEARCH_RETSTART_CEILING,
    ) -> dict[str, Any]:
        """Full esearch UID walk, count-verified.

        Returns ``{"count": int, "pmids": [str, ...], "query_translation": str}``.
        ``pmids`` is the complete result set (every page walked via retstart),
        verified against the API's own ``Count`` field; duplicates and short
        retrievals raise ``PubMedSearchError``.

        esearch cannot page past retstart+retmax=10,000; result sets larger than
        ``max_records`` raise with guidance to slice by EDAT/PDAT windows.
        """
        if not term or not term.strip():
            raise PubMedSearchError("empty search term")
        base: dict[str, Any] = {"db": "pubmed", "term": term, "retmode": "json"}
        if datetype:
            base["datetype"] = datetype
        if mindate:
            base["mindate"] = mindate
        if maxdate:
            base["maxdate"] = maxdate
        if sort:
            base["sort"] = _SORT_MAP.get(sort, sort)

        pmids: list[str] = []
        count: int | None = None
        translation: str | None = None
        retstart = 0
        while True:
            params = dict(base, retstart=retstart, retmax=page_size)
            resp = self._request(f"{EUTILS_BASE}/esearch.fcgi", params)
            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                raise PubMedSearchError(f"non-JSON esearch response: {exc}") from exc
            res = payload.get("esearchresult")
            if res is None:
                raise PubMedSearchError(f"malformed esearch payload: {str(payload)[:300]}")
            if "ERROR" in res:
                raise PubMedSearchError(f"esearch error: {res['ERROR']}")
            if count is None:
                count = int(res["count"])
                translation = res.get("querytranslation")
                if count > max_records:
                    raise PubMedSearchError(
                        f"result set has {count} records (> max_records="
                        f"{max_records}); esearch cannot page past retstart 10,000 — "
                        "slice the query into date windows (datetype='edat' or 'pdat' "
                        "with mindate/maxdate) and merge."
                    )
            pmids.extend(res.get("idlist", []))
            retstart += page_size
            if retstart >= count or not res.get("idlist"):
                break

        if count is None:
            raise PubMedSearchError("no esearch response received")
        if len(pmids) != len(set(pmids)):
            raise PubMedSearchError(
                f"duplicate PMIDs in walk: {len(pmids)} rows, {len(set(pmids))} unique"
            )
        if len(pmids) != count:
            raise PubMedSearchError(
                f"retrieved {len(pmids)} PMIDs but esearch Count={count}"
            )
        return {"count": count, "pmids": pmids, "query_translation": translation}



    @staticmethod
    def _citation_to_bdata(c: dict[str, Any], i: int) -> tuple[str, str]:
        key = str(c.get("key") or f"cit{i}")
        if "|" in key:
            raise PubMedSearchError(f"citation key may not contain '|': {key!r}")
        fields = [
            str(c.get("journal", "") or ""),
            str(c.get("year", "") or ""),
            str(c.get("volume", "") or ""),
            str(c.get("first_page", "") or ""),
            str(c.get("author", "") or ""),
            key,
        ]
        for f in fields[:5]:
            if "|" in f:
                raise PubMedSearchError(f"citation field may not contain '|': {f!r}")
        return key, "|".join(fields) + "|"

    def citmatch(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Batched ecitmatch. One request for the whole list.

        Each citation: ``{journal, year, volume, first_page, author, key?}``.
        Returns, in input order:
        ``{"key", "citation_string", "pmid" | None, "status": "found"|"not_found"|"ambiguous"|...}``.
        """
        if not citations:
            return []
        keyed = [self._citation_to_bdata(c, i) for i, c in enumerate(citations)]
        bdata = "\r".join(line for _, line in keyed)
        resp = self._request(
            f"{EUTILS_BASE}/ecitmatch.cgi",
            {"db": "pubmed", "retmode": "xml", "bdata": bdata},
        )


        by_key: dict[str, dict[str, Any]] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 7:
                continue
            key = parts[5]
            result = parts[6].strip()
            entry: dict[str, Any] = {"key": key, "citation_string": "|".join(parts[:6]) + "|"}
            if re.fullmatch(r"\d+", result):
                entry["pmid"] = result
                entry["status"] = "found"
            elif result.upper().startswith("AMBIGUOUS"):
                entry["pmid"] = None
                entry["status"] = "ambiguous"
                entry["detail"] = result
            else:
                entry["pmid"] = None
                entry["status"] = "not_found"
                entry["detail"] = result
            by_key[key] = entry
        out = []
        for key, line in keyed:
            out.append(
                by_key.get(
                    key,
                    {
                        "key": key,
                        "citation_string": line,
                        "pmid": None,
                        "status": "no_response_line",
                    },
                )
            )
        return out



    def convert_ids(
        self, ids: list[str], from_type: str = "pmid"
    ) -> list[dict[str, Any]]:
        """PMC ID Converter: PMID <-> PMCID <-> DOI.

        ``from_type`` in {"pmid", "pmcid", "doi"}; one request per batch of <= 200.
        NOTE: the converter requires HOMOGENEOUS id types per request (mixed-type
        requests are HTTP 400 upstream) — hence the explicit ``from_type``.

        Returns, in input order:
        ``{"requested_id", "pmid" | None, "pmcid" | None, "doi" | None,
           "status": "ok"|"error", "errmsg"?}``.
        ``status=="error"`` with "not found in PMC" is the normal outcome for
        PMIDs of articles with no PMC deposit.
        """
        if from_type not in ("pmid", "pmcid", "doi"):
            raise PubMedSearchError(f"from_type must be pmid|pmcid|doi, got {from_type!r}")
        records: list[dict[str, Any]] = []
        for i in range(0, len(ids), IDCONV_BATCH):
            batch = ids[i : i + IDCONV_BATCH]
            resp = self._request(
                IDCONV_URL,
                {
                    "ids": ",".join(batch),
                    "idtype": from_type,
                    "format": "json",
                    "versions": "no",
                },
            )
            payload = resp.json()
            if payload.get("status") == "error":
                raise PubMedSearchError(f"idconv error: {str(payload)[:300]}")
            raw_by_req = {
                str(r.get("requested-id")): r for r in payload.get("records", [])
            }
            for rid in batch:
                raw = raw_by_req.get(str(rid))
                if raw is None:
                    records.append(
                        {
                            "requested_id": rid,
                            "pmid": None,
                            "pmcid": None,
                            "doi": None,
                            "status": "error",
                            "errmsg": "no record in idconv response",
                        }
                    )
                    continue
                rec: dict[str, Any] = {
                    "requested_id": rid,
                    "pmid": str(raw["pmid"]) if raw.get("pmid") is not None else None,
                    "pmcid": raw.get("pmcid"),
                    "doi": raw.get("doi"),
                    "status": "error" if raw.get("status") == "error" else "ok",
                }
                if raw.get("errmsg"):
                    rec["errmsg"] = raw["errmsg"]
                records.append(rec)
        return records



    @staticmethod
    def _localname(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _pubmed_copyright(self, pmids: list[str]) -> dict[str, str | None]:
        """CopyrightInformation per PMID from efetch db=pubmed XML."""
        out: dict[str, str | None] = {p: None for p in pmids}
        for i in range(0, len(pmids), EFETCH_BATCH):
            batch = pmids[i : i + EFETCH_BATCH]
            resp = self._request(
                f"{EUTILS_BASE}/efetch.fcgi",
                {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
            )
            root = ET.fromstring(resp.content)
            for art in root.iter("PubmedArticle"):
                pmid_el = art.find(".//MedlineCitation/PMID")
                if pmid_el is None or not pmid_el.text:
                    continue
                ci = art.find(".//Abstract/CopyrightInformation")
                out[pmid_el.text.strip()] = (
                    ci.text.strip() if ci is not None and ci.text else None
                )
        return out

    def _pmc_permissions(
        self, pmcids_by_pmid: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        """<permissions> block per PMID from efetch db=pmc XML."""
        out: dict[str, dict[str, Any]] = {}
        numeric = [
            (pmid, pmcid.replace("PMC", "")) for pmid, pmcid in pmcids_by_pmid.items()
        ]
        for i in range(0, len(numeric), EFETCH_BATCH):
            batch = numeric[i : i + EFETCH_BATCH]
            resp = self._request(
                f"{EUTILS_BASE}/efetch.fcgi",
                {"db": "pmc", "id": ",".join(n for _, n in batch), "retmode": "xml"},
            )
            root = ET.fromstring(resp.content)
            for article in root:
                if self._localname(article.tag) != "article":
                    continue
                ids: dict[str, str] = {}
                perm: dict[str, Any] = {
                    "copyright_statement": None,
                    "copyright_year": None,
                    "license_type": None,
                    "license_ref": None,
                }
                for el in article.iter():
                    ln = self._localname(el.tag)
                    if ln == "article-id":
                        ids[el.get("pub-id-type", "")] = (el.text or "").strip()
                    elif ln == "permissions" and perm["copyright_statement"] is None:
                        for sub in el.iter():
                            sln = self._localname(sub.tag)
                            if sln == "copyright-statement" and sub.text:
                                perm["copyright_statement"] = sub.text.strip()
                            elif sln == "copyright-year" and sub.text:
                                perm["copyright_year"] = sub.text.strip()
                            elif sln == "license":
                                perm["license_type"] = sub.get("license-type") or perm[
                                    "license_type"
                                ]
                            elif sln == "license_ref" and sub.text:
                                if perm["license_ref"] is None:
                                    perm["license_ref"] = sub.text.strip()
                pmid = ids.get("pmid")
                if pmid:
                    out[pmid] = perm
        return out

    def copyright_status(self, pmids: list[str]) -> list[dict[str, Any]]:
        """License/copyright per PMID.

        Combines (1) PubMed ``CopyrightInformation`` (efetch db=pubmed),
        (2) the PMC ID Converter (PMID -> PMCID), and (3) the PMC
        ``<permissions>`` block (efetch db=pmc): copyright statement/year,
        ``license-type`` attribute, and the ALI ``license_ref`` URL.

        ``source`` is "pmc" when a PMC permissions block was found, "pubmed"
        when only a PubMed copyright line exists, else "not_available".
        """
        pmids = [str(p) for p in pmids]
        pubmed_ci = self._pubmed_copyright(pmids)
        conv = self.convert_ids(pmids, "pmid")
        pmcids_by_pmid = {
            r["requested_id"]: r["pmcid"]
            for r in conv
            if r["status"] == "ok" and r.get("pmcid")
        }
        pmc_perm = self._pmc_permissions(pmcids_by_pmid) if pmcids_by_pmid else {}
        out = []
        for pmid in pmids:
            perm = pmc_perm.get(pmid)
            ci = pubmed_ci.get(pmid)
            rec: dict[str, Any] = {
                "pmid": pmid,
                "pmcid": pmcids_by_pmid.get(pmid),
                "pubmed_copyright": ci,
                "pmc": perm,
                "source": "pmc" if perm else ("pubmed" if ci else "not_available"),
            }
            out.append(rec)
        return out
