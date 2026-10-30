# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase 1.2 structural facts + smaller pages (no AWS, no dotnet needed).

The .NET structure tool is replaced by a stand-in that prints a recorded facts.json for
samples/ap-invoices (fixtures/ap_invoices_facts_raw.json.gz, produced by runtimes/facts/structure
on AL2023). Everything else is real: paging (concat-batch.sh), the conservation check,
facts_post enrichment, block rendering, the map-prompt composition and run_distillation.sh.

Run:  python3 -m pytest -q runtimes/facts/tests
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FACTS = REPO / "runtimes" / "facts"
MCP = REPO / "runtimes" / "mcp-server"
KIRO = REPO / "runtimes" / "pipeline"
AP = REPO / "samples" / "ap-invoices"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ap_invoices_facts_raw.json.gz"

sys.path.insert(0, str(FACTS))
sys.path.insert(0, str(MCP))
import build_facts  # noqa: E402
import facts_block  # noqa: E402


def _default_page_tokens() -> int:
    m = re.search(r'TOKEN_BUDGET="\$\{DISTILL_PAGE_TOKENS:-\$\{DISTILL_TOKEN_BUDGET:-(\d+)\}\}"',
                  (REPO / "run_distillation.sh").read_text())
    assert m, "run_distillation.sh must default the page budget via DISTILL_PAGE_TOKENS"
    return int(m.group(1))


PAGE_TOKENS = _default_page_tokens()


def _exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_tool(tmp: Path, rc: int = 0) -> str:
    """A DISTILL_FACTS_CMD that prints the recorded AP facts (or fails with rc)."""
    script = tmp / "fake_structure_tool.py"
    script.write_text(
        "import gzip, sys\n"
        f"rc = {rc}\n"
        "if rc:\n"
        "    sys.stderr.write('boom: parse failed\\n'); sys.exit(rc)\n"
        f"sys.stdout.write(gzip.open({str(FIXTURE)!r}, 'rt').read())\n"
        "sys.stderr.write('files=17 (fake)\\n')\n")
    return f"{sys.executable} {script}"


def _build_pages(src: Path, work: Path, budget: int) -> tuple[Path, Path]:
    """Phase 0 enumeration + Phase 0b dependency-cohesive plate + Phase 1 page loop, as in
    run_distillation.sh."""
    enum = work / "enum"
    enum.mkdir(parents=True)
    subprocess.run([sys.executable, str(MCP / "source_inventory.py"), "enumerate", str(src), "--out-dir",
                    str(enum), "--suffix", "", "--config", f"token_budget={budget}"],
                   check=True, capture_output=True)
    manifest = enum / "plated_manifest.txt"
    subprocess.run([sys.executable, str(MCP / "plate_strategy.py"), "reorder", "dependency-cohesive",
                    str(enum / "candidates.txt"), str(manifest), "--source-root", str(src)],
                   check=True, capture_output=True)
    pages = work / ".pages"
    pages.mkdir()
    offset, n = "0", 0
    while True:
        assert n < 200
        d = pages / f"page_{n:03d}"
        d.mkdir()
        subprocess.run(["bash", str(KIRO / "concat-batch.sh"), str(src), str(manifest), str(d), offset,
                        str(budget)], check=True, capture_output=True)
        if (d / "stats.txt").read_text().split()[0] == "0":
            shutil.rmtree(d)
            break
        offset = (d / "next_offset.txt").read_text().strip()
        n += 1
    return pages, manifest


@pytest.fixture(scope="module")
def ap_pages(tmp_path_factory):
    work = tmp_path_factory.mktemp("ap")
    pages, manifest = _build_pages(AP, work, PAGE_TOKENS)
    return {"work": work, "pages": pages, "manifest": manifest}


def _numbered_lines(concat: Path) -> int:
    return sum(1 for ln in concat.read_text().splitlines() if re.match(r"^\s*\d+ \| ", ln))


# ── page sizing ──────────────────────────────────────────────────────────────────────────
def test_default_budget_splits_ap_sample_into_cohesive_pages_and_conserves(ap_pages):
    pages = sorted(p for p in ap_pages["pages"].glob("page_*") if p.is_dir())
    assert len(pages) >= 3, f"AP sample should span several pages at the default budget, got {len(pages)}"
    sizes = [_numbered_lines(p / "concat.txt") for p in pages]
    # every page but the tail is in the target band; none exceeds it
    assert all(s <= 900 for s in sizes), sizes
    assert all(s >= 600 for s in sizes[:-1]), sizes
    # dependency-cohesive: files of one directory are contiguous across the page sequence
    order = [ln for p in pages for ln in (p / "included.txt").read_text().splitlines() if ln]
    dirs = [str(Path(f).parent).lower() for f in order]
    seen, prev = set(), None
    for d in dirs:
        assert d == prev or d not in seen, f"module {d} split non-contiguously: {dirs}"
        seen.add(d)
        prev = d
    r = subprocess.run([sys.executable, str(KIRO / "check-page-conservation.py"), "--pages", str(ap_pages["pages"]),
                        "--manifest", str(ap_pages["manifest"]), "--source-root", str(AP)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads((ap_pages["pages"] / "conservation_report.json").read_text())
    assert rep["ok"] is True, rep


def test_page_tokens_env_overrides_legacy_budget_name():
    text = (REPO / "run_distillation.sh").read_text()
    assert 'TOKEN_BUDGET="${DISTILL_PAGE_TOKENS:-${DISTILL_TOKEN_BUDGET:-' in text
    assert 600 * 20 <= PAGE_TOKENS <= 900 * 40, PAGE_TOKENS


# ── facts phase: present / absent ─────────────────────────────────────────────────────────
def test_facts_absent_without_dotnet_is_a_warning_not_a_failure(ap_pages, tmp_path, monkeypatch):
    monkeypatch.delenv("DISTILL_FACTS_CMD", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # no dotnet anywhere
    out = tmp_path / "facts"
    r = subprocess.run([sys.executable, str(FACTS / "build_facts.py"), "build", "--source-root", str(AP),
                        "--manifest", str(ap_pages["manifest"]), "--pages", str(ap_pages["pages"]),
                        "--out", str(out), "--page-budget", str(PAGE_TOKENS)],
                       capture_output=True, text=True, env={**os.environ})
    assert r.returncode == 0, r.stderr
    assert "WARNING facts skipped" in r.stderr and "dotnet" in r.stderr
    st = json.loads((out / "facts_status.json").read_text())
    assert st["status"] == "skipped" and "dotnet" in st["reason"]
    assert not list(out.glob("pages/page_*.md"))
    assert build_facts.blocks_hash(out) is None


def test_facts_tool_failure_degrades_to_no_facts(ap_pages, tmp_path, monkeypatch):
    monkeypatch.setenv("DISTILL_FACTS_CMD", _fake_tool(tmp_path, rc=3))
    st = build_facts.build(AP, ap_pages["manifest"], ap_pages["pages"], tmp_path / "facts", PAGE_TOKENS)
    assert st["status"] == "skipped" and "boom" in st["reason"]
    assert build_facts.blocks_hash(tmp_path / "facts") is None


def test_facts_present_builds_facts_and_bounded_page_blocks(ap_pages, tmp_path, monkeypatch):
    monkeypatch.setenv("DISTILL_FACTS_CMD", _fake_tool(tmp_path))
    out = tmp_path / "facts"
    st = build_facts.build(AP, ap_pages["manifest"], ap_pages["pages"], out, PAGE_TOKENS)
    assert st["status"] == "ok", st
    c = st["counts"]
    assert c["vb_methods"] > 0 and c["vb_calls"] > 0 and c["sql_procs"] > 0 and c["sql_tables"] > 0
    assert c["cross_layer_candidates"] >= 1 and c["duplicate_candidates"] >= 1
    assert (out / "facts.json").is_file() and (out / "graph.json").is_file()
    # every staged file is one of the paged files
    staged = sorted(str(p.relative_to(out / "source")) for p in (out / "source").rglob("*") if not p.is_dir())
    assert staged == sorted(ln for ln in ap_pages["manifest"].read_text().splitlines() if ln)
    blocks = sorted(out.glob("pages/page_*.md"))
    n_pages = len([p for p in ap_pages["pages"].glob("page_*") if p.is_dir()])
    assert len(blocks) == n_pages
    bound_tokens = 0.15 * PAGE_TOKENS
    for b in blocks:
        text = b.read_text()
        assert text.startswith(facts_block.HEADER)
        assert len(text) / build_facts.CHARS_PER_TOKEN <= bound_tokens, (b.name, len(text))
        assert re.search(r"\w+\.(vb|sql):L\d+", text), "facts must be line-cited"
    # deterministic: same inputs -> same blocks hash
    h1 = build_facts.blocks_hash(out)
    st2 = build_facts.build(AP, ap_pages["manifest"], ap_pages["pages"], out, PAGE_TOKENS)
    assert h1 and st2["blocks_hash"] == h1


def test_block_only_covers_files_on_its_page(ap_pages, tmp_path):
    facts = json.loads(gzip.open(FIXTURE, "rt").read())
    import facts_post
    facts = facts_post.enrich(facts, AP)
    for pd in sorted(p for p in ap_pages["pages"].glob("page_*") if p.is_dir()):
        units = facts_block.page_units(pd)
        block = facts_block.render_page_block(facts, units, int(0.15 * PAGE_TOKENS * 2))
        on_page = {Path(f).name for f in units}
        # every "inline SQL in"/"proc ... [" definition line refers to a file on this page
        for ln in block.splitlines():
            m = re.match(r"- (?:inline SQL in \S+|proc \S+|trigger \S+|table \S+) \[([^:\]]+):L", ln)
            if m:
                assert m.group(1) in on_page, (pd.name, ln)


def test_tiny_bound_yields_empty_block(ap_pages):
    facts = facts_post_enriched()
    pd = sorted(p for p in ap_pages["pages"].glob("page_*") if p.is_dir())[0]
    assert facts_block.render_page_block(facts, facts_block.page_units(pd), 50) == ""
    assert facts_block.render_page_block(facts, {}, 10000) == ""


def facts_post_enriched():
    import facts_post
    return facts_post.enrich(json.loads(gzip.open(FIXTURE, "rt").read()), AP)


# ── map prompt ───────────────────────────────────────────────────────────────────────────
def test_map_prompt_contains_facts_block_within_bound(ap_pages, tmp_path, monkeypatch):
    import jobs
    import orchestration_strategy
    monkeypatch.setenv("DISTILL_FACTS_CMD", _fake_tool(tmp_path))
    out = tmp_path / "facts"
    assert build_facts.build(AP, ap_pages["manifest"], ap_pages["pages"], out, PAGE_TOKENS)["status"] == "ok"
    pd = sorted(p for p in ap_pages["pages"].glob("page_*") if p.is_dir())[0]
    page = (pd / "concat.txt").read_text()
    content = jobs.page_with_facts(page, pd.name, str(out / "pages"))
    assert content.startswith(page) and jobs.FACTS_SEPARATOR in content
    block = content[len(page) + len(jobs.FACTS_SEPARATOR):]
    assert facts_block.HEADER in block
    assert len(block) / 2 <= 0.15 * PAGE_TOKENS + 1
    # the Converse request carries the block inside the cached page prefix
    system, _ = orchestration_strategy.compose_map_request(content, "instr", cache_on=True)
    assert facts_block.HEADER in system[1]["text"] and "cachePoint" in system[2]
    # no facts dir / missing block -> page unchanged
    assert jobs.page_with_facts(page, pd.name, "") == page
    assert jobs.page_with_facts(page, "page_999", str(out / "pages")) == page


def test_pagecentric_executor_sends_facts_block(ap_pages, tmp_path, monkeypatch):
    """Drive the real page-centric executor with a fake Converse client and capture the prompt."""
    import bedrock_client
    import jobs
    monkeypatch.setenv("DISTILL_FACTS_CMD", _fake_tool(tmp_path))
    monkeypatch.setenv("DISTILL_TELEMETRY_DIR", str(tmp_path / "tel"))
    out = tmp_path / "facts"
    assert build_facts.build(AP, ap_pages["manifest"], ap_pages["pages"], out, PAGE_TOKENS)["status"] == "ok"
    sent = []

    def fake_call(client, model_id, system_blocks, user_text, max_tokens):
        sent.append(system_blocks[1]["text"])
        return bedrock_client.ConverseResult(output_text="### finding\n", input_tokens=1, output_tokens=1,
                                             stop_reason="end_turn")

    monkeypatch.setattr(bedrock_client, "call_llm_blocks", fake_call)
    pages = [str(p / "concat.txt") for p in sorted(ap_pages["pages"].glob("page_*")) if p.is_dir()]
    job = jobs.create_job(pages=pages, still_prompt="p", still_name="rule", output_dir=str(tmp_path / "stills"),
                          mode="realtime", concurrency=1, max_tokens=1000, model_id="m")
    job.orchestration_strategy = "page-centric"
    job.stills = [("rule", "p"), ("data", "q")]
    job.pending_pages = list(enumerate(pages))
    job.facts_dir = str(out / "pages")
    jobs._pagecentric_executor(job, client=object(), llm_client=None)
    assert len(sent) == 2 * len(pages)
    assert all(facts_block.HEADER in s for s in sent)


# ── run identity ─────────────────────────────────────────────────────────────────────────
def test_run_identity_facts_hash_gates_resume(tmp_path):
    import run_identity as ri
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    (pages / "concat.txt").write_text("x\n")
    stills = ri.still_registry.list_extraction_stills()[:1]
    off = ri.compute_identity(tmp_path / ".pages", stills)
    on = ri.compute_identity(tmp_path / ".pages", stills, facts_hash="sha256:abc")
    assert ri.diff(off, on)["state"] == "facts_mismatch"
    assert ri.diff(on, off)["state"] == "facts_mismatch"
    assert ri.diff(on, on)["state"] == "match"


# ── run_distillation.sh end to end (LLM stages stubbed) ──────────────────────────────────
@pytest.fixture()
def rig(tmp_path):
    sys.path.insert(0, str(MCP / "tests"))
    import pipeline_stubs
    repo = tmp_path / "repo"
    shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "output", ".backup", "tests", "samples", "eval", "docs", "*.pdf",
        "*.docx", "synthetic-corpus", "worktrees", "out", "bin", "obj"))
    pipeline_stubs.install(repo)
    vbin = repo / "runtimes" / "mcp-server" / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "activate").write_text("")
    _exe(vbin / "python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    tbin = tmp_path / "bin"
    tbin.mkdir()
    _exe(tbin / "aws", "#!/bin/sh\nexit 0\n")
    _exe(tbin / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    src = tmp_path / "src"
    shutil.copytree(AP, src)
    log = tmp_path / "calls.log"
    fake = _fake_tool(tmp_path)

    def run(**env_over):
        log.write_text("")
        env = dict(os.environ)
        env.update({"PATH": f"{tbin}:/usr/bin:/bin", "DISTILL_TARGET": str(src),
                    "DISTILL_OUTPUT": str(tmp_path / "out"), "DISTILL_STILLS": "rule data",
                    "STUB_LOG": str(log), "ORCHESTRATION_STRATEGY": "page-centric",
                    "DISTILL_FACTS_CMD": fake})
        for k in ("AWS_PROFILE", "DISTILL_TOKEN_BUDGET", "DISTILL_PAGE_TOKENS", "DISTILL_FACTS"):
            env.pop(k, None)
        env.update(env_over)
        env = {k: v for k, v in env.items() if v is not None}
        p = subprocess.run(["bash", str(repo / "run_distillation.sh")], env=env,
                           capture_output=True, text=True, timeout=600)
        calls = [c for c in log.read_text().splitlines() if c]
        return p.returncode, p.stdout + p.stderr, calls

    return {"run": run, "out": tmp_path / "out"}


def test_pipeline_facts_phase_chained_and_toggling_remaps(rig):
    rc, log, calls = rig["run"]()
    assert rc == 0, log[-3000:]
    out = rig["out"]
    assert "Phase 1.2: Building structural facts" in log and "structural facts ready" in log, log[-3000:]
    assert (out / ".stage_identity" / "facts.id").is_file()
    st = json.loads((out / "facts" / "facts_status.json").read_text())
    assert st["status"] == "ok"
    n_pages = len([p for p in (out / ".pages").glob("page_*") if p.is_dir()])
    assert n_pages >= 3 and len(list((out / "facts" / "pages").glob("page_*.md"))) == n_pages
    ident = json.loads((out / "run_manifest.json").read_text())["run_identity"]
    assert ident["facts_hash"] and ident["facts_hash"].startswith("sha256:")
    assert {"map:rule", "map:data"} <= set(calls)

    # unchanged re-run: facts reused, nothing re-maps
    rc, log, calls = rig["run"]()
    assert rc == 0, log[-3000:]
    assert "Phase 1.2: Reusing structural facts" in log
    assert not {"map:rule", "map:data"} & set(calls), calls

    # facts off: identity says facts changed -> every still re-maps; no --facts-dir
    rc, log, calls = rig["run"](DISTILL_FACTS="0")
    assert rc == 0, log[-3000:]
    assert "structural facts disabled" in log
    assert "facts_mismatch" in log
    assert {"map:rule", "map:data"} <= set(calls)
    ident = json.loads((out / "run_manifest.json").read_text())["run_identity"]
    assert ident["facts_hash"] is None


def test_pipeline_without_dotnet_warns_and_completes(rig):
    rc, log, calls = rig["run"](DISTILL_FACTS_CMD=None)
    if shutil.which("dotnet", path="/usr/bin:/bin"):
        pytest.skip("dotnet is installed on this host")
    assert rc == 0, log[-3000:]
    assert "WARNING dotnet not found" in log
    assert "no structural facts for this run" in log
    st = json.loads((rig["out"] / "facts" / "facts_status.json").read_text())
    assert st["status"] == "skipped"
    assert {"map:rule", "map:data"} <= set(calls)
