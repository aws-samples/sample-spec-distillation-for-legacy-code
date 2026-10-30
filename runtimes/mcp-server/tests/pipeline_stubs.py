# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline stand-ins for the LLM/embedding stages of run_distillation.sh (no AWS, no models).

install(repo_copy) overwrites the downstream scripts in a COPY of the repo with stubs that write
the files the orchestrator checks for and append "<stage>[:detail]" to $STUB_LOG, so a test can
see exactly which stages recomputed. The deterministic stages (enumeration, ingest, paging,
preflight, run identity, content guard, graph export) stay real.

Env knobs read by the stubs:
  STUB_LOG          file the stubs append calls to (required)
  STUB_MAP_MODE     ok (default) | refusal   — what the page-centric map/reduce writes
  STUB_VIEWER_FAIL  1 → viewer exits 1
  STUB_CG_FAIL      1 → code_graph exits 1
  STUB_ISSUES_EMPTY 1 → issues.py reports "candidates but no issues" (exit 3)
  STUB_COST_USD     USD each LLM-stage stub records in $DISTILL_TELEMETRY_DIR per call (map: one
                    call per page x still), so the budget guard sees real-looking spend
  STUB_MAP_BUDGET   1 → the page-centric invoker maps only the first still, then exits 6 (the
                    in-loop budget guard refused the next call)
  STUB_MAP_SLEEP_S  seconds the page-centric invoker sleeps before mapping (cancel tests)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REFUSAL = ("I don't have the actual file contents for a.py — only placeholder markers showing "
           "their size with no actual code inside. I cannot extract business rules without "
           "seeing the actual source code.\n")


def _log(entry: str) -> None:
    with open(os.environ["STUB_LOG"], "a") as f:
        f.write(entry + "\n")


def _charge(still: str, phase: str, page: str = "") -> None:
    """Record one stub LLM call costing $STUB_COST_USD (all output tokens, Sonnet price)."""
    usd = float(os.environ.get("STUB_COST_USD", "0") or 0)
    tel = os.environ.get("DISTILL_TELEMETRY_DIR", "")
    if not usd or not tel:
        return
    Path(tel).mkdir(parents=True, exist_ok=True)
    rec = {"timestamp": "2026-10-06T00:00:00.000Z", "still": still, "phase": phase, "page_id": page,
           "status": "success", "input_tokens": 0, "output_tokens": int(round(usd / 15e-6)),
           "cache_read_tokens": 0, "cache_write_tokens": 0,
           "model_id": "us.anthropic.claude-sonnet-4-6"}
    with open(Path(tel) / f"{still or '_'}_{phase or ''}.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def _arg(flag: str, default: str = "") -> str:
    a = sys.argv
    return a[a.index(flag) + 1] if flag in a else default


def pagecentric_invoke() -> int:
    pages = [p for p in Path(_arg("--pages-file")).read_text().splitlines() if p.strip()]
    base = Path(_arg("--output-base"))
    mode = os.environ.get("STUB_MAP_MODE", "ok")
    budget_stop = os.environ.get("STUB_MAP_BUDGET") == "1"
    if os.environ.get("STUB_MAP_SLEEP_S"):
        import time
        time.sleep(float(os.environ["STUB_MAP_SLEEP_S"]))  # a long map (cancel tests)
    for n_still, still in enumerate([s for s in _arg("--stills").split(",") if s]):
        sd = base / still
        rc = sd / f"{still}_raw_concat.md"
        if rc.exists() and rc.stat().st_size > 0:
            continue  # resume: already reduced
        if budget_stop and n_still >= 1:
            print("[page-centric] [budget] cap reached; no new calls")
            return 6
        _log(f"map:{still}")
        body = []
        for i, p in enumerate(pages):
            page = Path(p).parent.name
            src = Path(p).read_text()
            (sd / page).mkdir(parents=True, exist_ok=True)
            if mode == "refusal":
                text = REFUSAL
            else:
                first = next((ln for ln in src.splitlines() if ln.startswith("[FILE:")), "[FILE: ?]")
                text = (f"### Rule: Stub finding {i}\n- Definition: derived from {first}\n"
                        f"- Evidence: {page}\n- Source digest: {hash(src) & 0xffff}\n")
            (sd / page / f"{still}_extract.md").write_text(text)
            _charge(still, "map", page)
            body.append(f"<!-- PAGE: {page} -->\n{text}")
        rc.write_text(f"# {still} — Raw Extraction Concat\n\nPages: {len(pages)}\n\n---\n\n"
                      + "\n".join(body))
    return 0


def knowledge() -> int:
    concat, still, final = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    graph = sys.argv[5] if len(sys.argv) > 5 else str(final / "knowledge_graph.json")
    _log(f"knowledge:{still}")
    final.mkdir(parents=True, exist_ok=True)
    (final / "spec.md").write_text(Path(concat).read_text())
    (final / ".complete").write_text("ok")
    db = final.parent.parent / "shared_vectors.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS vectors (block_id TEXT, still_name TEXT, block_type TEXT,"
              " title TEXT, embedding BLOB, body TEXT)")
    c.execute("INSERT INTO vectors VALUES (?,?,?,?,?,?)", (still + "-1", still, "rule", "t", b"", "b"))
    c.commit(); c.close()
    g = {"nodes": [], "links": []}
    if os.path.exists(graph):
        g = json.loads(Path(graph).read_text())
    g["nodes"].append({"id": still})
    Path(graph).write_text(json.dumps(g))
    return 0


def synthesis() -> int:
    _log("synthesis")
    _charge("synthesis", "synthesize")
    return 0


def format_still() -> int:
    _log("format")
    _charge("format", "format_r0")
    for sd in Path(sys.argv[1]).iterdir():
        if (sd / "final").is_dir():
            fd = sd / "final" / "formatted" / "signal"
            fd.mkdir(parents=True, exist_ok=True)
            (fd / "x.md").write_text("### Rule: [RL-0001] Stub\n- Statement: REQUIRE DataProgram\n")
            (sd / "final" / "formatted" / ".complete").write_text("ok")
    return 0


def code_graph() -> int:
    _log("code_graph")
    if os.environ.get("STUB_CG_FAIL") == "1":
        print("[code_graph] ERROR: 1/1 files failed extraction")
        return 1
    db = sys.argv[2]
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS source_files (path TEXT PRIMARY KEY, lang TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS code_objects (path TEXT, object TEXT, kind TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS code_edges (src TEXT, dst TEXT, type TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS code_entities (path TEXT, code_name TEXT, business_name TEXT)")
    c.execute("INSERT OR REPLACE INTO source_files VALUES ('a.py','python')")
    c.execute("INSERT INTO code_objects VALUES ('a.py','dataprogram','program')")
    c.execute("INSERT INTO code_edges VALUES ('a.py','b','CALLS')")
    c.commit(); c.close()
    so = _arg("--stats-out")
    if so:
        Path(so).write_text(json.dumps({"files": 1, "ok": 1, "errors": 0}))
    return 0


def concept_symbols() -> int:
    _log("concept_symbols")
    Path(_arg("--out")).write_text(json.dumps(
        {"by_concept": {"rule::RL-0001": {"symbols": ["DataProgram"]}},
         "by_symbol": {"dataprogram": ["rule::RL-0001"]}}))
    return 0


def reconcile() -> int:
    _log("reconcile")
    Path(sys.argv[2]).mkdir(parents=True, exist_ok=True)
    (Path(sys.argv[2]) / "convergence.md").write_text("# convergence\n")
    return 0


def master_blend() -> int:
    _log("master_blend")
    (Path(sys.argv[3]) / "master_blend.md").write_text("# blend\n")
    return 0


def bottling() -> int:
    _log("bottling")
    _charge("bottling", "chunk")
    (Path(sys.argv[2]) / "bottled_spec.md").write_text("# spec\n")
    return 0


def issues() -> int:
    _log("issues")
    out = Path(_arg("--out-dir"))
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("STUB_ISSUES_EMPTY") == "1":  # candidates existed, nothing survived
        (out / "issues.json").write_text(json.dumps({"status": "failed", "issues": []}))
        return 3
    (out / "issues.json").write_text(json.dumps({"status": "ok", "issues": [{"id": "ISS-001"}]}))
    (out / "issues.md").write_text("# issues\n")
    return 0


def viewer() -> int:
    _log("viewer")
    if os.environ.get("STUB_VIEWER_FAIL") == "1":
        print("viewer: simulated failure")
        return 1
    vd = Path(_arg("--viewer-dir"))
    vd.mkdir(parents=True, exist_ok=True)
    (vd / "index.html").write_text("<html></html>")
    return 0


def _noop(name: str):
    def f() -> int:
        _log(name)
        return 0
    return f


STUBS = {
    "runtimes/mcp-server/pagecentric_invoke.py": pagecentric_invoke,
    "runtimes/mcp-server/normalize_output.py": _noop("normalize"),
    "runtimes/mcp-server/knowledge.py": knowledge,
    "runtimes/mcp-server/synthesize_concepts.py": synthesis,
    "runtimes/mcp-server/format_still.py": format_still,
    "runtimes/mcp-server/code_graph.py": code_graph,
    "runtimes/mcp-server/arbitrate.py": _noop("arbitrate"),
    "runtimes/mcp-server/crosscheck.py": _noop("crosscheck"),
    "runtimes/mcp-server/concept_symbols.py": concept_symbols,
    "runtimes/mcp-server/symbol_backmap.py": _noop("symbol_backmap"),
    "runtimes/mcp-server/symbol_backmap_residue.py": _noop("backmap_residue"),
    "runtimes/mcp-server/reconcile.py": reconcile,
    "runtimes/mcp-server/master_blend.py": master_blend,
    "runtimes/mcp-server/bottling.py": bottling,
    "runtimes/mcp-server/issues.py": issues,
    "runtimes/pipeline/generate-viewer-v3.py": viewer,
}


def install(repo: Path) -> None:
    """Overwrite the LLM/embedding stage scripts in a repo COPY with stubs."""
    here = Path(__file__).resolve()
    stub_dir = repo / "runtimes" / "mcp-server" / "tests"
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "pipeline_stubs.py").write_text(here.read_text())
    for rel, fn in STUBS.items():
        target = repo / rel
        if rel.endswith("code_graph.py"):
            # preflight imports code_graph as a library: keep the real module importable.
            (target.parent / "code_graph_real.py").write_text(target.read_text())
            prefix = ("import code_graph_real as _r\nglobals().update({k: v for k, v in vars(_r).items()"
                      " if not k.startswith('__')})\n")
        else:
            prefix = ""
        target.write_text(
            prefix
            + "if __name__ == '__main__':\n"
            + "    import sys\n"
            + f"    sys.path.insert(0, {str(stub_dir)!r})\n"
            + "    import pipeline_stubs\n"
            + f"    sys.exit(pipeline_stubs.{fn.__name__ if fn.__name__ != 'f' else '_run'}"
            + ("()" if fn.__name__ != "f" else f"({rel!r})") + ")\n")


def _run(rel: str) -> int:
    return STUBS[rel]()
