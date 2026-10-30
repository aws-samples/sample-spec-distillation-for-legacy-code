# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Repeated judging, voting, abstention, chunked evidence, budget and retries."""
from __future__ import annotations

import pytest

import score
from conftest_score import judgment, text_response, throttling_error, tool_response
from judge import schema as sch
from judge.cache import JudgeCache
from judge.client import JudgeProtocolError
from judge.runner import JudgeBudgetExceeded, consensus, judge_findings, make_batches

KEY_ITEMS = [
    {"id": "K-01", "type": "rule", "statement": "Invoices above the threshold need approval.",
     "citations": ["a.vb:2-4"], "flags": []},
    {"id": "K-02", "type": "rule", "statement": "Posted invoices are immutable.",
     "citations": ["sub/b.sql:1-2"], "flags": []},
]


def mk_findings(src_tree, texts):
    files = score.source_files(src_tree)
    counts = {k: score.line_count(v) for k, v in files.items()}
    out = []
    for i, t in enumerate(texts, 1):
        f = {"id": f"F{i}", "text": t, "context_file": None, "heading": "", "kind": "fragment",
             "req_id": None, "spec_lines": [i, i]}
        f["citations"] = score.extract_citations(f, files, counts, None, [])
        out.append(f)
    return out, files, counts


def run(fake_bedrock, src_tree, tmp_path, responses, *, runs=1, batch=25, **kw):
    findings, files, counts = mk_findings(src_tree, kw.pop("texts", [
        "Invoices above the threshold need approval. a.vb:L2-L4",
        "Posted invoices are immutable. sub/b.sql:L1-L2",
    ]))
    fake = fake_bedrock(responses)
    kc = {str(k["id"]): [score.parse_key_cit(c) for c in k["citations"]] for k in KEY_ITEMS}
    res = judge_findings(findings, KEY_ITEMS, files=files, counts=counts,
                         split=score.split_lines_exact, tokenize=score.tokens, key_cits=kc,
                         runs=runs, batch=batch,
                         cache=kw.pop("cache", JudgeCache(tmp_path / "cache")),
                         log_path=tmp_path / "log.jsonl", progress=lambda *a, **k: None, **kw)
    return res, fake, findings


# --------------------------------------------------------------------- basics
def test_single_pass_judging(fake_bedrock, src_tree, tmp_path):
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [tool_response([
        judgment("F1", matches=[("K-01", "full")]),
        judgment("F2", matches=[("K-02", "full")])])])
    assert len(fake.calls) == 1
    assert res["consensus"]["F1"]["matches"] == [{"key_id": "K-01", "degree": "full"}]
    assert res["consensus"]["F1"]["verdict"] == "matched"
    assert res["usage"]["calls"] == 1 and res["usage"]["cost_usd"] > 0


def test_batching_splits_calls(fake_bedrock, src_tree, tmp_path):
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [
        tool_response([judgment("F1", matches=[("K-01", "full")])]),
        tool_response([judgment("F2", matches=[("K-02", "full")])])], batch=1)
    assert len(fake.calls) == 2 and res["plan"]["batches"] == 2


def test_batches_are_ordered_naturally():
    fs = [{"id": f"F{i}"} for i in [10, 2, 1, 11]]
    assert [f["id"] for f in make_batches(fs, 10)[0]] == ["F1", "F2", "F10", "F11"]


def test_missing_judgments_trigger_one_corrective_retry(fake_bedrock, src_tree, tmp_path):
    """Both findings omitted: ask again, merge, and label whatever is still missing."""
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path,
                       [tool_response([]),
                        tool_response([judgment("F1", matches=[("K-01", "full")])])])
    assert len(fake.calls) == 2
    assert "CORRECTION" in fake.calls[1]["messages"][0]["content"][0]["text"]
    assert res["usage"]["missing_retries"] == 1
    assert any(i["kind"] == "missing_judgments" for i in res["issues"])
    assert res["consensus"]["F1"]["verdict"] == "matched"
    assert res["consensus"]["F2"]["verdict"] == sch.UNJUDGED
    assert res["consensus"]["F2"]["matches"] == []


def test_one_missing_of_two_is_within_tolerance_and_labelled_unjudged(fake_bedrock, src_tree, tmp_path):
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path,
                       [tool_response([judgment("F1", matches=[("K-01", "full")])])])
    assert len(fake.calls) == 1
    assert res["consensus"]["F2"]["verdict"] == sch.UNJUDGED


def test_unknown_key_id_is_dropped_and_recorded(fake_bedrock, src_tree, tmp_path):
    res, _, _ = run(fake_bedrock, src_tree, tmp_path, [tool_response([
        judgment("F1", matches=[("K-99", "full")]),
        judgment("F2", matches=[("K-02", "full")])])])
    assert res["consensus"]["F1"]["matches"] == []
    assert any(i["kind"] == "unknown_key_id" for i in res["issues"])


def test_unknown_finding_id_is_dropped_and_recorded(fake_bedrock, src_tree, tmp_path):
    res, _, _ = run(fake_bedrock, src_tree, tmp_path, [tool_response([
        judgment("F1", matches=[("K-01", "full")]),
        judgment("F2", matches=[("K-02", "full")]),
        judgment("F999", matches=[("K-01", "full")])])])
    assert "F999" not in res["consensus"]
    assert any(i["kind"] == "unknown_finding_id" for i in res["issues"])


def test_matched_without_a_match_becomes_abstention(fake_bedrock, src_tree, tmp_path):
    res, _, _ = run(fake_bedrock, src_tree, tmp_path, [tool_response([
        judgment("F1", verdict="matched"), judgment("F2", matches=[("K-02", "full")])])])
    assert res["consensus"]["F1"]["verdict"] == "uncertain"
    assert any(i["kind"] == "matched_without_match" for i in res["issues"])


# ------------------------------------------------------------- schema retry path
def test_schema_violation_is_retried_once_then_accepted(fake_bedrock, src_tree, tmp_path):
    bad = tool_response(None, raw={"judgments": [{"finding_id": "F1", "verdict": "probably"}]})
    good = tool_response([judgment("F1", matches=[("K-01", "full")]),
                          judgment("F2", matches=[("K-02", "full")])])
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [bad, good])
    assert len(fake.calls) == 2
    assert "CORRECTION" in fake.calls[1]["messages"][0]["content"][0]["text"]
    assert res["usage"]["schema_retries"] == 1
    assert any(i["kind"] == "schema_violation" for i in res["issues"])


def test_two_schema_violations_are_fatal(fake_bedrock, src_tree, tmp_path):
    bad = tool_response(None, raw={"judgments": [{"finding_id": "F1", "verdict": "probably"}]})
    with pytest.raises(sch.SchemaError):
        run(fake_bedrock, src_tree, tmp_path, [bad, bad])


def test_free_text_response_is_fatal(fake_bedrock, src_tree, tmp_path):
    with pytest.raises(JudgeProtocolError):
        run(fake_bedrock, src_tree, tmp_path, [text_response('{"judgments": []}')])


def test_throttling_is_retried_inside_the_runner(fake_bedrock, src_tree, tmp_path, monkeypatch):
    import judge.client as jc
    monkeypatch.setattr(jc.time, "sleep", lambda _s: None)
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [
        throttling_error(),
        tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])])
    assert res["usage"]["retries"] == 1 and len(fake.calls) == 2


def _cache_files(cache_dir):
    return sorted(cache_dir.rglob("*.json")) if cache_dir.exists() else []


def test_null_note_is_tolerated_without_a_retry(fake_bedrock, src_tree, tmp_path):
    """The observed production flake: ``note: null`` on an optional field."""
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [tool_response([
        judgment("F1", matches=[("K-01", "full")], note=None),
        judgment("F2", matches=[("K-02", "full")])])])
    assert len(fake.calls) == 1
    assert res["usage"]["schema_retries"] == 0
    assert res["consensus"]["F1"]["verdict"] == "matched"
    assert res["consensus"]["F1"]["note"] == ""


def test_null_required_verdict_is_still_fatal(fake_bedrock, src_tree, tmp_path):
    bad = tool_response(None, raw={"judgments": [{"finding_id": "F1", "verdict": None}]})
    with pytest.raises(sch.SchemaError):
        run(fake_bedrock, src_tree, tmp_path, [bad, bad])


def test_corrective_retry_with_null_note_is_accepted(fake_bedrock, src_tree, tmp_path):
    """The failing production case: bad verdict first, then a retry that has a null note."""
    bad = tool_response(None, raw={"judgments": [{"finding_id": "F1", "verdict": "matches"}]})
    retry = tool_response([judgment("F1", matches=[("K-01", "full")], note=None),
                           judgment("F2", matches=[("K-02", "full")], note=None)])
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [bad, retry])
    assert len(fake.calls) == 2 and res["usage"]["schema_retries"] == 1
    assert res["consensus"]["F2"]["verdict"] == "matched"


# ------------------------------------------- invalid answers are never cached
def test_invalid_answer_is_not_cached_but_the_valid_retry_is(fake_bedrock, src_tree, tmp_path):
    cache_dir = tmp_path / "cache"
    bad = tool_response(None, raw={"judgments": [{"finding_id": "F1", "verdict": "probably"}]})
    good = tool_response([judgment("F1", matches=[("K-01", "full")]),
                          judgment("F2", matches=[("K-02", "full")])])
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [bad, good], cache=JudgeCache(cache_dir))
    assert len(fake.calls) == 2
    files = _cache_files(cache_dir)
    assert len(files) == 1 and res["cache"]["writes"] == 1
    assert "probably" not in files[0].read_text()
    assert "CORRECTION" in fake.calls[1]["messages"][0]["content"][0]["text"]


def test_fatal_schema_failure_leaves_the_cache_empty(fake_bedrock, src_tree, tmp_path):
    cache_dir = tmp_path / "cache"
    bad = tool_response(None, raw={"judgments": [{"finding_id": "F1", "verdict": "probably"}]})
    with pytest.raises(sch.SchemaError):
        run(fake_bedrock, src_tree, tmp_path, [bad, bad], cache=JudgeCache(cache_dir))
    assert _cache_files(cache_dir) == []


def test_valid_answer_is_cached_in_the_unchanged_entry_format(fake_bedrock, src_tree, tmp_path):
    import json
    from judge.cache import CACHE_FORMAT
    cache_dir = tmp_path / "cache"
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    run(fake_bedrock, src_tree, tmp_path, [g], cache=JudgeCache(cache_dir))
    (f,) = _cache_files(cache_dir)
    rec = json.loads(f.read_text())
    assert set(rec) == {"format", "key", "created", "meta", "value"}
    assert rec["format"] == CACHE_FORMAT and f.name == f"{rec['key']}.json"
    assert set(rec["meta"]) == {"tag", "input_tokens", "output_tokens"}


def test_cached_answer_with_null_note_replays_as_valid(fake_bedrock, src_tree, tmp_path):
    """An entry poisoned by the old code (``note: null`` on disk) must replay for $0."""
    cache_dir = tmp_path / "cache"
    cached = JudgeCache(cache_dir)
    seed = {"judgments": [judgment("F1", matches=[("K-01", "full")], note=None),
                          judgment("F2", matches=[("K-02", "full")], note=None)]}
    # Capture the real key by running once with a stub that returns the same payload,
    # then overwrite the entry with the null-note payload exactly as the old code stored it.
    run(fake_bedrock, src_tree, tmp_path, [tool_response(None, raw=seed)], cache=cached)
    (f,) = _cache_files(cache_dir)
    import json
    rec = json.loads(f.read_text())
    rec["value"] = seed
    f.write_text(json.dumps(rec))
    assert "null" in f.read_text()
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [], cache=JudgeCache(cache_dir))
    assert fake.calls == []
    assert res["usage"]["schema_retries"] == 0 and res["usage"]["cost_usd"] == 0
    assert res["consensus"]["F1"]["verdict"] == "matched"


def test_cached_answer_that_is_invalid_is_retried_not_replayed(fake_bedrock, src_tree, tmp_path):
    """A genuinely invalid entry (written by older code) still takes the corrective path."""
    import json
    cache_dir = tmp_path / "cache"
    good = tool_response([judgment("F1", matches=[("K-01", "full")]),
                          judgment("F2", matches=[("K-02", "full")])])
    run(fake_bedrock, src_tree, tmp_path, [good], cache=JudgeCache(cache_dir))
    (f,) = _cache_files(cache_dir)
    rec = json.loads(f.read_text())
    rec["value"] = {"judgments": [{"finding_id": "F1", "verdict": "probably"}]}
    f.write_text(json.dumps(rec))
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [good], cache=JudgeCache(cache_dir))
    assert len(fake.calls) == 1 and res["usage"]["schema_retries"] == 1


# -------------------------------------------------------------------- the vote
def _runs_from(labels_per_run, matches_per_run=None):
    out = []
    for i, labels in enumerate(labels_per_run):
        r = {}
        for fid, v in labels.items():
            ms = (matches_per_run[i].get(fid) if matches_per_run else None) or []
            r[fid] = {"verdict": v, "matches": [{"key_id": k, "degree": d} for k, d in ms],
                      "note": "", "confidence": "high"}
        out.append(r)
    return out


def test_majority_vote_two_of_three():
    per_run = _runs_from([{"F1": "matched"}, {"F1": "matched"}, {"F1": "unsupported"}],
                         [{"F1": [("K-01", "full")]}, {"F1": [("K-01", "full")]}, {"F1": []}])
    c = consensus(per_run, ["F1"], 3)["F1"]
    assert c["verdict"] == "matched"
    assert c["matches"] == [{"key_id": "K-01", "degree": "full"}]
    assert c["verdict_agreement"] == round(2 / 3, 3)
    assert c["unanimous_verdict"] is False


def test_unanimous_vote():
    per_run = _runs_from([{"F1": "incorrect"}] * 3)
    c = consensus(per_run, ["F1"], 3)["F1"]
    assert c["verdict"] == "incorrect" and c["unanimous_verdict"] and c["verdict_agreement"] == 1.0


def test_three_way_split_has_no_majority():
    per_run = _runs_from([{"F1": "matched"}, {"F1": "unsupported"}, {"F1": "incorrect"}])
    c = consensus(per_run, ["F1"], 3)["F1"]
    assert c["no_majority"] is True and c["verdict"] == "uncertain"


def test_two_pass_disagreement_has_no_majority():
    per_run = _runs_from([{"F1": "matched"}, {"F1": "unsupported"}])
    c = consensus(per_run, ["F1"], 2)["F1"]
    assert c["no_majority"] is True and c["verdict"] == "uncertain"


def test_minority_match_is_dropped():
    """The +-3-5 item wobble: a key item matched by only one pass is not credited."""
    per_run = _runs_from([{"F1": "matched"}, {"F1": "unsupported"}, {"F1": "unsupported"}],
                         [{"F1": [("K-01", "full")]}, {"F1": []}, {"F1": []}])
    c = consensus(per_run, ["F1"], 3)["F1"]
    assert c["matches"] == []


def test_match_degree_follows_the_majority_of_matching_passes():
    per_run = _runs_from([{"F1": "matched"}] * 3,
                         [{"F1": [("K-01", "full")]}, {"F1": [("K-01", "partial")]},
                          {"F1": [("K-01", "partial")]}])
    assert consensus(per_run, ["F1"], 3)["F1"]["matches"] == [{"key_id": "K-01", "degree": "partial"}]


def test_match_set_agreement_is_reported():
    per_run = _runs_from([{"F1": "matched"}] * 3,
                         [{"F1": [("K-01", "full")]}, {"F1": [("K-01", "full")]},
                          {"F1": [("K-01", "full"), ("K-02", "partial")]}])
    c = consensus(per_run, ["F1"], 3)["F1"]
    assert c["match_agreement"] == round(2 / 3, 3)


def test_single_pass_keeps_its_verdict():
    c = consensus(_runs_from([{"F1": "unsupported"}]), ["F1"], 1)["F1"]
    assert c["verdict"] == "unsupported" and c["no_majority"] is False


def test_duplicate_matches_in_one_pass_count_once():
    per_run = [{"F1": {"verdict": "matched", "matches": [{"key_id": "K-01", "degree": "full"},
                                                        {"key_id": "K-01", "degree": "partial"}],
                       "note": "", "confidence": "high"}}]
    c = consensus(per_run, ["F1"], 1)["F1"]
    assert c["match_votes"] == {"K-01": 1}


def test_three_passes_end_to_end(fake_bedrock, src_tree, tmp_path):
    good = [judgment("F1", matches=[("K-01", "full")]), judgment("F2", matches=[("K-02", "full")])]
    drift = [judgment("F1", matches=[("K-01", "partial")]), judgment("F2", verdict="unsupported")]
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path,
                       [tool_response(good), tool_response(good), tool_response(drift)], runs=3)
    assert len(fake.calls) == 3 and res["plan"]["calls"] == 3
    assert res["consensus"]["F1"]["matches"] == [{"key_id": "K-01", "degree": "full"}]
    assert res["consensus"]["F2"]["verdict"] == "matched"
    assert res["consensus"]["F2"]["verdict_agreement"] == round(2 / 3, 3)


def test_pass_number_is_in_the_prompt(fake_bedrock, src_tree, tmp_path):
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    _, fake, _ = run(fake_bedrock, src_tree, tmp_path, [g, g], runs=2)
    assert "Evaluation pass 1 of 2" in fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "Evaluation pass 2 of 2" in fake.calls[1]["messages"][0]["content"][0]["text"]


# ----------------------------------------------------------------------- cache
def test_cache_serves_the_second_identical_run(fake_bedrock, src_tree, tmp_path):
    cache_dir = tmp_path / "cache"
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    res1, fake1, _ = run(fake_bedrock, src_tree, tmp_path, [g],
                         cache=JudgeCache(cache_dir))
    assert res1["cache"]["writes"] == 1
    # no scripted responses at all: any Bedrock call would blow up
    res2, fake2, _ = run(fake_bedrock, src_tree, tmp_path, [], cache=JudgeCache(cache_dir))
    assert fake2.calls == []
    assert res2["cache"]["hits"] == 1 and res2["usage"]["cost_usd"] == 0
    assert res2["consensus"]["F1"] == res1["consensus"]["F1"]


def test_no_cache_bypasses_a_warm_cache(fake_bedrock, src_tree, tmp_path):
    cache_dir = tmp_path / "cache"
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    run(fake_bedrock, src_tree, tmp_path, [g], cache=JudgeCache(cache_dir))
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [g],
                       cache=JudgeCache(cache_dir, enabled=False))
    assert len(fake.calls) == 1 and res["cache"]["enabled"] is False


# -------------------------------------------------------------------- evidence
def test_chunked_evidence_is_far_smaller_than_the_full_dump(fake_bedrock, src_tree, tmp_path):
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    chunked, _, _ = run(fake_bedrock, src_tree, tmp_path, [g], evidence_mode="chunked",
                        evidence_context=0)
    full, _, _ = run(fake_bedrock, src_tree, tmp_path, [g], evidence_mode="full")
    assert chunked["plan"]["evidence_chars"]["total_per_run"] < full["plan"]["evidence_chars"]["total_per_run"]
    assert chunked["evidence"][0]["mode"] == "chunked"
    assert chunked["evidence"][0]["files"]


def test_evidence_manifest_records_exactly_what_was_shown(fake_bedrock, src_tree, tmp_path):
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    res, _, _ = run(fake_bedrock, src_tree, tmp_path, [g, g], evidence_context=0, batch=1)
    m0 = res["evidence"][0]
    assert m0["finding_ids"] == ["F1"]
    assert {"file": "a.vb", "intervals": [[2, 4]], "lines": 3} in m0["files"]
    assert "K-01" in m0["key_items_shown"]


def test_evidence_budget_truncation_is_flagged(fake_bedrock, src_tree, tmp_path):
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    res, _, _ = run(fake_bedrock, src_tree, tmp_path, [g], max_evidence_chars=1)
    assert res["evidence"][0]["truncated"] is True
    assert res["plan"]["evidence_truncated_batches"] == 1


def test_prompt_dir_records_what_the_judge_saw(fake_bedrock, src_tree, tmp_path):
    g = tool_response([judgment("F1", matches=[("K-01", "full")]),
                       judgment("F2", matches=[("K-02", "full")])])
    run(fake_bedrock, src_tree, tmp_path, [g], prompt_dir=tmp_path / "prompts")
    saved = list((tmp_path / "prompts").rglob("*.txt"))
    assert len(saved) == 1 and "Evaluation pass 1 of 1" in saved[0].read_text()


# ------------------------------------------------------------------- cost guard
def test_dry_run_makes_no_calls_and_reports_a_plan(fake_bedrock, src_tree, tmp_path):
    res, fake, _ = run(fake_bedrock, src_tree, tmp_path, [], runs=3, dry_run=True)
    assert fake.calls == [] and res["dry_run"] is True
    assert res["plan"]["calls"] == 3 and res["plan"]["est_cost_usd"] > 0
    assert res["usage"]["cost_usd"] == 0


def test_cost_cap_aborts_before_calling(fake_bedrock, src_tree, tmp_path):
    with pytest.raises(JudgeBudgetExceeded, match="estimated judge cost"):
        run(fake_bedrock, src_tree, tmp_path, [], runs=3, max_cost_usd=0.0000001)


def test_cost_cap_aborts_mid_run(fake_bedrock, src_tree, tmp_path):
    big = tool_response([judgment("F1", matches=[("K-01", "full")]),
                         judgment("F2", matches=[("K-02", "full")])],
                        in_tok=10_000_000, out_tok=1000)
    with pytest.raises(JudgeBudgetExceeded, match="passed --max-judge-cost-usd"):
        run(fake_bedrock, src_tree, tmp_path, [big, big], runs=2, max_cost_usd=1.0)


def test_plan_reports_scale(fake_bedrock, src_tree, tmp_path):
    res, _, _ = run(fake_bedrock, src_tree, tmp_path, [], runs=5, batch=1, dry_run=True)
    assert res["plan"]["batches"] == 2 and res["plan"]["runs"] == 5 and res["plan"]["calls"] == 10
    assert res["plan"]["schema_hash"] == sch.schema_hash()
    assert res["plan"]["temperature"] == 0.0
