#!/usr/bin/env python3
"""Launcher for the vendored bio-tools MCP servers (stdio).

Usage: python run_server.py <package>   e.g. python run_server.py mcp_pubmed

Registered in the host's bundled-MCP registry as
  command: "python", args: ["${MCP_SERVERS_DIR}/bio-tools/run_server.py", "<pkg>"]
MCPPool resolves `python` to the shared bundled-MCP conda env and substitutes
the staged-assets path; deps come from the registry entry's installPip pins.

All packages (servers + the fleet retrieval packages they import) live flat
under lib/ next to this file — no pip install of the vendored code itself.
"""

import importlib
import sys
from pathlib import Path


def main() -> None:
    lib = Path(__file__).resolve().parent / "lib"





    servers = sorted(
        p.name for p in lib.iterdir()
        if p.name.startswith("mcp_") and (p / "server.py").is_file()
    )
    if len(sys.argv) != 2 or sys.argv[1] not in servers:
        sys.stderr.write(
            "usage: run_server.py <server package>\n"
            f"valid: {', '.join(servers)}\n")
        raise SystemExit(2)
    sys.path.insert(0, str(lib))












    try:
        from mcp_servers_common import tls_policy
    except ImportError as exc:



        missing = getattr(exc, "name", None) or str(exc)
        if missing in ("mcp_servers_common", "mcp_servers_common.tls_policy"):
            detail = f"policy module missing: {missing}"
        else:
            detail = f"dependency missing while loading the policy: {missing}"
        sys.stderr.write(
            f"[tls-policy] x509-strict=unavailable ({detail})\n")
    else:
        if tls_policy.apply_posture() == tls_policy.NOT_ARMED:
            sys.stderr.write(
                "[tls-policy] x509-strict=not-armed (relax requested, but this "
                "Python never arms the check)\n")


    from mcp_servers_common import never_trust, ua
    never_trust.install()
    ua.install_user_agent()
    mod = importlib.import_module(f"{sys.argv[1]}.server")
    mod.main()


if __name__ == "__main__":
    main()
