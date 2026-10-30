# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""telemetry.py: double-logged calls (call_llm's unlabelled `_.jsonl` record + the caller's
labelled record) are counted once, and cost prices cache tokens and the model actually used."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import telemetry  # noqa: E402

SONNET = "us.anthropic.claude-sonnet-4-6"
HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _rec(ts, inp, out, still="", phase="", model=SONNET, cr=0, cw=0):
    return {"timestamp": f"2026-10-05T21:18:{ts}Z", "still": still, "phase": phase,
            "status": "success", "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cr, "cache_write_tokens": cw, "model_id": model}


def _write(d: Path, name: str, recs):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def test_double_logged_calls_counted_once(tmp_path):
    tel = tmp_path / "telemetry"
    # call_llm's unlabelled copies of two reduce calls and one format call ...
    _write(tel, "_.jsonl", [_rec("51.286", 19462, 55), _rec("52.796", 26253, 55),
                            _rec("53.100", 1000, 900),
                            # ... and one genuinely unlabelled call (no labelled twin)
                            _rec("59.000", 500, 50)])
    _write(tel, "data_reduce.jsonl", [_rec("51.287", 19462, 55, "data", "reduce")])
    _write(tel, "domain_language_reduce.jsonl", [_rec("52.797", 26253, 55, "domain_language", "reduce")])
    _write(tel, "rule_format_r0.jsonl", [_rec("53.101", 1000, 900, "rule", "format_r0")])
    # a map call logged once by the page-centric executor (with cache tokens)
    _write(tel, "data_map.jsonl", [_rec("40.000", 5000, 2000, "data", "map", cr=100_000, cw=10_000)])

    assert len(telemetry.load_records(str(tel), dedupe=False)) == 8
    recs = telemetry.load_records(str(tel))
    assert len(recs) == 5
    assert sum(1 for r in recs if not r["still"]) == 1                     # the genuine one survives
    agg = telemetry.aggregate_tpm(str(tel))
    assert agg["total_calls"] == 5
    assert agg["total_input_tokens"] == 19462 + 26253 + 1000 + 500 + 5000


def test_duplicate_outside_window_is_kept(tmp_path):
    tel = tmp_path / "telemetry"
    _write(tel, "_.jsonl", [_rec("10.000", 100, 10)])
    _write(tel, "x_reduce.jsonl", [_rec("50.000", 100, 10, "x", "reduce")])
    assert len(telemetry.load_records(str(tel))) == 2


def test_cost_prices_cache_and_model(tmp_path):
    tel = tmp_path / "telemetry"
    _write(tel, "data_map.jsonl", [_rec("40.000", 1_000_000, 1_000_000, "data", "map",
                                        cr=1_000_000, cw=1_000_000)])
    _write(tel, "code_graph_extract.jsonl", [_rec("41.000", 1_000_000, 1_000_000, "code_graph",
                                                  "extract", model=HAIKU)])
    c = telemetry.compute_cost(str(tel))
    assert c["total_cache_read_tokens"] == 1_000_000 and c["total_cache_write_tokens"] == 1_000_000
    # sonnet: 3 + 15 + 3.75 (write) + 0.30 (read); haiku: 1 + 5
    assert c["cache_write_cost_usd"] == 3.75 and c["cache_read_cost_usd"] == 0.30
    assert c["total_cost_usd"] == round(3 + 15 + 3.75 + 0.30 + 1 + 5, 4)
    assert c["by_phase"]["code_graph/extract"]["cost_usd"] == 6.0


def test_real_run_matches_hand_deduped_summary():
    tel = HERE.parents[2].parent.parent.parent / "output" / "ap-invoices" / "telemetry"
    if not tel.exists():
        tel = HERE.parents[2] / "output" / "ap-invoices" / "telemetry"
    if not tel.exists():
        import pytest
        pytest.skip("output/ap-invoices/telemetry not present")
    c = telemetry.compute_cost(str(tel))
    # RUN_SUMMARY.md: 123 calls, $14.79 after removing the 45 duplicates by hand
    assert c["total_calls"] == 123
    assert abs(c["total_cost_usd"] - 14.79) < 0.01
