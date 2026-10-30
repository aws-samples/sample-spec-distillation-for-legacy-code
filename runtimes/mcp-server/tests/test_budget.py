# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Budget guard (DISTILL_MAX_USD), per-call max_tokens bound and the telemetry cost CLI.
No AWS: Bedrock is replaced by fakes; telemetry is written to a temp dir."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MCP = HERE.parent
sys.path.insert(0, str(MCP))

import bedrock_client  # noqa: E402
import budget  # noqa: E402
import jobs  # noqa: E402
import telemetry  # noqa: E402

SONNET = "us.anthropic.claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("DISTILL_MAX_USD", "DISTILL_WARN_USD", "DISTILL_ALLOW_OVER_ESTIMATE", "DISTILL_COST_HISTORY",
              "DISTILL_MAP_CALL_RESERVE_USD", "DISTILL_MAX_CALL_TOKENS", "SKIP_SYNTHESIS", "SKIP_BOTTLING",
              "DISTILL_CODE_GRAPH", "DISTILL_ISSUES", "DISTILL_SKIP_BACKMAP_RESIDUE", "BEDROCK_MODEL_ID"):
        monkeypatch.delenv(k, raising=False)


def _rec(usd: float, still="data", phase="map", page="page_000", model=SONNET) -> dict:
    """A telemetry record costing `usd` (output tokens only, at the model's price)."""
    out = int(round(usd / (telemetry.pricing_for(model)["output_per_1m"] / 1e6)))
    return {"timestamp": "2026-10-06T00:00:00.000Z", "still": still, "phase": phase, "page_id": page,
            "status": "success", "input_tokens": 0, "output_tokens": out, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "model_id": model}


def _write_tel(tel: Path, recs: list[dict], name="x.jsonl") -> None:
    tel.mkdir(parents=True, exist_ok=True)
    with open(tel / name, "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _pages(root: Path, n: int) -> Path:
    pd = root / ".pages"
    for i in range(n):
        (pd / f"page_{i:03d}").mkdir(parents=True, exist_ok=True)
        (pd / f"page_{i:03d}" / "concat.txt").write_text("[FILE: a.vb]\n")
    return pd


STILLS11 = ["data", "rule", "workflow", "state", "permission", "domain_language", "exception",
            "history", "interface", "runtime", "test"]


# ── estimate ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pages,actual", [(3, 17.21), (4, 22.92)])
def test_default_table_reproduces_reference_runs(tmp_path, pages, actual):
    """The default table must reproduce the two measured reference runs within 10%."""
    table, src = budget.unit_costs(SONNET)
    plan = budget.plan_from_disk(tmp_path, _pages(tmp_path, pages), STILLS11)
    est = sum(budget.stage_estimates(plan, table).values())
    assert abs(est - actual) / actual < 0.10, (est, actual)
    assert "default table" in src


def test_model_scaling_and_history(tmp_path, monkeypatch):
    t_sonnet, _ = budget.unit_costs(SONNET)
    t_haiku, src = budget.unit_costs("us.anthropic.claude-haiku-4-5")
    assert t_haiku["map"][0] == pytest.approx(t_sonnet["map"][0] / 3)
    assert t_haiku["code_graph"][0] == t_sonnet["code_graph"][0]  # code graph has its own model
    assert "scaled" in src
    # history: 2 pages x 2 stills, map $0.40 each call -> $0.40 per page x still
    run = tmp_path / "old_run"
    _write_tel(run / "telemetry", [_rec(0.40, s, "map", p) for s in ("a", "b") for p in ("page_000", "page_001")]
               + [_rec(1.0, "bottling", "chunk")])
    monkeypatch.setenv("DISTILL_COST_HISTORY", str(run))
    table, src = budget.unit_costs(SONNET)
    assert table["map"][0] == pytest.approx(0.40, abs=1e-4)
    assert table["bottling"][0] == pytest.approx(0.5, abs=1e-4)          # $1 over 2 pages
    assert "telemetry history" in src
    assert budget.history_units([str(run)])["map_call_max"] == pytest.approx(0.40, abs=1e-3)


def test_estimate_counts_finished_work_as_free(tmp_path):
    pd = _pages(tmp_path, 2)
    stills = ["data", "rule"]
    for s in stills:
        for p in ("page_000", "page_001"):
            f = tmp_path / "stills" / s / p / f"{s}_extract.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
    (tmp_path / "stills" / "data" / "data_raw_concat.md").write_text("x")
    plan = budget.plan_from_disk(tmp_path, pd, stills)
    assert plan["map_todo"] == 0 and plan["reduce_todo_stills"] == 1
    rem = budget.remaining_estimates(plan, budget.unit_costs(SONNET)[0])
    assert rem["map"] == 0 and rem["reduce"] > 0 and rem["format"] > 0
    # stamped downstream stages are free once map + reduce are complete
    (tmp_path / "stills" / "rule" / "rule_raw_concat.md").write_text("x")
    (tmp_path / ".stage_identity").mkdir()
    (tmp_path / ".stage_identity" / "format.id").write_text("a b")
    rem = budget.remaining_estimates(budget.plan_from_disk(tmp_path, pd, stills), budget.unit_costs(SONNET)[0])
    assert rem["reduce"] == 0 and rem["format"] == 0 and rem["bottling"] > 0


def test_estimate_refuses_over_cap_and_override(tmp_path, monkeypatch, capsys):
    pd = _pages(tmp_path, 3)
    out = tmp_path
    monkeypatch.setenv("DISTILL_TARGET", str(tmp_path / "src"))
    monkeypatch.setenv("DISTILL_OUTPUT", str(out))
    monkeypatch.setenv("DISTILL_MAX_USD", "5")
    rc = budget.main(["estimate", "--output", str(out), "--pages-dir", str(pd), "--stills", " ".join(STILLS11)])
    assert rc == budget.EXIT_BUDGET
    st = json.loads((out / "run_status.json").read_text())
    assert st["status"] == "over_estimate" and st["max_usd"] == 5
    assert st["projected_total_usd"] > 15
    assert f"DISTILL_MAX_USD={st['suggested_max_usd']:.2f}" in st["resume_command"]
    assert st["suggested_max_usd"] >= st["projected_total_usd"]
    assert "REFUSING TO START" in capsys.readouterr().out
    est = json.loads((out / "budget_estimate.json").read_text())
    assert est["plan"]["pages"] == 3 and set(est["full_usd_by_stage"]) == set(budget.STAGE_ORDER)
    monkeypatch.setenv("DISTILL_ALLOW_OVER_ESTIMATE", "1")
    (out / "run_status.json").unlink()
    assert budget.main(["estimate", "--output", str(out), "--pages-dir", str(pd), "--stills", "data"]) == 0
    assert not (out / "run_status.json").exists()
    # no cap: always 0, still prints the estimate
    monkeypatch.delenv("DISTILL_MAX_USD")
    monkeypatch.delenv("DISTILL_ALLOW_OVER_ESTIMATE")
    assert budget.main(["estimate", "--output", str(out), "--pages-dir", str(pd), "--stills", "data"]) == 0
    assert "projected total" in capsys.readouterr().out


# ── gate + stop ─────────────────────────────────────────────────────────────────────────────
def test_gate_stops_with_status_and_resume_command(tmp_path, monkeypatch, capsys):
    pd = _pages(tmp_path, 1)
    out = tmp_path
    monkeypatch.setenv("DISTILL_TARGET", "rel/src")
    monkeypatch.setenv("DISTILL_OUTPUT", str(out))
    monkeypatch.setenv("DISTILL_STILLS", "data rule")
    monkeypatch.setenv("AWS_PROFILE", "sandbox")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/tmp/creds-file")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-in-the-command")
    monkeypatch.setenv("DISTILL_TELEMETRY_DIR", str(out / "telemetry"))
    monkeypatch.setenv("DISTILL_ALLOW_OVER_ESTIMATE", "1")
    monkeypatch.setenv("DISTILL_MAX_USD", "1.00")
    assert budget.main(["estimate", "--output", str(out), "--pages-dir", str(pd), "--stills", "data rule"]) == 0
    # spend $0.80 (dedupe: an unlabelled twin of a labelled record is not double-counted)
    _write_tel(out / "telemetry", [_rec(0.40, "data"), _rec(0.40, "rule")], "m.jsonl")
    twin = _rec(0.40, "", "")
    _write_tel(out / "telemetry", [twin], "_.jsonl")
    assert budget.spent_usd(out) == pytest.approx(0.80, abs=1e-3)
    # synthesis estimate is 2 x $0.053 -> $0.91 <= $1: passes
    assert budget.main(["gate", "--output", str(out), "--stage", "synthesis"]) == 0
    # format estimate is 2 x $0.165 -> $1.13 > $1: stops
    capsys.readouterr()
    assert budget.main(["gate", "--output", str(out), "--stage", "format"]) == budget.EXIT_BUDGET
    printed = capsys.readouterr().out
    st = json.loads((out / "run_status.json").read_text())
    assert st["status"] == "budget_exhausted"
    assert st["stage_reached"] == "2.75 format"
    assert st["spent_usd"] == pytest.approx(0.80, abs=1e-3)
    assert st["next_step_estimate_usd"] == pytest.approx(0.33, abs=0.01)
    cmd = st["resume_command"]
    assert "DISTILL_ALLOW_OVER_ESTIMATE" not in cmd and "DISTILL_TELEMETRY_DIR" not in cmd
    assert "AWS_PROFILE=sandbox" in cmd and "DISTILL_STILLS='data rule'" in cmd
    assert "AWS_SHARED_CREDENTIALS_FILE=/tmp/creds-file" in cmd and "never-in-the-command" not in cmd
    assert f"DISTILL_OUTPUT={out}" in cmd
    assert "DISTILL_TARGET=/" in cmd                     # relative target made absolute
    assert cmd.rstrip().endswith("run_distillation.sh")
    assert st["suggested_max_usd"] > 0.80 + st["remaining_estimate_usd"] - 0.01
    assert "BUDGET EXHAUSTED at 2.75 format" in printed and cmd in printed
    # no cap -> gate is a no-op
    monkeypatch.delenv("DISTILL_MAX_USD")
    assert budget.main(["gate", "--output", str(out), "--stage", "bottling"]) == 0


def test_warn_threshold_only_warns(tmp_path, monkeypatch, capsys):
    pd = _pages(tmp_path, 1)
    monkeypatch.setenv("DISTILL_MAX_USD", "100")
    monkeypatch.setenv("DISTILL_WARN_USD", "0.5")
    assert budget.main(["estimate", "--output", str(tmp_path), "--pages-dir", str(pd), "--stills", "data rule"]) == 0
    assert "over DISTILL_WARN_USD" in capsys.readouterr().out
    _write_tel(tmp_path / "telemetry", [_rec(0.6)])
    assert budget.main(["gate", "--output", str(tmp_path), "--stage", "format"]) == 0
    assert "WARNING spend $0.60 is at or over DISTILL_WARN_USD" in capsys.readouterr().out


def test_bad_cap_value_fails_loudly(monkeypatch):
    monkeypatch.setenv("DISTILL_MAX_USD", "twenty")
    with pytest.raises(SystemExit, match="not a number"):
        budget.max_usd()


# ── in-loop guard ───────────────────────────────────────────────────────────────────────────
def test_guard_counts_inflight_reservations(tmp_path):
    tel = tmp_path / "tel"
    g = budget.BudgetGuard(1.0, tel, call_reserve_usd=0.3, log=lambda m: None)
    assert g.try_reserve() and g.try_reserve() and g.try_reserve()   # 0.9 reserved
    assert not g.try_reserve() and g.exhausted                       # 1.2 > 1.0
    g.release(); g.release(); g.release()
    _write_tel(tel, [_rec(0.75)])
    assert not budget.BudgetGuard(1.0, tel, 0.3, log=lambda m: None).try_reserve()  # 0.75 + 0.3
    # the ledger reads appended records incrementally
    led = budget.SpendLedger(tel)
    assert led.spent() == pytest.approx(0.75, abs=1e-3)
    _write_tel(tel, [_rec(0.10)])
    assert led.spent() == pytest.approx(0.85, abs=1e-3)


class _R:
    def __init__(self, text, out_tokens):
        self.output_text, self.stop_reason = text, "end_turn"
        self.input_tokens, self.output_tokens = 0, out_tokens
        self.cache_read_tokens = self.cache_write_tokens = 0


def _map_job(tmp_path, n_pages, stills):
    pages = []
    for i in range(n_pages):
        p = tmp_path / ".pages" / f"page_{i:03d}" / "concat.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[FILE: a{i}.vb]\n1: x\n")
        pages.append(str(p))
    return jobs.Job(
        job_id="j", still_name="", mode="realtime", status="running", pages=pages,
        pending_pages=list(enumerate(pages)), pages_total=n_pages, pages_complete=0, pages_failed=0,
        pages_in_flight=0, output_dir=str(tmp_path / "stills"), still_prompt="", model_id=SONNET,
        max_tokens=1000, concurrency=1, start_time=time.time(),
        orchestration_strategy="page-centric", stills=[(s, f"P-{s}") for s in stills])


def test_map_stops_at_cap_and_resume_redoes_only_missing(tmp_path, monkeypatch):
    """2 pages x 3 stills, every call costs $0.15 (10K Sonnet output tokens); cap $0.50 with a
    $0.15 reserve -> at most 3 calls, job budget_exhausted, actual spend never over the cap. A
    resume with a higher cap redoes only the missing (page, still) extracts."""
    tel = tmp_path / "telemetry"
    monkeypatch.setenv("DISTILL_TELEMETRY_DIR", str(tel))
    monkeypatch.setenv("DISTILL_MAX_USD", "0.50")
    monkeypatch.setenv("DISTILL_MAP_CALL_RESERVE_USD", "0.15")
    monkeypatch.setenv("DISTILL_PC_START_CONCURRENCY", "1")
    calls = []

    def fake(_c, _m, _blocks, prompt, budget_tokens):
        calls.append(prompt)
        return _R(f"out {prompt}", 10_000)

    monkeypatch.setattr(bedrock_client, "call_llm_blocks", fake)
    stills = ["data", "rule", "state"]
    job = _map_job(tmp_path, 2, stills)
    jobs._pagecentric_executor(job, client=None, llm_client=object())
    assert job.status == "budget_exhausted", job.errors
    assert len(calls) == 3
    assert budget.spent_usd(tmp_path) <= 0.50 + 1e-9
    assert any(e["error_type"] == "budget_exhausted" for e in job.errors)
    done = sorted(str(p.relative_to(tmp_path / "stills")) for p in (tmp_path / "stills").rglob("*_extract.md"))
    assert len(done) == 3 and all(p.stat().st_size for p in (tmp_path / "stills").rglob("*_extract.md"))
    # resume with a raised cap: only the 3 missing extracts are mapped
    monkeypatch.setenv("DISTILL_MAX_USD", "5")
    calls.clear()
    job2 = _map_job(tmp_path, 2, stills)
    jobs._pagecentric_executor(job2, client=None, llm_client=object())
    assert job2.status == "complete" and len(calls) == 3
    assert len(list((tmp_path / "stills").rglob("*_extract.md"))) == 6


def test_map_without_cap_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DISTILL_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    monkeypatch.setattr(bedrock_client, "call_llm_blocks", lambda *a: _R("ok", 10_000))
    job = _map_job(tmp_path, 2, ["data", "rule"])
    jobs._pagecentric_executor(job, client=None, llm_client=object())
    assert job.status == "complete" and job.pages_complete == 2


def test_reduce_stops_before_a_still_that_would_cross_the_cap(tmp_path, monkeypatch):
    tel = tmp_path / "telemetry"
    monkeypatch.setenv("DISTILL_TELEMETRY_DIR", str(tel))
    base = tmp_path / "stills"
    for s in ("data", "rule"):
        f = base / s / "page_000" / f"{s}_extract.md"
        f.parent.mkdir(parents=True)
        f.write_text("### Rule: x\n")
    _write_tel(tel, [_rec(0.97)])
    monkeypatch.setenv("DISTILL_MAX_USD", "1.0")   # $0.97 spent + $0.054 for one still > $1
    called = []
    monkeypatch.setattr(jobs, "reduce_one_still", lambda **k: called.append(k["still_name"]) or {"error": False})
    job = jobs.create_reduce_job([("data", ""), ("rule", "")], str(base), str(base), SONNET, 1000)
    jobs._reduce_executor(job, client=None)
    assert job.status == "budget_exhausted" and called == []
    monkeypatch.setenv("DISTILL_MAX_USD", "2.0")
    job = jobs.create_reduce_job([("data", ""), ("rule", "")], str(base), str(base), SONNET, 1000)
    jobs._reduce_executor(job, client=None)
    assert job.status == "complete" and called == ["data", "rule"]


# ── per-call max_tokens bound ───────────────────────────────────────────────────────────────
def test_max_tokens_bound_applies_to_every_call_path(monkeypatch):
    seen = {}

    class Conv:
        def converse(self, **kw):
            seen.setdefault("converse", []).append(kw["inferenceConfig"]["maxTokens"])
            return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {}, "stopReason": "end_turn"}

    class LLM:
        def converse(self, **kw):
            seen.setdefault("llm", []).append(kw["inferenceConfig"]["maxTokens"])
            return {"output": {"message": {"content": [{"text": "ok"}]}}, "usage": {}, "stopReason": "end_turn"}

    monkeypatch.setattr(bedrock_client, "_call_llm_streaming",
                        lambda c, kw: seen.setdefault("stream", []).append(kw["inferenceConfig"]["maxTokens"]) or
                        bedrock_client.ConverseResult("ok", 1, 1, "end_turn"))
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    bedrock_client.call_converse(Conv(), SONNET, "s", "u", 128_000)
    bedrock_client.call_converse_blocks(Conv(), SONNET, [{"text": "s"}], "u", 100)
    bedrock_client.call_llm(LLM(), "anthropic.claude-haiku-4-5", "s", "u", 128_000)
    bedrock_client.call_llm_blocks(LLM(), "anthropic.claude-haiku-4-5", [], "u", 8000)
    assert seen["converse"] == [64000, 100]
    assert seen["stream"] == [64000] and seen["llm"] == [8000]
    monkeypatch.setenv("DISTILL_MAX_CALL_TOKENS", "4096")
    bedrock_client.call_converse(Conv(), SONNET, "s", "u", 8000)
    assert seen["converse"][-1] == 4096
    assert bedrock_client.clamp_max_tokens(100) == 100


# ── telemetry cost CLI ──────────────────────────────────────────────────────────────────────
def _cli(*args):
    return subprocess.run([sys.executable, str(MCP / "telemetry.py"), *args], capture_output=True, text=True)


def test_cost_cli_accepts_run_dir_or_telemetry_dir_and_fails_on_empty(tmp_path):
    run = tmp_path / "run"
    _write_tel(run / "telemetry", [_rec(0.30), _rec(0.10, "bottling", "chunk")])
    a, b = _cli("cost", str(run)), _cli("cost", str(run / "telemetry"))
    assert a.returncode == 0 and b.returncode == 0
    assert json.loads(a.stdout) == json.loads(b.stdout)
    assert json.loads(a.stdout)["total_cost_usd"] == pytest.approx(0.40, abs=1e-3)
    assert json.loads(a.stdout)["total_calls"] == 2
    empty = tmp_path / "empty"
    (empty / "telemetry").mkdir(parents=True)
    r = _cli("cost", str(empty))
    assert r.returncode == 1 and "no telemetry records found" in r.stderr and r.stdout == ""
    r = _cli("summary", str(tmp_path / "missing"))
    assert r.returncode == 1 and "not a directory" in r.stderr
