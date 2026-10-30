# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Region and log location of eval/bedrock_util.py (fixes 7 and 8). No AWS calls.

Regressions:
* provenance recorded AWS_DEFAULT_REGION (for example us-west-2) while every call went
  to bedrock_util's pinned us-east-1;
* every session appended to one shared log file instead of a per-machine, gitignored one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import bedrock_util

EVAL = Path(__file__).resolve().parents[1]
REPO = EVAL.parent


def _probe(env_extra: dict) -> dict:
    code = ("import json, bedrock_util as b; print(json.dumps({'region': b.REGION, "
            "'in_use': b.region_in_use(), 'log': str(b.DEFAULT_LOG), 'boto3': "
            "__import__('sys').modules.get('boto3') is not None}))")
    env = {k: v for k, v in os.environ.items()
           if k not in ("EVAL_BEDROCK_LOG", "EVAL_BEDROCK_REGION")}
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(EVAL),
                       env=env)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_region_ignores_the_shell_default_region():
    got = _probe({"AWS_DEFAULT_REGION": "us-west-2", "AWS_REGION": "us-west-2"})
    assert got["region"] == "us-east-1" and got["in_use"] == "us-east-1"


def test_region_override_is_explicit():
    assert _probe({"EVAL_BEDROCK_REGION": "eu-west-1"})["region"] == "eu-west-1"


def test_importing_needs_no_boto3():
    assert _probe({})["boto3"] is False


def test_default_log_is_the_gitignored_local_log_not_the_frozen_record():
    log = Path(_probe({})["log"])
    assert log == EVAL / "logs" / "local" / "bedrock_calls.jsonl"
    assert log != bedrock_util.FROZEN_HISTORICAL_LOG
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", str(log)])
    assert r.returncode == 0, "eval/logs/local/ must be gitignored"


def test_env_override_of_the_log_still_wins(tmp_path):
    assert _probe({"EVAL_BEDROCK_LOG": str(tmp_path / "x.jsonl")})["log"] == str(tmp_path / "x.jsonl")


class _Resp:
    def converse(self, **kw):
        return {"usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "end_turn",
                "output": {"message": {"content": [{"text": "{}"}]}}}

    class meta:                                         # noqa: N801  (boto3 client shape)
        region_name = "us-east-1"


def test_log_records_carry_the_region_actually_used(tmp_path, monkeypatch):
    monkeypatch.setattr(bedrock_util, "client", lambda: _Resp())
    monkeypatch.setattr(bedrock_util, "_client", _Resp())
    log = tmp_path / "calls.jsonl"
    bedrock_util.converse("hi", tag="t", log_path=log)
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["region"] == "us-east-1"


def test_suite_provenance_reads_bedrock_util_not_the_environment(monkeypatch):
    sys.path.insert(0, str(EVAL / "ci"))
    import suite_lib
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    region, source = suite_lib.bedrock_region(REPO)
    assert region == "us-east-1" and "bedrock_util.py" in source
    assert suite_lib.SHARED_BEDROCK_LOG == "eval/logs/local/bedrock_calls.jsonl"
