"""URL path-segment hygiene (operon-authored NEW module — registered like
criteria.py/gate.py in VENDOR.json; a re-vendor's rmtree of
mcp_servers_common deletes it and must re-add).

The bug class (review #4672 follow-up): ``quote(..., safe="")`` keeps dots
verbatim (they are unreserved), and both ``requests`` and ``httpx`` remove
dot-segments client-side before the request leaves the process — so an id of
exactly "." or ".." collapses an item route onto its collection/list route
(e.g. ``/projects/..`` becomes the API root, ``/projects/.`` the project
listing). Empty ids collapse the same way via a trailing slash.
"""

from __future__ import annotations

import urllib.parse

__all__ = ["seg"]


def seg(value: object, safe: str = "") -> str:
    """Percent-encode one URL path segment, rejecting values that cannot
    name an item: empty or dots-only strings ("", ".", "..", ...) would
    re-target the request onto a different route."""
    s = str(value)
    if s.strip(".") == "":
        raise ValueError(
            f"invalid value {s!r}: empty or dots-only values cannot form "
            "a URL path segment")
    return urllib.parse.quote(s, safe=safe)
