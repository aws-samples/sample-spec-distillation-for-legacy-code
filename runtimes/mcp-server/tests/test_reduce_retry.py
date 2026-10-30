# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Reduce reconciliation retry (no AWS): a transient Bedrock stall (StreamIdleTimeout) on one
reconciliation chunk is retried with backoff instead of abandoning the whole still, mirroring the
page-centric map executor. Also covers the opt-in failed-still tolerance in _reduce_executor."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bedrock_client  # noqa: E402
import jobs  # noqa: E402


class _R:
    """Minimal ConverseResult stand-in (only the fields reduce_one_still reads)."""

    def __init__(self, text='{"duplicates": [], "unique_count": 1, "total_findings": 1}'):
        self.output_text = text
        self.input_tokens = self.output_tokens = 10
        self.cache_read_tokens = self.cache_write_tokens = 0
        self.stop_reason = "end_turn"


def _partials(tmp_path, still="data"):
    """Build a minimal partials tree so Phase 1 concat has one page extract to read."""
    base = tmp_path / "stills"
    ef = base / still / "page_000" / f"{still}_extract.md"
    ef.parent.mkdir(parents=True)
    ef.write_text("- finding one\n- finding two\n")
    return str(base)


def _reduce(tmp_path, monkeypatch, responder, cancel_event=None, still="data"):
    """Run reduce_one_still with call_llm monkeypatched to `responder`. Returns (result, calls)."""
    base = _partials(tmp_path, still)
    calls = []

    def fake_llm(_client, _model, _system, _user, _max):
        calls.append(1)
        return responder(len(calls))

    monkeypatch.setattr(bedrock_client, "call_llm", fake_llm)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    monkeypatch.setattr(jobs.time, "sleep", lambda *_: None)  # no real backoff waits
    r = jobs.reduce_one_still(
        still_name=still, partials_base=base, output_base=base, model_id="m",
        max_tokens=1000, client=None, llm_client=object(), job_id="j",
        cancel_event=cancel_event,
    )
    return r, calls, base, still


def test_retry_then_success(tmp_path, monkeypatch):
    """StreamIdleTimeout on the first 2 calls, then success → successful still (not an error)."""
    def responder(n):
        if n <= 2:
            raise bedrock_client.StreamIdleTimeout("stream went idle")
        return _R()

    r, calls, base, still = _reduce(tmp_path, monkeypatch, responder)
    assert r["error"] is False
    assert len(calls) == 3  # 2 failures + 1 success (chunk count is 1 for this small input)
    assert (Path(base) / still / f"{still}_raw_concat.md").exists()
    assert (Path(base) / still / f"{still}_dedup_index.json").exists()


def test_retry_exhausted_errors(tmp_path, monkeypatch):
    """Always StreamIdleTimeout → error only after the full attempt budget is spent."""
    def responder(_n):
        raise bedrock_client.StreamIdleTimeout("stream went idle")

    r, calls, _base, still = _reduce(tmp_path, monkeypatch, responder)
    assert r["error"] is True
    assert len(calls) == jobs.REDUCE_CHUNK_MAX_ATTEMPTS
    assert r["still_name"] == still
    assert "chunk" in r["message"] and "attempt" in r["message"]


def test_non_retryable_fails_fast(tmp_path, monkeypatch):
    """A non-retryable exception (name not in the retryable set) → error after exactly 1 call."""
    def responder(_n):
        raise ValueError("bad request")

    r, calls, _base, _still = _reduce(tmp_path, monkeypatch, responder)
    assert r["error"] is True
    assert len(calls) == 1


def test_cancel_mid_retry(tmp_path, monkeypatch):
    """cancel_event set after the first failure stops retrying before the budget is exhausted."""
    ev = threading.Event()

    def responder(n):
        # Fail the first call, then trip cancellation so the next attempt's pre-check bails.
        ev.set()
        raise bedrock_client.StreamIdleTimeout("stream went idle")

    r, calls, _base, _still = _reduce(tmp_path, monkeypatch, responder, cancel_event=ev)
    assert r["error"] is True
    assert "cancelled" in r["message"]
    assert len(calls) == 1  # did NOT exhaust REDUCE_CHUNK_MAX_ATTEMPTS
    assert len(calls) < jobs.REDUCE_CHUNK_MAX_ATTEMPTS


# ── _reduce_executor failed-still tolerance (opt-in; default strict) ──────────

def _reduce_job(tmp_path):
    base = str(tmp_path / "out")
    return jobs.create_reduce_job(
        stills=[("data", "P1"), ("rule", "P2")],
        partials_base=base, output_base=base, model_id="m", max_tokens=1000,
    )


def _patch_executor(monkeypatch):
    """reduce_one_still: 'data' fails, 'rule' succeeds."""
    def fake_reduce_one_still(still_name, **_k):
        if still_name == "data":
            return {"error": True, "still_name": still_name, "message": "boom"}
        return {"error": False, "still_name": still_name, "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(jobs, "reduce_one_still", fake_reduce_one_still)
    # Avoid real budget-guard wiring (no telemetry dir) — guard_from_env returns None already,
    # but unit_costs must resolve; the stills loop only needs reduce_one_still faked.


def test_reduce_executor_strict_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DISTILL_REDUCE_MAX_FAILED_STILLS", raising=False)
    _patch_executor(monkeypatch)
    job = _reduce_job(tmp_path)
    jobs._reduce_executor(job, client=None, llm_client=object())
    assert job.status == "failed"
    assert job.pages_failed == 1 and job.pages_complete == 1
    assert any(e["error_type"] == "ReduceError" for e in job.errors)


def test_reduce_executor_tolerates_within_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("DISTILL_REDUCE_MAX_FAILED_STILLS", "1")
    _patch_executor(monkeypatch)
    job = _reduce_job(tmp_path)
    jobs._reduce_executor(job, client=None, llm_client=object())
    assert job.status == "complete"
    assert job.pages_failed == 1 and job.pages_complete == 1
    # The failed still's per-still error entry is retained...
    assert any(e["error_type"] == "ReduceError" and "data" in e["message"] for e in job.errors)
    # ...plus a summary note recording the toleration.
    assert any(e["error_type"] == "ReduceTolerated" for e in job.errors)
