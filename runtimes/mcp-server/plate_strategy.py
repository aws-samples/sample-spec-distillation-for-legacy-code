#!/usr/bin/env python3
"""plate_strategy.py — pluggable PLATE STRATEGY for page building (the distillation column's
tray positioning). One abstraction; the current behavior is its degenerate single-tray case.

WHY (SYSTEM_SPEC §7.2 tray layout; L6 identity; L7 class/instance; L8 provenance). Page building
feeds the stills. HOW files are racked onto pages ("plates") shapes what each still reads — a
still that reads a module-COHESIVE page fractionates cleanly; one that reads a proximity-packed
mix fuses cross-module signal (the observed epic-anneal defect). The plate strategy is therefore
a first-class, NAMED, pluggable capability — not one implicit behavior baked into the packer.

The strategy is a pure transform on the file MANIFEST ORDER consumed by concat-batch.sh:
    strategy(manifest_paths, ctx) -> reordered manifest_paths
concat-batch.sh (unchanged) then fills pages in that order to the token budget. So a strategy
changes ONLY the order/grouping of files into pages — never their content — and the seam is tiny.

REGISTRY (framework/System code, L7 class — generic, no per-run specifics):
  single-plate         — identity order (one flat tray, everything stacked in manifest order).
                         The current/inferred behavior, named. Degenerate case (N=1 plate).
                         Built → maturity COVERED.
  dependency-cohesive  — reorder so same-module files are contiguous (module inferred
                         deterministically from path structure; a later refinement feeds the
                         declarative topology graph). Real fractionation. Built here → COVERED.

SELECTION precedence (resolved per run, recorded into the run manifest — L7 instance / L8):
  1. run manifest's recorded plate_strategy   (HIGHEST — an existing run is reproducible; a
     run that recorded 'single-plate' ALWAYS re-resolves to it, even after the default flips)
  2. explicit orchestrator/CLI pass-in        (a new run overrides the default)
  3. registry effective-default              (LOWEST — the best BUILT strategy marked default)

IDENTITY NOTE (L6, not a defect): block_id embeds the page, so two DIFFERENT plate strategies
produce different pages → disjoint, each-internally-stable id spines. That is EXPECTED and made
interpretable by recording plate_strategy in the manifest — a strategy's re-run is stable
(deterministic reorder + reused pages); cross-strategy id-disjointness is correct (different
method = different derivation), never a silent surprise.

CLI (used by map-reduce-still.sh as a manifest pre-transform):
    python plate_strategy.py reorder <strategy> <manifest_in> <manifest_out> [--source-root R]
    python plate_strategy.py resolve <manifest_json_or_-> [--passin S]   # prints effective strategy
    python plate_strategy.py registry                                    # prints the registry
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict


# ── module inference (deterministic, source-agnostic) ────────────────────────────────────────
# A file's "module" is inferred from its path structure — the most stable, universally-available
# grouping signal (no topology graph required for the first built strategy). We take the LONGEST
# stable directory prefix below the source root, minus leaf/language noise, as the module key.
# Source-agnostic: no hardcoded module names; the grouping key is derived from the tree itself.
def infer_module(rel_path: str) -> str:
    p = rel_path.replace("\\", "/").strip("/")
    parts = p.split("/")
    if len(parts) <= 1:
        return "(root)"
    # drop the filename; the directory path IS the module locus. Collapse case so
    # Foo/ and foo/ group together (filesystems differ), but keep the structure.
    dirs = [d for d in parts[:-1] if d not in (".", "..")]
    return "/".join(d.lower() for d in dirs) if dirs else "(root)"


# ── the strategies (each: manifest_paths, ctx -> reordered manifest_paths) ────────────────────
def _single_plate(paths: list[str], ctx: dict) -> list[str]:
    """Identity: leave manifest order as-is. One flat tray (the degenerate/baseline case)."""
    return list(paths)


def _dependency_cohesive(paths: list[str], ctx: dict) -> list[str]:
    """Reorder so files of the same inferred module are CONTIGUOUS, so the budget-fill packs
    module-cohesive pages. Deterministic + stable: modules are ordered by (descending file count,
    then module key) and files within a module keep their original relative order. A pure
    permutation of the input — same set of files, no addition/loss (L2), just grouped."""
    by_module: dict[str, list[str]] = defaultdict(list)
    first_seen: dict[str, int] = {}
    for i, path in enumerate(paths):
        m = infer_module(path)
        by_module[m].append(path)
        first_seen.setdefault(m, i)
    # order modules: larger modules first (they fill whole plates), then by key for determinism
    ordered_modules = sorted(by_module, key=lambda m: (-len(by_module[m]), m))
    out: list[str] = []
    for m in ordered_modules:
        out.extend(by_module[m])  # within-module order preserved (stable)
    return out


# name -> (fn, maturity, is_default_intent, description)
REGISTRY = {
    "single-plate": (_single_plate, "covered", False,
                     "identity order; one flat tray (degenerate/baseline case)"),
    "dependency-cohesive": (_dependency_cohesive, "covered", True,
                            "reorder so same-module files are contiguous (module from path "
                            "structure); module-cohesive pages"),
}


def code_hash(strategy: str) -> str:
    """sha256 of the SOURCE CODE that implements a plate strategy — its behavioral identity.

    Detects algorithm DRIFT UNDER A STABLE NAME: if `_dependency_cohesive` (or the shared
    `infer_module` that shapes racking) is edited, this hash flips even though the strategy name
    is unchanged — so a run-identity resume gate can refuse to treat a re-coded strategy as the
    same one. Complements (does not replace) the palm print: palm = "did the pages change" (the
    dish); this = "did the racking algorithm change" (the recipe). A recipe edit that happens to
    yield the same dish for this corpus is still a provenance change, caught only here.

    Covered set (EXPLICIT, documented — not auto closure-walked): the resolved strategy's own
    function + `infer_module` (the one shared helper that materially shapes racking). A change to
    any other helper is out of scope by design; extend this set if a new helper shapes racking.
    """
    import hashlib
    import inspect
    if strategy not in REGISTRY:
        raise ValueError(f"unknown plate_strategy {strategy!r}; registered: {sorted(REGISTRY)}")
    fn = REGISTRY[strategy][0]
    src = inspect.getsource(fn) + "\n" + inspect.getsource(infer_module)
    return "sha256:" + hashlib.sha256(src.encode("utf-8")).hexdigest()


def effective_default() -> str:
    """The best BUILT (covered) strategy marked default-intent; falls back to the only/first
    covered strategy. Guards against defaulting to an unbuilt strategy (never default to vapor)."""
    built = [(n, d) for n, (fn, mat, d, _) in REGISTRY.items() if mat == "covered"]
    for name, is_default in built:
        if is_default:
            return name
    return built[0][0] if built else "single-plate"


def resolve(manifest_recorded: str | None, passin: str | None) -> str:
    """Precedence: recorded manifest strategy > orchestrator pass-in > effective default.
    An UNKNOWN recorded/pass-in strategy is an error (fail loud — never silently fall back and
    mislabel the run's provenance)."""
    for src, val in (("manifest", manifest_recorded), ("passin", passin)):
        if val:
            if val not in REGISTRY:
                raise ValueError(f"unknown plate_strategy {val!r} (from {src}); "
                                 f"registered: {sorted(REGISTRY)}")
            return val
    return effective_default()


def reorder(strategy: str, paths: list[str], ctx: dict | None = None) -> list[str]:
    if strategy not in REGISTRY:
        raise ValueError(f"unknown plate_strategy {strategy!r}; registered: {sorted(REGISTRY)}")
    fn = REGISTRY[strategy][0]
    out = fn(paths, ctx or {})
    # conservation guard (L2): a strategy is a PERMUTATION — same multiset of files, none added
    # or dropped. A strategy that changes the file set is a defect, caught here.
    if sorted(out) != sorted(paths):
        raise AssertionError(f"plate_strategy {strategy!r} altered the file set "
                             f"(in={len(paths)} out={len(out)}) — must be a pure permutation (L2)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Plate strategy: pluggable page-manifest reorder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reorder", help="reorder a manifest file by a strategy")
    r.add_argument("strategy")
    r.add_argument("manifest_in")
    r.add_argument("manifest_out")
    r.add_argument("--source-root", default="")

    rs = sub.add_parser("resolve", help="print the effective strategy given manifest+passin")
    rs.add_argument("manifest_json", help="run manifest JSON path or '-' for none")
    rs.add_argument("--passin", default="")

    sub.add_parser("registry", help="print the registered strategies")

    args = ap.parse_args()

    if args.cmd == "registry":
        print(json.dumps({n: {"maturity": mat, "default_intent": d, "desc": desc}
                          for n, (fn, mat, d, desc) in REGISTRY.items()}, indent=2))
        print(json.dumps({"effective_default": effective_default()}))
        return 0

    if args.cmd == "resolve":
        recorded = None
        if args.manifest_json and args.manifest_json != "-" and os.path.exists(args.manifest_json):
            try:
                recorded = json.load(open(args.manifest_json)).get("plate_strategy")
            except Exception:
                recorded = None
        print(resolve(recorded, args.passin or None))
        return 0

    if args.cmd == "reorder":
        with open(args.manifest_in) as f:
            paths = [ln.strip() for ln in f if ln.strip()]
        out = reorder(args.strategy, paths, {"source_root": args.source_root})
        with open(args.manifest_out, "w") as f:
            f.write("\n".join(out) + ("\n" if out else ""))
        # report to stderr (stdout stays clean for piping)
        mods = len({infer_module(p) for p in out})
        print(f"[plate:{args.strategy}] reordered {len(paths)} files into {mods} module groups",
              file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
