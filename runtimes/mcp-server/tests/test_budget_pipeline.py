# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""run_distillation.sh end to end with the LLM stages stubbed (no AWS): the budget guard refuses
an over-estimate start, stops cleanly at a stage gate or inside the map with budget_exhausted and
a resume command that works (completed stages are reused at $0), the monitoring hint prints a
correct path for an absolute DISTILL_OUTPUT, and Phase 1.3 identifier evidence is chained into the
run identity."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline_reuse import ALWAYS, rig  # noqa: E402,F401  (rig is a pytest fixture)


def _status(rig):
    return json.loads((rig["out"] / "run_status.json").read_text())


def _run_resume(rig, cmd: str, **env_over):
    """Run the printed resume command verbatim (its inline env overrides the base env)."""
    log = Path(rig["out"]).parent / "calls.log"
    log.write_text("")
    env = dict(os.environ)
    env.update({"PATH": f"{Path(rig['out']).parent / 'bin'}:{env['PATH']}", "STUB_LOG": str(log)})
    for k in list(env):
        if k.startswith("DISTILL_"):
            env.pop(k)
    env.update(env_over)
    p = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout + p.stderr, [c for c in log.read_text().splitlines() if c]


def test_over_estimate_refuses_before_any_stage(rig):
    rc, out, calls = rig["run"](DISTILL_MAX_USD="0.05")
    assert rc == 6, out[-4000:]
    assert "REFUSING TO START" in out and "Distillation complete" not in out
    assert calls == [], calls                      # no map, no LLM stage ran
    st = _status(rig)
    assert st["status"] == "over_estimate" and st["max_usd"] == 0.05
    assert "DISTILL_MAX_USD=" in st["resume_command"]
    est = json.loads((rig["out"] / "budget_estimate.json").read_text())
    assert est["plan"]["pages"] == 1 and est["plan"]["stills"] == ["rule", "data"]


def test_stage_gate_stops_cleanly_and_resume_reuses_finished_stages(rig):
    # each stub LLM call records $0.30: map (2 stills x 1 page) = $0.60, synthesis $0.30 -> $0.90;
    # the format gate (estimate 2 x $0.165) would take it past the $1.00 cap -> stop there.
    rc, out, calls = rig["run"](DISTILL_MAX_USD="1.00", DISTILL_ALLOW_OVER_ESTIMATE="1", STUB_COST_USD="0.30")
    assert rc == 6, out[-4000:]
    assert "Distillation complete" not in out
    assert "BUDGET EXHAUSTED at 2.75 format" in out
    assert {"map:rule", "map:data", "synthesis"} <= set(calls) and "format" not in calls
    st = _status(rig)
    assert st["status"] == "budget_exhausted" and st["stage_reached"] == "2.75 format"
    assert abs(st["spent_usd"] - 0.90) < 0.01 and st["max_usd"] == 1.0
    cmd = st["resume_command"]
    assert cmd in out
    # the resume command, run verbatim: finished stages cost $0 (not re-run), the rest completes
    rc, out, calls = _run_resume(rig, cmd, STUB_COST_USD="0.30", ORCHESTRATION_STRATEGY="page-centric",
                                 PLATE_STRATEGY="single-plate")
    assert rc == 0, out[-4000:]
    assert "Distillation complete." in out
    assert not any(c.startswith(("map:", "knowledge:")) for c in calls) and "synthesis" not in calls, calls
    assert {"format", "code_graph", "issues", "bottling"} <= set(calls), calls
    assert _status(rig)["status"] == "complete"


def test_map_guard_stop_marks_budget_exhausted_with_resume(rig):
    rc, out, calls = rig["run"](DISTILL_MAX_USD="50", STUB_MAP_BUDGET="1")
    assert rc == 6, out[-4000:]
    assert "Distillation complete" not in out and "BUDGET EXHAUSTED at 2 map" in out
    st = _status(rig)
    assert st["status"] == "budget_exhausted" and st["stage_key"] == "map"
    assert calls == ["map:rule"], calls
    rc, out, calls = _run_resume(rig, st["resume_command"], ORCHESTRATION_STRATEGY="page-centric",
                                 PLATE_STRATEGY="single-plate")
    assert rc == 0, out[-4000:]
    assert "map:data" in calls and "map:rule" not in calls, calls   # only the missing still


def test_monitor_hint_uses_absolute_output_verbatim(rig):
    rc, out, _ = rig["run"](DISTILL_IDENTITY="stop")
    assert rc == 0, out[-3000:]
    abs_out = str(rig["out"])
    assert f"progress_server.py {abs_out} --port 8002" in out
    assert f"//{abs_out.lstrip('/')}" not in out and f"{rig['repo']}/{abs_out}" not in out


def test_identifier_evidence_is_built_and_chained_into_the_run_identity(rig):
    (rig["src"] / "flags.sql").write_text(
        "CREATE TABLE dbo.ShipHdr (\n"
        "    ShipNo  int NOT NULL,\n"
        "    SentFlg char(1) NOT NULL DEFAULT ('N'),  -- sent to carrier\n"
        "    RtgCd   varchar(4) NULL\n"
        ")\nGO\n")
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    block = (rig["out"] / "identifiers" / "pages" / "page_000.md").read_text()
    assert "SentFlg [stated]" in block and "sent to carrier" in block
    assert "RtgCd [none]" in block
    ident = json.loads((rig["out"] / "run_manifest.json").read_text())["run_identity"]
    assert ident["identifiers_hash"].startswith("sha256:")
    assert (rig["out"] / ".stage_identity" / "identifiers.id").is_file()
    # unchanged re-run: reused, nothing re-maps
    rc, out, calls = rig["run"]()
    assert rc == 0 and set(calls) <= ALWAYS, calls
    assert "Reusing identifier evidence" in out
    # a change to the evidence code changes the blocks' identity chain -> every still re-maps
    py = rig["repo"] / "runtimes" / "facts" / "identifier_evidence.py"
    py.write_text(py.read_text().replace('"[none] = nothing beyond its declaration."',
                                         '"[none] = nothing beyond its declaration (edited)."'))
    rc, out, calls = rig["run"]()
    assert rc == 0, out[-4000:]
    assert "map:rule" in calls and "map:data" in calls, calls
    # turning it off re-maps too (identifiers_hash -> None)
    rc, out, calls = rig["run"](DISTILL_IDENTIFIERS="0")
    assert rc == 0 and "map:rule" in calls, calls
