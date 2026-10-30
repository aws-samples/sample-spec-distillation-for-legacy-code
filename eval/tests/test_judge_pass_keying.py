# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Judge cache keyed on pass index k, not pass count N (fix 2). No AWS calls.

Regression: the prompt says "Evaluation pass k of N" and the cache key covered the
whole prompt, so a 1-pass run's verdicts could not be reused as pass 1 of a 3-pass run
(and vice versa); on JC that meant re-paying ~$5.4 for a pass already in the cache. And
``--max-judge-cost-usd`` compared the cold-cache estimate with the cap, so a fully
cached re-score could be refused.
"""
from __future__ import annotations

import pytest

import score
from conftest_score import judgment, tool_response
from judge import runner
from judge.cache import JudgeCache
from judge.runner import JudgeBudgetExceeded, cache_keys, cache_prompt, judge_findings

KEY_ITEMS = [
    {"id": "K-01", "type": "rule", "statement": "Invoices above the threshold need approval.",
     "citations": ["a.vb:2-4"], "flags": []},
    {"id": "K-02", "type": "rule", "statement": "Posted invoices are immutable.",
     "citations": ["sub/b.sql:1-2"], "flags": []},
]
GOOD = [judgment("F1", matches=[("K-01", "full")]), judgment("F2", matches=[("K-02", "full")])]


def _run(fake_bedrock, src_tree, cache, responses, runs, **kw):
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    findings = []
    for i, t in enumerate(["Invoices above the threshold need approval. a.vb:L2-L4",
                           "Posted invoices are immutable. sub/b.sql:L1-L2"], 1):
        f = {"id": f"F{i}", "text": t, "context_file": None, "heading": "", "kind": "fragment",
             "req_id": None, "spec_lines": [i, i]}
        f["citations"] = score.extract_citations(f, files, counts, None, [])
        findings.append(f)
    fake = fake_bedrock(responses)
    kc = {str(k["id"]): [score.parse_key_cit(c) for c in k["citations"]] for k in KEY_ITEMS}
    res = judge_findings(findings, KEY_ITEMS, files=files, counts=counts,
                         split=score.split_lines_exact, tokenize=score.tokens, key_cits=kc,
                         runs=runs, cache=cache, progress=lambda *a, **k: None, **kw)
    return res, fake


def test_cache_prompt_drops_only_the_pass_count():
    assert cache_prompt("Evaluation pass 2 of 3. Judge independently") == \
        "Evaluation pass 2. Judge independently"
    # nothing else in the prompt is touched
    assert cache_prompt("Evaluation pass 1 of 1. x pass 1 of 9") == "Evaluation pass 1. x pass 1 of 9"


def test_same_pass_index_shares_a_key_across_pass_counts():
    a = cache_keys("Evaluation pass 1 of 1. body")[0]
    b = cache_keys("Evaluation pass 1 of 3. body")[0]
    c = cache_keys("Evaluation pass 2 of 3. body")[0]
    assert a == b and a != c


def test_one_pass_verdicts_are_reused_as_pass_one_of_three(fake_bedrock, src_tree, tmp_path):
    cache = tmp_path / "cache"
    res1, fake1 = _run(fake_bedrock, src_tree, JudgeCache(cache), [tool_response(GOOD)], runs=1)
    assert len(fake1.calls) == 1
    res3, fake3 = _run(fake_bedrock, src_tree, JudgeCache(cache),
                       [tool_response(GOOD), tool_response(GOOD)], runs=3)
    # only passes 2 and 3 reach Bedrock, and the prompts sent still say "of 3"
    assert len(fake3.calls) == 2
    sent = [c["messages"][0]["content"][0]["text"] for c in fake3.calls]
    assert sent[0].startswith("Evaluation pass 2 of 3.") and sent[1].startswith("Evaluation pass 3 of 3.")
    assert res3["usage"]["cached_calls"] == 1
    strip = lambda run: {k: {f: x for f, x in v.items() if f != "cached"} for k, v in run.items()}  # noqa: E731
    assert strip(res3["runs"][0]) == strip(res1["runs"][0])
    assert all(v["cached"] for v in res3["runs"][0].values())


def test_three_pass_cache_serves_a_one_pass_run_for_free(fake_bedrock, src_tree, tmp_path):
    cache = tmp_path / "cache"
    _run(fake_bedrock, src_tree, JudgeCache(cache), [tool_response(GOOD)] * 3, runs=3)
    res, fake = _run(fake_bedrock, src_tree, JudgeCache(cache), [], runs=1)
    assert fake.calls == [] and res["usage"]["cost_usd"] == 0


def test_plan_discounts_cache_hits(fake_bedrock, src_tree, tmp_path):
    cache = tmp_path / "cache"
    cold, _ = _run(fake_bedrock, src_tree, JudgeCache(cache), [], runs=3, dry_run=True)
    _run(fake_bedrock, src_tree, JudgeCache(cache), [tool_response(GOOD)], runs=1)
    warm, fake = _run(fake_bedrock, src_tree, JudgeCache(cache), [], runs=3, dry_run=True)
    assert fake.calls == []
    assert cold["plan"]["expected_cache_hits"] == 0
    assert warm["plan"]["expected_cache_hits"] == 1
    assert warm["plan"]["expected_bedrock_calls"] == 2
    assert warm["plan"]["est_cost_usd_uncached"] == cold["plan"]["est_cost_usd"]
    assert 0 < warm["plan"]["est_cost_usd"] < cold["plan"]["est_cost_usd"]
    # the dry run did not touch the hit/miss counters
    assert warm["cache"]["hits"] == 0 and warm["cache"]["misses"] == 0


def test_cost_cap_counts_cache_hits_as_free(fake_bedrock, src_tree, tmp_path):
    cache = tmp_path / "cache"
    _run(fake_bedrock, src_tree, JudgeCache(cache), [tool_response(GOOD)], runs=1)
    # fully cached: a $0 plan passes even a near-zero cap, and makes no call
    res, fake = _run(fake_bedrock, src_tree, JudgeCache(cache), [], runs=1, max_cost_usd=1e-9)
    assert fake.calls == [] and res["plan"]["est_cost_usd"] == 0
    # the uncached part of a 3-pass run is still checked against the cap
    with pytest.raises(JudgeBudgetExceeded, match="estimated judge cost"):
        _run(fake_bedrock, src_tree, JudgeCache(cache), [], runs=3, max_cost_usd=1e-9)


def test_legacy_full_prompt_entries_are_found_and_migrated(fake_bedrock, src_tree, tmp_path,
                                                          monkeypatch):
    """Entries written before this change were keyed on the full "pass k of N" prompt.
    A run with a different N must still find them, and re-file them under the new key."""
    legacy_dir = tmp_path / "legacy"
    orig = runner.cache_keys

    def legacy_key_only(prompt, **kw):
        # what the old runner did: key on the prompt exactly as sent ("pass k of N")
        ks = orig(prompt, **kw)
        k = int(prompt.split()[2])
        n = int(prompt.split(" of ", 1)[1].split(".", 1)[0])
        return [ks[1 + (n - k)]]

    monkeypatch.setattr(runner, "cache_keys", legacy_key_only)
    _run(fake_bedrock, src_tree, JudgeCache(legacy_dir), [tool_response(GOOD)] * 3, runs=3)
    monkeypatch.setattr(runner, "cache_keys", orig)

    # a 1-pass run against the old-format cache: pass 1 is found via "pass 1 of 3"
    res1, fake = _run(fake_bedrock, src_tree, JudgeCache(legacy_dir), [], runs=1)
    assert fake.calls == [] and res1["usage"]["cached_calls"] == 1
    assert res1["cache"]["writes"] == 1          # migrated to the pass-index key
    # the plan saw it as a hit too
    assert res1["plan"]["expected_cache_hits"] == 1 and res1["plan"]["est_cost_usd"] == 0
