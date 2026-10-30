# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unmeasured is never zero (fix 3). No AWS: bedrock_util.converse is scripted.

Regression: on the JC run qa_eval.py spent its whole $2.45 cap answering 16 of 25
questions, graded none, and reported ``correct_pct 0.0``; the suite said "ok".

    python3 -m pytest -q eval/benefits/tests
"""
from __future__ import annotations

import json

import pytest

from conftest_benefits import TOY, fake_converse, no_aws, ns  # noqa: F401  (fixtures)

import common
import fieldapp_eval
import impact_eval
import qa_eval


# ------------------------------------------------------------------ qa
def test_qa_fully_graded_is_measured(fake_converse, tmp_path):
    fake_converse()
    r = qa_eval.run(ns(tmp_path, qa=TOY / "qa.yaml", judge_batch=6))
    assert r["measurement"]["status"] == "measured"
    assert r["summary"]["questions_graded"] == 3
    assert r["summary"]["correct_pct"] == 100.0


def test_qa_budget_gone_before_grading_reports_null_not_zero(fake_converse, tmp_path):
    """Enough budget for answers, none for grading: the old code reported 0.0% correct."""
    fake = fake_converse()
    a = ns(tmp_path, qa=TOY / "qa.yaml", judge_batch=6, max_usd=0.0001)
    r = qa_eval.run(a)
    s = r["summary"]
    assert r["measurement"]["status"] == "not_measured"
    assert r["measurement"]["reason"]
    for k in ("correct_pct", "score_pct", "engineer_correct_pct", "product_correct_pct",
              "trick_correct_pct", "trick_abstain_pct", "citation_supported_pct", "wrong"):
        assert s[k] is None, (k, s[k])
    assert s["questions_graded"] == 0
    # nothing was graded, and nothing was answered either: the reserve protects grading money
    assert not [c for c in fake.calls if c["tag"] == "benefits:qa:judge"]
    # written JSON carries null, not 0
    common.write_outputs(tmp_path, "qa", r, qa_eval.to_markdown(r))
    on_disk = json.loads((tmp_path / "qa.json").read_text())
    assert on_disk["summary"]["correct_pct"] is None


def test_qa_partial_grading_reports_only_graded_items(fake_converse, tmp_path, monkeypatch):
    """Grade per batch; when the money runs out, the graded batch is reported, the rest
    is excluded from the percentages (not counted as wrong)."""
    fake_converse(qa_grade="correct")
    a = ns(tmp_path, qa=TOY / "qa.yaml", judge_batch=1, max_usd=5.0)
    real_check = common.Budget.check
    seen = {"judge": 0}

    def check(self, chars, out_tokens, what="", reserve=0.0):
        if what == "benefits:qa:judge":
            seen["judge"] += 1
            if seen["judge"] > 1:     # only the first batch can be graded
                self.stopped = "budget guard: test cap"
                raise common.BudgetExceeded(self.stopped)
        return real_check(self, chars, out_tokens, what, reserve)

    monkeypatch.setattr(common.Budget, "check", check)
    r = qa_eval.run(a)
    s = r["summary"]
    assert r["measurement"]["status"] == "partial"
    assert s["questions_graded"] == 1 and s["questions_total"] == 3
    assert s["correct_pct"] == 100.0          # 1 of 1 graded, not 1 of 3
    assert "1 of 3 questions graded" in r["measurement"]["reason"]


def test_qa_empty_key_is_an_error(tmp_path):
    k = tmp_path / "qa.yaml"
    k.write_text("questions: []\n")
    with pytest.raises(SystemExit) as e:
        qa_eval.load_questions(k)
    assert e.value.code == common.EXIT_BAD_KEY


# ------------------------------------------------------------------ impact
def test_impact_rule_recall_is_null_when_the_judge_did_not_run(fake_converse, tmp_path, monkeypatch):
    fake_converse()
    real_check = common.Budget.check

    def check(self, chars, out_tokens, what="", reserve=0.0):
        if what == "benefits:impact:judge":
            self.stopped = "budget guard: test cap"
            raise common.BudgetExceeded(self.stopped)
        return real_check(self, chars, out_tokens, what, reserve)

    monkeypatch.setattr(common.Budget, "check", check)
    a = ns(tmp_path, impact=TOY / "impact.yaml", key=TOY / "key.yaml", no_llm=False)
    r = impact_eval.run(a)
    s = r["summary"]
    assert s["rule_recall_pct"] is None        # was: silently the location fallback, or 0
    assert r["measurement"]["status"] == "not_measured"
    assert s["location_recall_pct"] is not None   # deterministic metrics are still reported
    assert all(c["rule_method"] == "ungraded" for c in r["changes"] if c["affected_rules"])


def test_impact_nothing_answered_gives_null_counts(fake_converse, tmp_path):
    fake_converse()
    a = ns(tmp_path, impact=TOY / "impact.yaml", key=TOY / "key.yaml", no_llm=False, max_usd=0.0)
    r = impact_eval.run(a)
    s = r["summary"]
    assert r["measurement"]["status"] == "not_measured"
    for k in ("location_recall_pct", "rule_recall_pct", "invalid_locations", "predicted_locations"):
        assert s[k] is None, k


# ------------------------------------------------------------------ field app
def test_fieldapp_without_judge_reports_null_recall(fake_converse, tmp_path, monkeypatch):
    fake_converse()
    real_check = common.Budget.check

    def check(self, chars, out_tokens, what="", reserve=0.0):
        if what == "benefits:fieldapp:judge":
            self.stopped = "budget guard: test cap"
            raise common.BudgetExceeded(self.stopped)
        return real_check(self, chars, out_tokens, what, reserve)

    monkeypatch.setattr(common.Budget, "check", check)
    a = ns(tmp_path, key=TOY / "field-app.yaml", request=None)
    r = fieldapp_eval.run(a)
    s = r["summary"]
    assert r["measurement"]["status"] == "not_measured"
    for k in ("recall_full_pct", "recall_any_pct", "precision_keyed_pct", "wrong_requirements",
              "invented_requirements", "key_items_contradicted"):
        assert s[k] is None, k
    assert all(i["coverage"] == "ungraded" for i in r["items"])


def test_fieldapp_ids_the_judge_omitted_are_ungraded_not_missed(fake_converse, tmp_path):
    fake_converse()   # the fake judge grades only FA-1
    a = ns(tmp_path, key=TOY / "field-app.yaml", request=None)
    r = fieldapp_eval.run(a)
    cov = {i["id"]: i["coverage"] for i in r["items"]}
    assert cov["FA-1"] == "full"
    assert all(v == "ungraded" for k, v in cov.items() if k != "FA-1")
    assert r["measurement"]["status"] == "partial"
    assert r["summary"]["recall_full_pct"] == 100.0       # 1 of 1 graded


# ------------------------------------------------------------------ plans
def test_dry_run_plans_cost_without_calls(fake_converse, tmp_path):
    fake = fake_converse()
    p = qa_eval.plan(ns(tmp_path, qa=TOY / "qa.yaml", judge_batch=6))
    assert p["est_cost_usd"] > 0 and p["by_step"]["answer"]["calls"] == 3
    assert p["by_step"]["judge"]["calls"] == 1
    assert p["region"] == "us-east-1"
    assert fake.calls == []
