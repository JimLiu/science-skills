"""Dispatch-layer blank-criterion guard for tier-2 (FastMCP) servers
(operon-authored NEW module — register like gate.py/criteria.py in
VENDOR.json; a re-vendor's rmtree of mcp_servers_common deletes it and must
re-add).

Why a dispatch layer: #4141 added per-site guards for every
review-named blank-criterion lane, and three review rounds each found new
instances anyway (openalex_search_works was fixed twice in that PR and
still dropped a blank work_type one parameter over). The post-merge census
(2026-07-06: all 17 touched servers, 196 tools, ~422 string criteria traced
wrapper→client→wire) found 26 further tier-2 lanes — every one entering
through a tool argument. Per-site guarding does not converge; making every
tool blank-rejecting BY CONSTRUCTION does.

``GuardedFastMCP`` is a drop-in ``FastMCP`` subclass: construction is the
one-line adoption (``mcp = GuardedFastMCP("mcp-expression")``) and its
``.tool()`` wraps every registered tool (sync or async) with a
signature-bound walk that raises ``criteria.py``'s standard ValueError for
any provided-but-blank criterion:

* a blank ``str`` argument ("" or whitespace-only);
* a blank ``str`` entry of a list/tuple argument (it would otherwise ride
  upstream inside a comma-join or vanish from a filter conjunction);
* a blank key or blank ``str`` value of a dict argument (the shared
  ``reject_blank`` helper checks dict VALUES but not KEYS — ``{'': 'x'}``
  passed; census researcher-3 finding).

The walk recurses through nested list/dict values and runs BEFORE the
tool body, so no blank reaches a client lane. What it sees depends on the
caller: a direct in-process call is signature-bound, so only the arguments
actually passed are walked; on the real FastMCP dispatch path the pydantic
argument model materializes every default and passes ALL parameters, so
defaults are walked there too. No guarded tool has a blank-ish default —
if one ever grows one, every dispatch fails closed and loud, which is the
intended polarity.

Per-parameter opt-outs keep the documented exceptions explicit at the
registration site:

* ``blank_as_absent={"query"}`` — ``none_if_blank`` semantics: the blank
  becomes ``None`` before the tool body (documented-browse tools where
  blank-means-omitted is the #4141-reviewed behavior, e.g.
  ``openalex_search_works``);
* ``allow_blank={"sep"}`` — the WHOLE parameter is exempt, container
  contents included (e.g. ``pdb_get_structures(pdb_ids=...)``, whose
  blank entries are skipped with an n_blank_skipped disclosure by the tool
  itself). By contrast ``blank_as_absent`` is str-only semantics: a
  non-blank value — including any container — still gets the full walk.

Opt-out names are validated against the function signature at registration
time, so a typo'd opt-out fails at import, not silently in production.
"""

from __future__ import annotations

import functools
import inspect
import typing
from collections.abc import Iterable, Mapping

from mcp.server.fastmcp import FastMCP

from .criteria import blank, reject_blank

__all__ = ["GuardedFastMCP"]


def _walk(path: str, value: object, bad: list[str]) -> None:
    """Collect the display names of every blank criterion under ``value``."""
    if isinstance(value, str):
        if not value.strip():
            bad.append(path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and not key.strip():
                bad.append(f"{path} key {key!r}")
            _walk(f"{path}[{key!r}]", item, bad)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _walk(f"{path}[{i}]", item, bad)


def _guard_blank_criteria(fn, allow_blank: frozenset[str],
                          blank_as_absent: frozenset[str]):
    """Wrap ``fn`` so every call is blank-checked before the body runs.

    The wrapper binds the original signature (so direct in-process calls
    with positional args are guarded identically to FastMCP dispatch, which
    passes keywords) and preserves it for FastMCP's schema generation
    (``inspect.signature`` follows ``__wrapped__``; ``functools.wraps``
    keeps the docstring the schema description comes from).
    """
    sig = inspect.signature(fn, eval_str=True)
    unknown = (allow_blank | blank_as_absent) - set(sig.parameters)
    if unknown:
        raise ValueError(
            f"{fn.__name__}: no parameter named {sorted(unknown)} — "
            "allow_blank/blank_as_absent must name real parameters")
    overlap = allow_blank & blank_as_absent
    if overlap:
        raise ValueError(
            f"{fn.__name__}: {sorted(overlap)} in both allow_blank and "
            "blank_as_absent — pick one semantics per parameter")
    for name in blank_as_absent:




        param = sig.parameters[name]
        ann = param.annotation
        permits_none = (
            param.default is None
            or ann is inspect.Parameter.empty
            or ann is type(None)
            or type(None) in typing.get_args(ann))
        if not permits_none:
            raise ValueError(
                f"{fn.__name__}: blank_as_absent param {name!r} is neither "
                "Optional nor None-defaulted — the blank would become a None "
                "the body can't accept; use allow_blank or widen the "
                "annotation")

    def _check(args: tuple, kwargs: dict):
        bound = sig.bind(*args, **kwargs)
        bad: list[str] = []
        for name, value in bound.arguments.items():
            if name in allow_blank:
                continue
            if name in blank_as_absent:
                if blank(value):
                    bound.arguments[name] = None
                    continue



            _walk(name, value, bad)
        if bad:
            reject_blank({name: "" for name in bad})
        return bound

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def guarded(*args, **kwargs):
            bound = _check(args, kwargs)
            return await fn(*bound.args, **bound.kwargs)
    else:
        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            bound = _check(args, kwargs)
            return fn(*bound.args, **bound.kwargs)

    return guarded


class GuardedFastMCP(FastMCP):
    """A FastMCP whose every tool rejects provided-but-blank criteria."""

    def tool(self, *args, allow_blank: Iterable[str] = (),
             blank_as_absent: Iterable[str] = (), **kwargs):
        base = super().tool(*args, **kwargs)
        allow = frozenset(allow_blank)
        absent = frozenset(blank_as_absent)

        def register(fn):
            return base(_guard_blank_criteria(fn, allow, absent))

        return register
