# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock tool-use call: every failure mode must raise, not degrade.

All Bedrock traffic is mocked. The autouse ``no_aws`` fixture makes a real client
an immediate test failure.
"""
from __future__ import annotations

import json

import pytest

from conftest_score import (access_denied_error, empty_response, judgment, text_response,
                      throttling_error, tool_response)
from judge import schema as sch
from judge.client import JudgeProtocolError, JudgeTransportError, converse_tool


def call(**kw):
    kw.setdefault("tag", "t")
    return converse_tool("prompt", system="sys", sleep=lambda _s: None, **kw)


def test_happy_path_returns_tool_input(fake_bedrock, tmp_path):
    fake_bedrock([tool_response([judgment("F1", matches=[("K-01", "full")])])])
    r = call(log_path=tmp_path / "log.jsonl")
    assert r["data"] == {"judgments": [judgment("F1", matches=[("K-01", "full")])]}
    assert r["input_tokens"] == 100 and r["output_tokens"] == 50
    assert r["cost_usd"] > 0
    sch.validate(r["data"])


def test_tool_config_is_sent_with_forced_tool_choice(fake_bedrock, tmp_path):
    fake = fake_bedrock([tool_response([judgment("F1")])])
    call(log_path=tmp_path / "log.jsonl")
    kw = fake.calls[0]
    assert kw["toolConfig"]["toolChoice"] == {"tool": {"name": sch.TOOL_NAME}}
    assert kw["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["required"] == ["judgments"]
    assert kw["inferenceConfig"]["temperature"] == 0.0


def test_every_call_is_logged(fake_bedrock, tmp_path):
    log = tmp_path / "calls.jsonl"
    fake_bedrock([tool_response([judgment("F1")])])
    call(log_path=log, tag="judge:x")
    rec = json.loads(log.read_text().strip())
    assert rec["tag"] == "judge:x" and rec["mode"] == "tool_use"
    assert rec["input_tokens"] == 100 and rec["cost_usd"] > 0


def test_text_only_response_raises(fake_bedrock, tmp_path):
    """The old judge would have happily regex-scraped this."""
    fake_bedrock([text_response('{"judgments": [{"finding_id": "F1", "verdict": "matched"}]}')])
    with pytest.raises(JudgeProtocolError, match="no 'record_judgments' tool-use block"):
        call(log_path=tmp_path / "log.jsonl")


def test_empty_response_raises(fake_bedrock, tmp_path):
    fake_bedrock([empty_response()])
    with pytest.raises(JudgeProtocolError, match="empty response"):
        call(log_path=tmp_path / "log.jsonl")


def test_truncated_response_raises(fake_bedrock, tmp_path):
    fake_bedrock([tool_response([judgment("F1")], stop="max_tokens")])
    with pytest.raises(JudgeProtocolError, match="maxTokens"):
        call(log_path=tmp_path / "log.jsonl")


def test_wrong_tool_name_raises(fake_bedrock, tmp_path):
    fake_bedrock([tool_response([judgment("F1")], tool_name="something_else")])
    with pytest.raises(JudgeProtocolError):
        call(log_path=tmp_path / "log.jsonl")


def test_two_tool_blocks_raise(fake_bedrock, tmp_path):
    resp = tool_response([judgment("F1")])
    resp["output"]["message"]["content"].append(
        {"toolUse": {"toolUseId": "tu_2", "name": sch.TOOL_NAME, "input": {"judgments": []}}})
    fake_bedrock([resp])
    with pytest.raises(JudgeProtocolError, match="expected exactly one"):
        call(log_path=tmp_path / "log.jsonl")


def test_tool_input_as_unparsable_string_raises(fake_bedrock, tmp_path):
    fake_bedrock([tool_response(None, raw="{not json at all")])
    with pytest.raises(JudgeProtocolError, match="not JSON"):
        call(log_path=tmp_path / "log.jsonl")


def test_tool_input_as_json_string_is_parsed(fake_bedrock, tmp_path):
    payload = json.dumps({"judgments": [judgment("F1")]})
    fake_bedrock([tool_response(None, raw=payload)])
    r = call(log_path=tmp_path / "log.jsonl")
    assert r["data"]["judgments"][0]["finding_id"] == "F1"


def test_throttling_is_retried_then_succeeds(fake_bedrock, tmp_path):
    fake = fake_bedrock([throttling_error(), throttling_error(),
                         tool_response([judgment("F1")])])
    r = call(log_path=tmp_path / "log.jsonl")
    assert r["retries"] == 2 and len(fake.calls) == 3
    assert r["data"]["judgments"][0]["finding_id"] == "F1"


def test_throttling_beyond_budget_raises_transport_error(fake_bedrock, tmp_path):
    fake_bedrock([throttling_error()] * 6)
    with pytest.raises(JudgeTransportError, match="ThrottlingException after 6 attempts"):
        call(log_path=tmp_path / "log.jsonl", max_retries=5)


def test_non_retryable_error_surfaces_immediately(fake_bedrock, tmp_path):
    from botocore.exceptions import ClientError
    fake = fake_bedrock([access_denied_error(), tool_response([judgment("F1")])])
    with pytest.raises(ClientError):
        call(log_path=tmp_path / "log.jsonl")
    assert len(fake.calls) == 1


def test_retry_backoff_is_bounded(fake_bedrock, tmp_path):
    sleeps = []
    fake_bedrock([throttling_error()] * 4 + [tool_response([judgment("F1")])])
    converse_tool("p", system="s", tag="t", log_path=tmp_path / "l.jsonl",
                  sleep=sleeps.append)
    assert sleeps == [1, 2, 4, 8]
