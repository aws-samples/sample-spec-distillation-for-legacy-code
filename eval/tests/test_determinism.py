# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Same inputs twice -> identical report bytes, on both layers."""
from __future__ import annotations

import json

import score
from conftest_score import judgment, tool_response
from judge.cache import JudgeCache

SPEC = """# Spec

## Business Rules

#### [RL-0001] Approval Threshold

**Requirement:** Invoices above the threshold SHALL need approval.

Evidence: a.vb:L2-L4

#### [RL-0002] Immutability

**Requirement:** Posted invoices SHALL be immutable.

Evidence: sub/b.sql:L1-L2
"""


def write(tmp_path, name="spec.md"):
    p = tmp_path / name
    p.write_text(SPEC)
    return p


def canon(report):
    return json.dumps(report, indent=2, sort_keys=True)


def test_deterministic_layer_is_byte_identical(tmp_path, src_tree, mini_key):
    p = write(tmp_path)
    a, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements")
    b, _ = score.score(p, mini_key, src_tree, False, None, None, segment="requirements")
    assert canon(a) == canon(b)


def test_markdown_render_is_byte_identical(tmp_path, src_tree, mini_key):
    p = write(tmp_path)
    a, items = score.score(p, mini_key, src_tree, False, None, None, segment="requirements")
    b, items2 = score.score(p, mini_key, src_tree, False, None, None, segment="requirements")
    assert score.to_markdown(a, items) == score.to_markdown(b, items2)


def test_source_file_order_does_not_depend_on_the_filesystem(src_tree):
    once = list(score.source_files(src_tree))
    assert once == sorted(once)


def test_judged_layer_is_identical_with_the_same_responses(tmp_path, src_tree, mini_key,
                                                           fake_bedrock):
    p = write(tmp_path)
    judgments = [judgment("F1", matches=[("K-01", "full")]),
                 judgment("F2", matches=[("K-02", "full")]),
                 judgment("F3", verdict="unsupported", note="rationale only")]
    reports = []
    for i in range(2):
        fake_bedrock([tool_response(judgments)] * 3)
        rep, _ = score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                             judge_runs=3, judge_batch=25,
                             cache_dir=tmp_path / f"cache{i}", cache_enabled=False,
                             progress=lambda *a, **k: None)
        # the one path that legitimately differs between runs
        rep["judge"]["cache"]["root"] = "<cache>"
        reports.append(rep)
    assert canon(reports[0]) == canon(reports[1])


def test_bootstrap_ci_is_reproducible(tmp_path, src_tree, mini_key, fake_bedrock):
    p = write(tmp_path)
    out = []
    for i in range(2):
        fake_bedrock([tool_response([judgment("F1", matches=[("K-01", "full")]),
                                     judgment("F2", matches=[("K-02", "partial")])])])
        rep, _ = score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                             judge_runs=1, cache_enabled=False, cache_dir=tmp_path / f"c{i}",
                             progress=lambda *a, **k: None)
        out.append(rep["judge"]["bootstrap_ci95"])
    assert out[0] == out[1]


def test_cache_reuse_reproduces_the_report_without_calls(tmp_path, src_tree, mini_key,
                                                         fake_bedrock):
    p = write(tmp_path)
    cache = tmp_path / "cache"
    js = [judgment("F1", matches=[("K-01", "full")]), judgment("F2", matches=[("K-02", "full")]),
          judgment("F3", verdict="correct_extra")]
    fake_bedrock([tool_response(js)] * 2)
    first, _ = score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                           judge_runs=2, cache_dir=cache, progress=lambda *a, **k: None)
    fake = fake_bedrock([])          # any call now raises
    second, _ = score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                            judge_runs=2, cache_dir=cache, progress=lambda *a, **k: None)
    assert fake.calls == []
    assert first["recall"] == second["recall"]
    assert first["key_coverage"] == second["key_coverage"]
    assert second["judge"]["cache"]["hits"] == 2
    assert second["judge_usage"]["cost_usd"] == 0


def test_prompt_is_identical_across_identical_invocations(tmp_path, src_tree, mini_key,
                                                          fake_bedrock):
    p = write(tmp_path)
    prompts = []
    for i in range(2):
        fake = fake_bedrock([tool_response([judgment("F1", matches=[("K-01", "full")])])])
        score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                    judge_runs=1, cache_enabled=False, cache_dir=tmp_path / f"c{i}",
                    progress=lambda *a, **k: None)
        prompts.append(fake.calls[0]["messages"][0]["content"][0]["text"])
    assert prompts[0] == prompts[1]


def test_key_items_appear_in_sorted_order_in_the_prompt(tmp_path, src_tree, fake_bedrock):
    from judge.runner import format_key
    items = [{"id": "K-09", "statement": "nine"}, {"id": "K-01", "statement": "one"},
             {"id": "K-10", "statement": "ten"}]
    assert format_key(items).splitlines() == ["K-01: one", "K-09: nine", "K-10: ten"]


def test_reuse_judgement_round_trips(tmp_path, src_tree, mini_key, fake_bedrock):
    p = write(tmp_path)
    fake_bedrock([tool_response([judgment("F1", matches=[("K-01", "full")]),
                                 judgment("F2", matches=[("K-02", "full")])])])
    first, items = score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                               judge_runs=1, cache_enabled=False, cache_dir=tmp_path / "c",
                               progress=lambda *a, **k: None)
    saved = tmp_path / "score.json"
    saved.write_text(json.dumps(first, indent=2))
    fake = fake_bedrock([])
    again, _ = score.score(p, mini_key, src_tree, True, None, None, segment="requirements",
                           reuse_judgement=saved, progress=lambda *a, **k: None)
    assert fake.calls == []
    assert again["recall"] == first["recall"]
    assert again["key_coverage"] == first["key_coverage"]
