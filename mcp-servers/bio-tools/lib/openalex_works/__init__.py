"""openalex-works — OpenAlex scholarly-works retrieval (bio-tools fleet).

Covers the works / authors / sources surface of the OpenAlex REST API
(citation graph, OA status, abstract reconstruction from the inverted
index). Authenticated: OpenAlex requires an API key on every request (the
anonymous polite pool and ``mailto`` are retired); this package sends it as
a bearer header. The key comes from the ``OPENALEX_API_KEY`` env injected by
the host; a missing key raises ``mcp_servers_common.ua.OpenAlexKeyRequired``.
"""
from .client import OpenAlexClient, OpenAlexApiError, NotFound
from .records import (lean_author, lean_source, lean_work,
                      normalize_author_id, normalize_source_id,
                      normalize_work_id, reconstruct_abstract)
from .tool import OpenAlexWorks

__all__ = [
    "OpenAlexClient", "OpenAlexApiError", "NotFound", "OpenAlexWorks",
    "lean_work", "lean_author", "lean_source", "reconstruct_abstract",
    "normalize_work_id", "normalize_author_id", "normalize_source_id",
]
__version__ = "0.1.0"
