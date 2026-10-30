# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test mining, docs mining, merge / dedupe / corroboration. No AWS, no dotnet."""
from __future__ import annotations

import kg_docs
import kg_merge as MG
import kg_testutil as U
from kg_source import Candidate, loc


def mined():
    files, idx = U.load("mini-cs")
    return U.T.mine_tests(files, idx), files, idx


# --------------------------------------------------------------------------- test mining
def test_xunit_nunit_mstest_methods_are_mined():
    t, _, _ = mined()
    names = sorted(c.extra["test"] for c in t)
    assert names == ["OrderAddLine.CantAddLineWithZeroQuantity", "OrderAddLine.TotalSumsLineAmounts",
                     "OrderLateMsTest.IsLateWhenPlacedBeforeCutoff",
                     "OrderSubmitNUnit.SubmitMovesDraftToSubmitted"]
    assert {c.extra["framework_attr"] for c in t} == {"Fact", "Theory", "Test", "TestMethod"}
    assert all(c.source == "tests" and c.evidence_class == "Verified" for c in t)


def test_statement_uses_name_assertion_and_system_under_test():
    t, _, _ = mined()
    c = U.by_text(t, "can't add line with zero quantity")
    assert "Assert.Throws<ArgumentException>" in c.statement
    assert "Order.AddLine" in c.extra["sut"]
    # the test location plus the production method it exercises
    files = [l["file"] for l in c.locations]
    assert files[0] == "tests/UnitTests/OrderTests.cs" and "src/Domain/Order.cs" in files
    th = U.by_text(t, "total sums line amounts")
    assert "(2 data rows)" in th.statement and "Order.Total" in th.extra["sut"]


def test_sut_resolution_ignores_assertions_and_mocks():
    t, _, _ = mined()
    for c in t:
        assert not any(s.split(".")[-1] in ("Equal", "Throws", "AreEqual", "IsTrue") for s in c.extra["sut"])


def test_sut_resolution_prefers_facts_call_graph():
    files, idx = U.load("mini-cs")
    facts = {"cs": {"calls": [{"from": "OrderLateMsTest.IsLateWhenPlacedBeforeCutoff",
                               "to": "Order.IsLate", "file": "tests/OtherFrameworks/NUnitAndMsTest.cs",
                               "line": 28, "callee_file": "src/Domain/Order.cs"}]}}
    t = U.T.mine_tests(files, idx, facts)
    late = U.by_text(t, "is late when placed before cutoff")
    assert late.extra["sut"] == ["Order.IsLate"]


# --------------------------------------------------------------------------- docs
def test_docs_feature_bullets_are_inferred_workflow_items():
    d = kg_docs.mine_docs(U.FIX / "mini-cs", ["README.md"])
    stmts = [c.statement for c in d]
    assert any("up to fifty order lines" in s for s in stmts)
    assert not any("build script" in s for s in stmts)          # "Getting started" is skipped
    assert all(c.evidence_class == "Inferred" and c.type == "workflow" and c.source == "docs" for c in d)


# --------------------------------------------------------------------------- merge
def _h(stmt, a, b=None, f="x.cs", det="guard_ardalis", **kw):
    return Candidate("rule", stmt, [loc(f, a, b)], "harvest", det, **kw)


def test_identical_harvest_items_dedupe_and_keep_provenance():
    c1 = _h("Basket item quantity must not be negative", 10)
    c2 = _h("Basket item quantity must not be negative", 10, det="literal_threshold")
    c3 = _h("Order total is computed from lines", 30)
    items = MG.to_items(MG.merge([c1, c2, c3]))
    assert len(items) == 2
    q = next(i for i in items if "quantity" in i["statement"])
    assert q["source"] == ["harvest:guard_ardalis", "harvest:literal_threshold"]
    assert len(q["provenance"]) == 2
    assert "corroborated" not in q["flags"]            # two detectors are one source


def test_harvest_items_at_different_places_stay_separate():
    items = MG.to_items(MG.merge([_h("Quantity must not be negative", 10),
                                  _h("Quantity must not be negative", 50)]))
    assert len(items) == 2


def test_test_corroborates_the_rule_it_exercises():
    t, files, idx = mined()
    h = U.H.harvest(files, idx)
    items = MG.to_items(MG.merge(h + t))
    q = next(i for i in items if i["statement"].startswith("Order quantity must be greater than zero"))
    assert "corroborated" in q["flags"]
    assert q["source"] == ["harvest:guard_ardalis", "tests"]
    assert any("can't add line with zero quantity" in p["statement"] for p in q["provenance"])
    # a test merges into at most one item
    for c in t:
        owners = [i for i in items if any(p["statement"] == c.statement for p in i["provenance"])]
        assert len(owners) == 1


def test_docs_join_by_statement_similarity_without_location_overlap():
    h = _h("Order holds at most fifty order lines", 40, f="src/Domain/Order.cs")
    d = Candidate("workflow", "Documented capability (Features): buyers can place orders with up to fifty order lines",
                  [loc("README.md", 5)], "docs", "markdown", evidence_class="Inferred")
    items = MG.to_items(MG.merge([h, d]))
    assert len(items) == 1 and "corroborated" in items[0]["flags"]
    assert items[0]["evidence_class"] == "Verified"


def test_ids_are_stable_and_unique():
    t, files, idx = mined()
    h = U.H.harvest(files, idx)
    a = MG.to_items(MG.merge(h + t))
    b = MG.to_items(MG.merge(h + t))
    assert [i["id"] for i in a] == [i["id"] for i in b]
    assert len({i["id"] for i in a}) == len(a)
    assert all(i["id"].split("-")[0] in ("BR", "DM", "ST", "WF", "EX", "IF", "PM") for i in a)
    assert all(i["status"] == "unreviewed" for i in a)
