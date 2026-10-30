#!/usr/bin/env python3
"""
spec-query MCP Server — read-only access to a completed distilled specification.

Started via MCP configuration (stdio transport). Tools live in `tools_*.py` modules
beside this file and are discovered automatically, so this file does not change when
tools are added — see spec_tooling.py for the contract.

WHY A SEPARATE SERVER FROM server.py
------------------------------------
server.py ("bedrock-distill") is a PRODUCTION server: its tools submit map/reduce jobs
that cost real money, and its own descriptions warn they are "invoked ONLY by the Phase
2.5 orchestrator" and "must NEVER be called by distill-worker agents". This server is
for CONSUMPTION — an agent reading a finished spec while it works. Keeping them apart
means a consuming agent cannot see, let alone call, a tool that launches a distillation
run. That is a structural guarantee rather than a warning an agent has to read
correctly.

NO BEDROCK CLIENT AT IMPORT TIME
--------------------------------
server.py builds its Bedrock clients on import. Here nothing does: ask_spec constructs
what it needs when called, so every other tool works with NO AWS credentials configured
at all. A developer can register this server and start querying immediately.

Every tool takes `run_dir` (the directory holding run.json) and resolves artifact paths
internally via run_manager, so callers never pass database paths (G7). Start with
`list_runs` if you do not have a run_dir yet.

AUTO-DISCOVERY
--------------
Every `tools_*.py` module exposing `register(server)` is loaded. Adding a tool is adding
a file — nobody edits this module, so several people can work in parallel without
conflicting here. A module that fails to import is reported on stderr and skipped: one
broken tool area must not take the whole server down, since the others may be all a
caller needs.
"""

import importlib
import sys
from pathlib import Path

from mcp.server import FastMCP

server = FastMCP("spec-query")


def _load_tool_modules(srv) -> list[str]:
    """Import every tools_*.py beside this file and let it register its tools."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    loaded = []
    for path in sorted(here.glob("tools_*.py")):
        name = path.stem
        try:
            mod = importlib.import_module(name)
            register = getattr(mod, "register", None)
            if register is None:
                print(f"[spec-query] {name}: no register(server), skipped", file=sys.stderr)
                continue
            register(srv)
            loaded.append(name)
        except Exception as e:
            # stderr, never stdout: stdout is the JSON-RPC channel.
            print(f"[spec-query] {name}: FAILED to load — {type(e).__name__}: {e}",
                  file=sys.stderr)
    return loaded


_LOADED = _load_tool_modules(server)
print(f"[spec-query] tool modules loaded: {', '.join(_LOADED) or '(none)'}",
      file=sys.stderr)


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    server.run(transport="stdio")
