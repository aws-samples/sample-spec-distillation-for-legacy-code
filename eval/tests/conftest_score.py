# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fixtures. No test in this suite may make an AWS call.

``no_aws`` is autouse: it replaces ``bedrock_util.client`` with something that
raises, so a test that forgets to install a fake fails loudly instead of
quietly spending money.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parent.parent
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import score  # noqa: E402
from judge import schema as sch  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: large-input performance tests")


# ------------------------------------------------------------------ no AWS, ever
class _ForbiddenClient:
    def __getattr__(self, name):
        raise AssertionError(
            f"test tried to use the real Bedrock client (.{name}); install the fake_bedrock fixture")


@pytest.fixture(autouse=True)
def no_aws(monkeypatch):
    import bedrock_util
    monkeypatch.setattr(bedrock_util, "_client", None, raising=False)
    monkeypatch.setattr(bedrock_util, "client", lambda: _ForbiddenClient())
    yield


class FakeBedrock:
    """Scripted ``converse`` stand-in. Each entry is a response dict or an
    exception instance to raise."""

    def __init__(self, responses=None, default=None):
        self.responses = list(responses or [])
        self.default = default
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else self.default
        if item is None:
            raise AssertionError(f"FakeBedrock ran out of scripted responses (call {len(self.calls)})")
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(**kwargs)
        return item


@pytest.fixture
def fake_bedrock(monkeypatch):
    import bedrock_util

    def install(responses=None, default=None):
        fake = FakeBedrock(responses, default)
        monkeypatch.setattr(bedrock_util, "client", lambda: fake)
        return fake

    return install


# ------------------------------------------------------------------- builders
def tool_response(judgments, *, in_tok=100, out_tok=50, stop="tool_use",
                  tool_name=sch.TOOL_NAME, raw=None):
    """A well-formed Converse tool-use response."""
    payload = raw if raw is not None else {"judgments": judgments}
    return {
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tu_1", "name": tool_name, "input": payload}}]}},
        "usage": {"inputTokens": in_tok, "outputTokens": out_tok},
        "stopReason": stop,
    }


def text_response(text, *, in_tok=10, out_tok=10, stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "usage": {"inputTokens": in_tok, "outputTokens": out_tok}, "stopReason": stop}


def empty_response(stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": []}},
            "usage": {"inputTokens": 5, "outputTokens": 0}, "stopReason": stop}


def throttling_error():
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse")


def access_denied_error():
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "Converse")


def judgment(fid, *, verdict="matched", matches=(), note="", confidence="high"):
    j = {"finding_id": fid, "verdict": verdict, "note": note, "confidence": confidence}
    if matches:
        j["matches"] = [{"key_id": k, "degree": d} for k, d in matches]
    return j


# ------------------------------------------------------------------- corpora
@pytest.fixture
def src_tree(tmp_path):
    """A tiny source tree with exactly known line counts.

    ``a.vb`` 10 lines, ``sub/b.sql`` 5 lines, ``Reports/Odd Name (412).txt`` 3 lines,
    ``empty.sql`` 0 lines.
    """
    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    (root / "Reports").mkdir(parents=True)
    (root / "a.vb").write_text("".join(f"line {i} of a\n" for i in range(1, 11)))
    (root / "sub" / "b.sql").write_text("".join(f"sql {i}\n" for i in range(1, 6)))
    (root / "Reports" / "Odd Name (412).txt").write_text("r1\nr2\nr3\n")
    (root / "empty.sql").write_text("")
    return root


@pytest.fixture
def files_and_counts(src_tree):
    files = score.source_files(src_tree)
    counts = {k: score.line_count(p) for k, p in files.items()}
    return files, counts


def write_key(path: Path, items) -> Path:
    path.write_text(json.dumps({"corpus": "t", "items": items}, indent=1))
    return path


@pytest.fixture
def mini_key(tmp_path):
    return write_key(tmp_path / "key.json", [
        {"id": "K-01", "type": "rule", "statement": "Invoices above the threshold need approval.",
         "locations": [{"file": "a.vb", "start_line": 2, "end_line": 4}],
         "evidence_class": "Verified", "flags": []},
        {"id": "K-02", "type": "state", "statement": "Posted invoices are immutable.",
         "locations": [{"file": "sub/b.sql", "start_line": 1, "end_line": 2}],
         "evidence_class": "Verified", "flags": ["db_only"]},
    ])


def make_finding(text, fid="F1", **kw):
    f = {"id": fid, "text": text, "context_file": None, "heading": "", "kind": "fragment",
         "req_id": None, "spec_lines": [1, 1]}
    f.update(kw)
    return f


def cite(text, files, counts, default_file=None):
    """extract_citations over a bare string, with the literal-name patterns on."""
    return score.extract_citations(make_finding(text), files, counts, default_file,
                                   score.literal_file_patterns(files))
