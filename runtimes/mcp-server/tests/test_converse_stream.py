# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""bedrock_client.call_llm (no AWS): small requests use converse, large ones use converse_stream,
and a stalled stream raises StreamIdleTimeout instead of hanging."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bedrock_client  # noqa: E402


class _Stream:
    def __init__(self, events, hang=False):
        self._events, self._hang, self._closed = events, hang, threading.Event()

    def __iter__(self):
        yield from self._events
        if self._hang:
            self._closed.wait(30)
            raise RuntimeError("connection closed")

    def close(self):
        self._closed.set()


class _Client:
    def __init__(self, stream=None):
        self.calls, self._stream = [], stream

    def converse(self, **kw):
        self.calls.append(("converse", kw))
        return {"output": {"message": {"content": [{"text": "small"}]}},
                "usage": {"inputTokens": 3, "outputTokens": 1}, "stopReason": "end_turn"}

    def converse_stream(self, **kw):
        self.calls.append(("stream", kw))
        return {"stream": self._stream}


@pytest.fixture(autouse=True)
def _no_telemetry(monkeypatch):
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)


def test_small_request_uses_converse():
    c = _Client()
    r = bedrock_client.call_llm(c, "us.anthropic.claude-sonnet-4-6", "sys", "user", 1000)
    assert r.output_text == "small" and c.calls[0][0] == "converse"
    assert c.calls[0][1]["system"] == [{"text": "sys"}]


def test_large_request_streams_and_accumulates():
    events = [{"contentBlockDelta": {"delta": {"text": "hel"}}},
              {"contentBlockDelta": {"delta": {"text": "lo"}}},
              {"messageStop": {"stopReason": "max_tokens"}},
              {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2,
                                      "cacheReadInputTokens": 7}}}]
    c = _Client(_Stream(events))
    r = bedrock_client.call_llm_blocks(c, "us.anthropic.claude-sonnet-4-6", [{"text": "s"}], "u",
                                       32000)
    assert c.calls[0][0] == "stream"
    assert (r.output_text, r.stop_reason, r.input_tokens, r.output_tokens, r.cache_read_tokens) == \
        ("hello", "max_tokens", 10, 2, 7)


def test_stalled_stream_raises_idle_timeout():
    c = _Client(_Stream([{"contentBlockDelta": {"delta": {"text": "x"}}}], hang=True))
    req = bedrock_client._converse_request("m", [{"text": "s"}], "u", 32000, 0.0)
    with pytest.raises(bedrock_client.StreamIdleTimeout):
        bedrock_client._call_llm_streaming(c, req, idle_timeout=0.5)
    assert bedrock_client.is_retryable_bedrock_error(bedrock_client.StreamIdleTimeout("x"))
