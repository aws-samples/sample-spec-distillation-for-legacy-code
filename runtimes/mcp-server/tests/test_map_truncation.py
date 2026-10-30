# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Page-centric map (no AWS): an extract cut off at max_tokens is retried at the ceiling and,
if still truncated, fails the page instead of being written as a complete extract."""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bedrock_client  # noqa: E402
import jobs  # noqa: E402


class _R:
    def __init__(self, text, stop):
        self.output_text, self.stop_reason = text, stop
        self.input_tokens = self.output_tokens = 10
        self.cache_read_tokens = self.cache_write_tokens = 0


def _job(tmp_path, max_tokens):
    page = tmp_path / "pages" / "page_000" / "concat.txt"
    page.parent.mkdir(parents=True)
    page.write_text("[FILE: a.vb]\n1: x\n[END FILE: a.vb]\n")
    return jobs.Job(
        job_id="j", still_name="", mode="realtime", status="running", pages=[str(page)],
        pending_pages=[(0, str(page))], pages_total=1, pages_complete=0, pages_failed=0,
        pages_in_flight=0, output_dir=str(tmp_path / "stills"), still_prompt="", model_id="m",
        max_tokens=max_tokens, concurrency=1, start_time=time.time(),
        orchestration_strategy="page-centric", stills=[("data", "P1"), ("rule", "P2")])


def _run(tmp_path, monkeypatch, responder, max_tokens=100):
    calls = []

    def fake(_c, _m, _blocks, prompt, budget):
        calls.append((prompt, budget))
        return responder(prompt, budget)

    monkeypatch.setattr(bedrock_client, "call_llm_blocks", fake)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    job = _job(tmp_path, max_tokens)
    jobs._pagecentric_executor(job, client=None, llm_client=object())
    return job, calls


def test_truncated_extract_retried_at_ceiling(tmp_path, monkeypatch):
    job, calls = _run(tmp_path, monkeypatch, lambda p, b: _R(
        f"{p}-full" if b == jobs.MAP_MAX_TOKENS_CEILING else f"{p}-cut",
        "end_turn" if b == jobs.MAP_MAX_TOKENS_CEILING else "max_tokens"))
    assert job.status == "complete"
    assert calls == [("P1", 100), ("P1", jobs.MAP_MAX_TOKENS_CEILING),
                     ("P2", 100), ("P2", jobs.MAP_MAX_TOKENS_CEILING)]
    out = tmp_path / "stills" / "data" / "page_000" / "data_extract.md"
    assert out.read_text() == "P1-full"


def test_still_truncated_fails_closed(tmp_path, monkeypatch):
    job, _ = _run(tmp_path, monkeypatch, lambda p, b: _R("cut", "max_tokens"))
    assert job.status == "failed" and job.pages_failed == 1
    assert "truncated" in job.errors[0]["message"]
    assert not (tmp_path / "stills" / "data" / "page_000" / "data_extract.md").exists()


def test_untruncated_single_call(tmp_path, monkeypatch):
    job, calls = _run(tmp_path, monkeypatch, lambda p, b: _R("ok", "end_turn"))
    assert job.status == "complete" and len(calls) == 2
