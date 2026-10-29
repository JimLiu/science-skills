"""Download / cache layer for the PanglaoDB bulk marker TSV.

PanglaoDB's web server returns HTTP 403 to non-browser User-Agents; this
client identifies itself honestly rather than spoofing a browser, so the
download only works if the operator permits the UA (see CLIENT_UA note).
The marker table is versioned and frozen upstream (last update 27 Mar 2020),
which makes it safe to checksum-pin and cache indefinitely.
"""
from __future__ import annotations

import hashlib
import os
import pathlib

import requests

MARKER_URL = "https://panglaodb.se/markers/PanglaoDB_markers_27_Mar_2020.tsv.gz"



MARKER_SHA256 = "6779952ad40aa5a124de7bd0e18975c6630bd6006d6b3ef210a916caaa6b53c9"







CLIENT_UA = "bio-tools-panglaodb-markers/1.0 (anthropic-experimental/bio-tools)"

_FILENAME = "PanglaoDB_markers_27_Mar_2020.tsv.gz"


class ChecksumError(RuntimeError):
    """Downloaded/cached file does not match the pinned sha256."""


def default_cache_dir() -> pathlib.Path:
    env = os.environ.get("PANGLAODB_CACHE")
    if env:
        return pathlib.Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".cache"
    return base / "panglaodb-markers"


def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_markers_gz(
    cache_dir: str | os.PathLike | None = None,
    *,
    force: bool = False,
    verify_checksum: bool = True,
    session: requests.Session | None = None,
    timeout: float = 120.0,
) -> pathlib.Path:
    """Return a local path to the marker TSV (.gz), downloading at most once.

    A cached copy is re-used if its sha256 matches the pinned value (or if
    verification is disabled). Downloads are written atomically.
    """
    cdir = pathlib.Path(cache_dir) if cache_dir is not None else default_cache_dir()
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / _FILENAME

    if path.exists() and not force:
        if not verify_checksum or _sha256_file(path) == MARKER_SHA256:
            return path


    getter = session if session is not None else requests
    resp = getter.get(MARKER_URL, headers={"User-Agent": CLIENT_UA}, timeout=timeout)
    resp.raise_for_status()
    blob = resp.content
    digest = hashlib.sha256(blob).hexdigest()
    if verify_checksum and digest != MARKER_SHA256:
        raise ChecksumError(
            f"PanglaoDB marker file checksum mismatch: expected {MARKER_SHA256}, "
            f"got {digest} ({len(blob)} bytes). Upstream file is supposed to be "
            "static since 2020 - refusing to use it."
        )
    tmp = path.with_suffix(".gz.tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return path
