#!/usr/bin/env python3
"""still_registry.py — the engine's own registry of STILLS, resolved by NAME (key).

WHY (door-3 packaging; SYSTEM_SPEC §7 registries; L7 class/instance). A still's prompt is not
runtime input to be located and injected by every caller — it is ENGINE SOURCE that ships with
the MCP server, exactly like `orchestration_strategy.py` or `plate_strategy.py`. So the map/reduce
surface takes a still NAME (a key) and the server resolves name→prompt itself. No prompt text
crosses the MCP boundary; a caller (kiro-cli today, anything tomorrow) needs to know only names —
which are just the `still_specs/<name>/` directory names. This is the same idiom already used by
`bottling.py` (resolve its prompt relative to the server module's own location).

STILL SPECS ROOT (resolved, not injected): the packaged `still_specs/` tree, found relative to
this module's own path (`<repo>/runtimes/mcp-server/still_registry.py` → `<repo>/still_specs`),
with a cwd-relative fallback for in-tree runs. Overridable via DISTILL_STILL_SPECS_DIR for tests,
but never REQUIRED — the point is the engine knows where its own stills live.

An "extraction still" is a `still_specs/<name>/` dir with a `prompt.md` that is NOT one of the
meta/synthesis stills (conventions, bottling, reconciliation, master_blend, artifact_selector,
domain_partitioner, cross_domain_blend, redistillation) — the exact exclusion set the orchestrator
used to hardcode in shell (run_distillation.sh), now owned here as data.

CLI:
    python still_registry.py list                 # extraction still names (newline-sep)
    python still_registry.py prompt <name>        # full prompt text for one still
    python still_registry.py root                 # resolved still_specs root
    python still_registry.py selftest             # self-checks
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


# Meta/synthesis stills that are NOT extraction stills (the orchestrator's historical skip set).
_NON_EXTRACTION = frozenset({
    "00_conventions", "bottling", "reconciliation", "master_blend",
    "artifact_selector", "domain_partitioner", "cross_domain_blend", "redistillation",
})

# Conventions fallback used when a still has no prompt.md of its own (the shell's old fallback).
_CONVENTIONS_FALLBACK = ("00_conventions", "formal_representation.md")


def still_specs_root() -> Path:
    """Resolve the packaged still_specs/ root.

    Precedence:
      1. DISTILL_STILL_SPECS_DIR env (tests / non-standard layouts)
      2. packaged location relative to THIS module (<repo>/still_specs), the bottling.py idiom
      3. cwd-relative ./still_specs (in-tree run fallback)
    The first existing candidate wins; if none exist, the packaged path is returned anyway so the
    error surfaced downstream names the location the engine EXPECTS its stills to be packaged at.
    """
    env = os.environ.get("DISTILL_STILL_SPECS_DIR", "").strip()
    packaged = Path(__file__).resolve().parent.parent.parent / "still_specs"
    cwd_rel = Path("still_specs")
    candidates = [Path(env)] if env else []
    candidates += [packaged, cwd_rel]
    for c in candidates:
        if c.is_dir():
            return c
    return packaged


def prompt_path(name: str) -> Path:
    """Path to a still's prompt.md (may not exist — see prompt_for for the fallback)."""
    return still_specs_root() / name / "prompt.md"


def prompt_for(name: str) -> str:
    """Full prompt text for a still, resolved by NAME.

    Reads `<root>/<name>/prompt.md`. If that is missing, falls back to the conventions
    `formal_representation.md` (the orchestrator's historical fallback). Raises FileNotFoundError
    with the resolved paths if neither exists — fail loud, never return an empty/vapor prompt.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("still name must be a non-empty string")
    p = prompt_path(name)
    if p.is_file():
        return p.read_text(encoding="utf-8")
    fallback = still_specs_root() / _CONVENTIONS_FALLBACK[0] / _CONVENTIONS_FALLBACK[1]
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"no prompt for still {name!r}: tried {p} and fallback {fallback}"
    )


def is_extraction_still(name: str) -> bool:
    """True if `name` is an extraction still (has a prompt.md and is not a meta/synthesis still)."""
    if name in _NON_EXTRACTION or name.startswith("."):
        return False
    return (still_specs_root() / name / "prompt.md").is_file()


def list_extraction_stills() -> list[str]:
    """The extraction still names, sorted — the auto-discovery the orchestrator used to do in
    shell, now owned here as data. Excludes meta/synthesis stills and dirs lacking a prompt.md."""
    root = still_specs_root()
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and is_extraction_still(child.name):
            names.append(child.name)
    return names


def resolve_stills(names: list[str]) -> list[tuple[str, str]]:
    """Resolve a list of still NAMES to (name, prompt_text) pairs via the registry.

    This is the boundary function the map/reduce handlers call: callers pass names, the server
    resolves the prompt bodies. An empty/None list resolves to ALL extraction stills (the
    orchestrator's auto-discover default), so an invoker can ask for "everything" by name-absence.
    """
    if not names:
        names = list_extraction_stills()
    return [(n, prompt_for(n)) for n in names]


def _selftest() -> int:
    root = still_specs_root()
    print(f"still_specs_root = {root}  (exists={root.is_dir()})")
    stills = list_extraction_stills()
    print(f"extraction stills ({len(stills)}): {stills}")
    assert all(s not in _NON_EXTRACTION for s in stills), "meta still leaked into extraction set"
    if stills:
        n = stills[0]
        body = prompt_for(n)
        assert isinstance(body, str) and len(body) > 0, f"empty prompt for {n}"
        print(f"prompt_for({n!r}) -> {len(body)} chars OK")
        pairs = resolve_stills([n])
        assert pairs == [(n, body)], "resolve_stills mismatch for single name"
        allpairs = resolve_stills([])
        assert [p[0] for p in allpairs] == stills, "resolve_stills([]) != all extraction stills"
        print(f"resolve_stills([]) -> {len(allpairs)} stills OK")
    # unknown still fails loud (unless conventions fallback exists — then it returns that)
    try:
        prompt_for("__definitely_not_a_still__")
        fb = still_specs_root() / _CONVENTIONS_FALLBACK[0] / _CONVENTIONS_FALLBACK[1]
        assert fb.is_file(), "unknown still did not raise and no conventions fallback present"
        print("unknown still -> conventions fallback (present) OK")
    except FileNotFoundError:
        print("unknown still -> FileNotFoundError OK")
    print("SELFTEST OK")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Still registry: resolve stills by name")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="print extraction still names")
    p = sub.add_parser("prompt", help="print a still's prompt text")
    p.add_argument("name")
    sub.add_parser("root", help="print the resolved still_specs root")
    sub.add_parser("selftest", help="run self-checks")
    args = ap.parse_args()

    if args.cmd == "list":
        print("\n".join(list_extraction_stills()))
        return 0
    if args.cmd == "prompt":
        sys.stdout.write(prompt_for(args.name))
        return 0
    if args.cmd == "root":
        print(still_specs_root())
        return 0
    if args.cmd == "selftest":
        return _selftest()
    return 1


if __name__ == "__main__":
    sys.exit(main())
