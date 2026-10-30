# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fixtures for the benefit-evaluator tests (imported by each test module; there is
no conftest.py here so the eval suites can run together). No test may reach AWS: the
Bedrock client raises, and ``fake_converse`` scripts ``bedrock_util.converse``."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pytest

BENEFITS = Path(__file__).resolve().parents[1]
EVAL = BENEFITS.parent
REPO = EVAL.parent
TOY = BENEFITS / "fixtures" / "toy"
for p in (str(BENEFITS), str(EVAL)):
    if p not in sys.path:
        sys.path.insert(0, p)

import bedrock_util  # noqa: E402


class _NoClient:
    def __getattr__(self, name):
        raise AssertionError("a benefits test tried to use the real Bedrock client")


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    monkeypatch.setattr(bedrock_util, "client", lambda: _NoClient())
    yield


def _ids(prompt: str, pat: str) -> list[str]:
    return re.findall(pat, prompt, re.M)


class FakeConverse:
    """Answers by tag. Token counts follow prompt length, so the budget guard sees real sizes."""

    def __init__(self, qa_grade="correct"):
        self.calls: list[dict] = []
        self.qa_grade = qa_grade

    def __call__(self, prompt, *, system=None, max_tokens=8000, temperature=0.0, tag="", log_path=None):
        self.calls.append({"tag": tag, "prompt": prompt})
        if tag.endswith(":qa") and "judge" not in tag:
            body = {"answer": "25 lines; ERR_TOO_MANY_LINES", "evidence_class": "code",
                    "citations": [{"file": "InvoiceService.vb", "start_line": 8, "end_line": 8}]}
        elif tag == "benefits:qa:judge":
            body = {"grades": [{"id": i, "correctness": self.qa_grade, "citation_support": "supported",
                                "trick_handling": "n/a", "plain_language": 4, "jargon": [], "note": ""}
                               for i in _ids(prompt, r"^### (\S+) \(audience")]}
        elif tag.endswith(":impact"):
            body = {"affected_locations": [{"file": "InvoiceService.vb", "start_line": 20, "end_line": 20,
                                            "reason": "tax"}],
                    "affected_rules": ["tax rate"], "affected_reports": [], "affected_contracts": []}
        elif tag == "benefits:impact:judge":
            body = {"changes": [{"id": i, "covered": [], "note": ""} for i in _ids(prompt, r"^### (\S+)$")]}
        elif tag.endswith(":fieldapp"):
            body = {"requirements": [{"id": "R1", "category": "limit", "statement": "max 25 lines",
                                      "citations": [{"file": "InvoiceService.vb", "start_line": 8,
                                                     "end_line": 8}]}]}
        elif tag == "benefits:fieldapp:judge":
            body = {"items": [{"id": "FA-1", "coverage": "full"}],
                    "requirements": [{"id": "R1", "verdict": "matched", "key_ids": ["FA-1"]}]}
        else:
            body = {}
        it, ot = max(1, len(prompt) // 4), 200
        cost = round(bedrock_util.cost_usd(it, ot), 6)
        if log_path:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as fh:
                fh.write(json.dumps({"ts": str(len(self.calls)), "tag": tag, "input_tokens": it,
                                     "output_tokens": ot, "cost_usd": cost}) + "\n")
        return {"text": json.dumps(body), "input_tokens": it, "output_tokens": ot, "cost_usd": cost,
                "stop_reason": "end_turn"}


@pytest.fixture
def fake_converse(monkeypatch):
    def install(**kw):
        fake = FakeConverse(**kw)
        monkeypatch.setattr(bedrock_util, "converse", fake)
        return fake
    return install


def ns(out: Path, **kw) -> argparse.Namespace:
    """The attributes add_adapter_args() would define, with test defaults."""
    base = dict(adapter="raw-code", source_root=TOY / "src", exclude=[], spec=None, command=None,
                command_timeout=60, tool_name=None, max_source_chars=150000, max_spec_chars=60000,
                max_usd=5.0, dry_run=False, out=out, limit=0)
    base.update(kw)
    return argparse.Namespace(**base)
