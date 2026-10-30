#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""build_facts.py: Phase 1.2 (facts). Deterministic structural facts for the enumerated source.

Runs after paging and the conservation check, before map:

  1. Stage the enumerated source files (the page manifest) under <out>/source/ as symlinks, so the
     structure tool sees exactly the files that were paged, with the same relative paths.
  2. Run the .NET structure tool (runtimes/facts/structure: Roslyn for VB.NET and C#, ScriptDom for
     T-SQL) -> raw facts on stdout.
  3. Enrich with facts_post (cross-layer comparison and duplicate-logic candidates, contracts, ...)
     -> <out>/facts.json and <out>/graph.json.
  4. Render one bounded facts block per page -> <out>/pages/page_NNN.md (facts_block.py). The map
     phase appends that block to the page text.
  5. Write <out>/facts_status.json: {"status": "ok" | "skipped", "reason", "counts", "blocks_hash"}.

Facts are an enhancement. When dotnet or the tool is unavailable, or the tool fails, the status is
"skipped" with a reason, nothing is written under pages/, and the exit code is still 0 so the
pipeline continues without facts. Exit code 2 is reserved for bad arguments.

Usage:
  build_facts.py build --source-root S --manifest M --pages P --out O --page-budget N
                       [--max-fraction 0.15] [--tool-cmd "cmd ..."]
  build_facts.py hash --out O          # prints blocks_hash, or "none" when facts were skipped

Environment:
  DISTILL_FACTS_TOOL    path to a published LeanStructure.dll (default: structure/out/LeanStructure.dll)
  DISTILL_FACTS_CMD     full command that takes the source root as its last argument and prints raw
                        facts JSON (overrides dotnet; used by tests)
  DISTILL_FACTS_BUILD   1 (default) = build the tool with `dotnet publish` when the dll is missing
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import facts_block  # noqa: E402
import facts_post  # noqa: E402

STRUCTURE_DIR = HERE / "structure"
DEFAULT_DLL = STRUCTURE_DIR / "out" / "LeanStructure.dll"
# concat-batch.sh estimates tokens as chars/2, so the page budget is in those units too.
CHARS_PER_TOKEN = 2


def _log(msg: str) -> None:
    print(f"  [facts] {msg}", file=sys.stderr, flush=True)


def stage_source(source_root: Path, manifest: Path, dest: Path) -> list[str]:
    """Symlink (copy as a fallback) every manifest entry into dest; returns the staged rel paths."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    staged = []
    for rel in manifest.read_text(encoding="utf-8").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        src = (source_root / rel).resolve()
        if not src.is_file():
            continue
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        try:
            tgt.symlink_to(src)
        except OSError:
            shutil.copy2(src, tgt)
        staged.append(rel)
    return staged


def resolve_tool(log=_log) -> tuple[list[str] | None, str]:
    """(command prefix, reason). The prefix is None when no tool can run."""
    cmd = os.environ.get("DISTILL_FACTS_CMD", "").strip()
    if cmd:
        return shlex.split(cmd), "DISTILL_FACTS_CMD"
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return None, "dotnet not found on PATH (install the .NET 8 SDK: setup.sh does `dnf install -y dotnet-sdk-8.0`)"
    dll = Path(os.environ.get("DISTILL_FACTS_TOOL", "") or DEFAULT_DLL)
    if not dll.is_file():
        if os.environ.get("DISTILL_FACTS_BUILD", "1") != "1":
            return None, f"structure tool not built ({dll}) and DISTILL_FACTS_BUILD=0"
        log(f"structure tool not built; running dotnet publish ({STRUCTURE_DIR})")
        try:
            r = subprocess.run([dotnet, "publish", "-c", "Release", "-o", str(dll.parent)], cwd=STRUCTURE_DIR,
                               capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"dotnet publish failed: {type(e).__name__}: {e}"
        if r.returncode != 0 or not dll.is_file():
            tail = " | ".join((r.stdout + r.stderr).strip().splitlines()[-3:])
            return None, f"dotnet publish failed (rc={r.returncode}): {tail}"
    return [dotnet, str(dll)], "dotnet"


def run_tool(prefix: list[str], src: Path, timeout: float = 1800) -> dict:
    r = subprocess.run(prefix + [str(src)], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = " | ".join(r.stderr.strip().splitlines()[-3:])
        raise RuntimeError(f"structure tool rc={r.returncode}: {tail}")
    if r.stderr.strip():
        _log("tool: " + r.stderr.strip().splitlines()[-1])
    return json.loads(r.stdout)


def counts(facts: dict) -> dict:
    vb, cs, sql, v1 = (facts.get("vb", {}), facts.get("cs", {}), facts.get("sql", {}),
                       facts.get("v1", {}))
    return {
        "files": len(facts.get("files", [])),
        "vb_methods": len(vb.get("methods", [])),
        "vb_calls": len(vb.get("calls", [])),
        "vb_proc_refs": len(vb.get("proc_refs", [])),
        "vb_inline_sql": len(vb.get("inline_sql", [])),
        "vb_constants": len(vb.get("constants", [])),
        "cs_types": len(cs.get("types", [])),
        "cs_methods": len(cs.get("methods", [])),
        "cs_calls": len(cs.get("calls", [])),
        "cs_proc_refs": len(cs.get("proc_refs", [])),
        "cs_inline_sql": len(cs.get("inline_sql", [])),
        "cs_constants": len(cs.get("constants", [])),
        "sql_procs": len(sql.get("procs", [])),
        "sql_triggers": len(sql.get("triggers", [])),
        "sql_tables": len(sql.get("tables", [])),
        "comments": len(facts.get("comments", [])),
        "comparisons": len(v1.get("comparisons", [])),
        "cross_layer_candidates": len(v1.get("cross_layer_candidates", [])),
        "duplicate_candidates": len(v1.get("duplicate_candidates", [])),
        "db_checks": len(v1.get("db_checks", [])),
    }


def write_page_blocks(facts: dict, pages_dir: Path, out_pages: Path, max_chars: int) -> dict:
    """Render every page's block; returns {page_NNN: {"chars": n}}. Pages with no facts get none."""
    if out_pages.exists():
        shutil.rmtree(out_pages)
    out_pages.mkdir(parents=True)
    report = {}
    for pd in sorted(p for p in pages_dir.iterdir() if p.is_dir() and p.name.startswith("page_")):
        block = facts_block.render_page_block(facts, facts_block.page_units(pd), max_chars)
        report[pd.name] = {"chars": len(block)}
        if block:
            (out_pages / f"{pd.name}.md").write_text(block, encoding="utf-8")
    return report


def blocks_hash(out: Path) -> str | None:
    """sha256 over every rendered page block (name + bytes), or None when facts were skipped."""
    st = out / "facts_status.json"
    if not st.is_file() or json.loads(st.read_text()).get("status") != "ok":
        return None
    h = hashlib.sha256()
    for p in sorted((out / "pages").glob("page_*.md")):
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return "sha256:" + h.hexdigest()


def _write_status(out: Path, status: dict) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    (out / "facts_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def build(source_root: Path, manifest: Path, pages_dir: Path, out: Path, page_budget: int,
          max_fraction: float = 0.15) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("facts.json", "graph.json", "pages"):
        p = out / stale
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    max_chars = int(page_budget * max_fraction * CHARS_PER_TOKEN)
    base = {"page_budget_tokens": page_budget, "max_fraction": max_fraction, "max_block_chars": max_chars}
    prefix, how = resolve_tool()
    if prefix is None:
        return _write_status(out, {"status": "skipped", "reason": how, **base})
    src = out / "source"
    staged = stage_source(source_root, manifest, src)
    if not staged:
        return _write_status(out, {"status": "skipped", "reason": "no manifest files to parse", **base})
    try:
        facts = run_tool(prefix, src)
        facts = facts_post.enrich(facts, src)
    except Exception as e:  # noqa: BLE001 - any tool/parse failure degrades to "no facts"
        return _write_status(out, {"status": "skipped", "reason": f"{type(e).__name__}: {e}"[:500], **base})
    (out / "facts.json").write_text(json.dumps(facts, indent=1), encoding="utf-8")
    try:
        (out / "graph.json").write_text(json.dumps(facts_post.build_graph(facts, facts["v1"]), indent=1),
                                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - the graph is a by-product; the blocks do not need it
        _log(f"WARNING graph.json not written: {type(e).__name__}: {e}")
    pages = write_page_blocks(facts, pages_dir, out / "pages", max_chars)
    status = {"status": "ok", "reason": how, **base, "staged_files": len(staged), "counts": counts(facts),
              "pages": pages}
    _write_status(out, status)
    status["blocks_hash"] = blocks_hash(out)
    return _write_status(out, status)


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1.2: deterministic structural facts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source-root", required=True, type=Path)
    b.add_argument("--manifest", required=True, type=Path)
    b.add_argument("--pages", required=True, type=Path)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--page-budget", required=True, type=int)
    b.add_argument("--max-fraction", type=float, default=float(os.environ.get("DISTILL_FACTS_MAX_FRACTION", "0.15")))
    h = sub.add_parser("hash")
    h.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    if a.cmd == "hash":
        print(blocks_hash(a.out) or "none")
        return 0
    if a.page_budget <= 0 or not (0 < a.max_fraction <= 1):
        print("build_facts: --page-budget must be > 0 and 0 < --max-fraction <= 1", file=sys.stderr)
        return 2
    st = build(a.source_root, a.manifest, a.pages, a.out, a.page_budget, a.max_fraction)
    if st["status"] == "ok":
        sizes = [p["chars"] for p in st["pages"].values()]
        c = st["counts"]
        _log(f"facts ok: {c['files']} files, "
             f"{c['vb_methods']}/{c['cs_methods']} VB/C# methods, "
             f"{c['vb_calls']}/{c['cs_calls']} calls, "
             f"{c['vb_proc_refs'] + c['cs_proc_refs']} client->proc refs, "
             f"{c['sql_procs']} procs, {c['sql_tables']} tables, "
             f"{c['vb_constants'] + c['cs_constants']} constants, {c['cross_layer_candidates']} cross-layer + "
             f"{c['duplicate_candidates']} duplicate candidates; page blocks "
             f"{sum(1 for s in sizes if s)}/{len(sizes)} (max {max(sizes, default=0)} chars, "
             f"bound {st['max_block_chars']})")
    else:
        _log(f"WARNING facts skipped: {st['reason']} — continuing WITHOUT structural facts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
