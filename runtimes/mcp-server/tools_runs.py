#!/usr/bin/env python3
"""
tools_runs.py — discovery: which distilled specifications exist?

WHY THIS IS THE ENTRY POINT
---------------------------
Every other tool needs a `run_dir`. Without discovery an agent cannot begin — a human
has to paste a path first, which defeats the point of exposing the spec to an agent at
all. This walks a runs root and reports what is there, so the agent can pick one.

It also reports whether each run is actually USABLE, which is not the same as present:
a directory can hold a half-finished run, or one whose identity gate found the source
had changed underneath it. Reporting `complete` and `identity` lets a caller avoid
querying a run whose answers would be stale or partial.

Pure reads of run.json / run_status.json / .identity_verdict.json. No model calls.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from spec_tooling import err, ok, quiet

# Searched in order when no root is passed. DISTILL_RUNS_ROOT wins so an operator can
# point the server at wherever runs actually live.
_DEFAULT_ROOTS = ("output", "outputs")


def _runs_root(explicit: str) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    env = os.environ.get("DISTILL_RUNS_ROOT", "")
    if env and Path(env).expanduser().is_dir():
        return Path(env).expanduser()
    # Relative to the package root (this file is runtimes/mcp-server/…).
    pkg = Path(__file__).resolve().parents[2]
    for name in _DEFAULT_ROOTS:
        if (pkg / name).is_dir():
            return pkg / name
    return None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _describe(d: Path) -> dict | None:
    """Summarise one candidate run directory, or None if it is not a run at all."""
    run = _read_json(d / "run.json")
    vectors = d / "stills" / "shared_vectors.db"
    # A directory counts as a run if it has either a manifest or a vector store: the
    # manifest alone means it started, the store alone means it predates run.json.
    if not run and not vectors.is_file():
        return None

    status = _read_json(d / "run_status.json")
    identity = _read_json(d / ".identity_verdict.json")
    stills_dir = d / "stills"
    stills = sorted(
        p.name for p in stills_dir.iterdir()
        if stills_dir.is_dir() and p.is_dir() and not p.name.startswith(".")
    ) if stills_dir.is_dir() else []

    return {
        "run_dir": str(d),
        "run_id": run.get("run_id", ""),
        "target": (run.get("env_snapshot", {}) or {}).get("DISTILL_TARGET_NAME")
                  or (run.get("env_snapshot", {}) or {}).get("DISTILL_TARGET", ""),
        "created_at": run.get("created_at", ""),
        "tool_version": run.get("tool_version", ""),
        "model_id": (run.get("params", {}) or {}).get("model_id", ""),
        "stills": stills,
        "status": status.get("status", "unknown"),
        "failed_stages": status.get("failed_stages", []),
        "complete": status.get("status") == "complete",
        "identity": identity.get("state", ""),
        "queryable": vectors.is_file(),
    }


def register(server):
    @server.tool(
        description=(
            "List the distilled specification runs available to query. CALL THIS FIRST "
            "if you do not already have a `run_dir` — every other tool in this server "
            "needs one. Free; no AWS credentials needed.\n\n"
            "For each run: `run_dir` (pass this to the other tools), target system, "
            "creation time, which extraction stills ran, and model id.\n\n"
            "Check two fields before trusting a run's answers:\n"
            "  `complete`  false means the pipeline did not finish, so parts of the "
            "spec may be missing. `failed_stages` lists what broke.\n"
            "  `queryable` false means there is no vector store, so the graph and Q&A "
            "tools cannot read it.\n"
            "`identity` reports the resume gate's verdict; `source_mismatch` means the "
            "source tree changed after this run, so its answers describe older code.\n\n"
            "Searches `root` if given, else $DISTILL_RUNS_ROOT, else output/ in the "
            "package."
        )
    )
    @quiet
    async def list_runs(root: str = "") -> str:
        """Discover completed distillation runs under a runs root."""
        base = _runs_root(root)
        if base is None:
            return err(
                "no runs root found — pass `root`, or set DISTILL_RUNS_ROOT, or create "
                "output/ in the package root",
                searched=[root] if root else list(_DEFAULT_ROOTS),
            )
        try:
            runs = [r for r in (_describe(d) for d in sorted(base.iterdir()) if d.is_dir())
                    if r is not None]
        except OSError as e:
            return err(f"{type(e).__name__}: {e}", root=str(base))

        # Newest first, so the most relevant run leads.
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return ok({
            "root": str(base),
            "count": len(runs),
            "queryable_count": sum(1 for r in runs if r["queryable"]),
            "runs": runs,
        })
