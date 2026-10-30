# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The optional AI draft, with the model call mocked: citation verification, de-duplication,
cost cap, call ceiling, default model. No AWS: a real Bedrock call would fail the test."""
from __future__ import annotations

import json

import kg_llm
import kg_testutil as U


def fake(responses, cost=0.01, seen=None):
    it = iter(responses)

    def call(prompt, system, max_tokens, model):
        if seen is not None:
            seen.append({"prompt": prompt, "system": system, "model": model})
        return {"text": json.dumps({"rules": next(it)}), "cost_usd": cost}
    return call


GOOD = {"statement": "Submitted orders keep their buyer", "type": "rule",
        "file": "src/Domain/Order.cs", "start_line": 60, "end_line": 60,
        "quote": "Status = OrderStatus.Submitted;"}


def test_kept_items_are_cited_inferred_llm_draft():
    files, _ = U.load("mini-cs")
    out, rep = kg_llm.draft(files, [], max_usd=1.0, call=fake([[GOOD]] * 5), max_calls=1)
    assert rep["calls"] == 1 and rep["kept"] == 1
    c = out[0]
    assert c.source == "llm-draft" and c.evidence_class == "Inferred"
    assert c.locations == [{"file": "src/Domain/Order.cs", "start_line": 60, "end_line": 60}]


def test_uncited_or_misquoted_proposals_are_dropped():
    files, _ = U.load("mini-cs")
    bad = [
        {**GOOD, "file": "src/Nope.cs"},                                  # file_not_found
        {**GOOD, "start_line": 900, "end_line": 901},                     # out_of_range
        {**GOOD, "quote": "this text is nowhere in the file"},            # quote_not_in_cited_lines
        {**GOOD, "quote": ""},                                            # no_quote
        {**GOOD, "start_line": "x"},                                      # bad_line_numbers
    ]
    out, rep = kg_llm.draft(files, [], max_usd=1.0, call=fake([bad]), max_calls=1)
    assert out == []
    assert rep["dropped"] == {"file_not_found": 1, "out_of_range": 1, "quote_not_in_cited_lines": 1,
                              "no_quote": 1, "bad_line_numbers": 1}


def test_duplicates_of_existing_items_are_dropped():
    files, _ = U.load("mini-cs")
    known = [{"id": "BR-1", "statement": "Submitted orders keep their buyer"}]
    out, rep = kg_llm.draft(files, known, max_usd=1.0, call=fake([[GOOD]]), max_calls=1)
    assert out == [] and rep["dropped"] == {"duplicates_existing_item": 1}


def test_cost_cap_is_checked_before_every_call():
    files, _ = U.load("mini-cs")
    seen = []
    # a tiny chunk size forces several calls; the cap admits none of them
    out, rep = kg_llm.draft(files, [], max_usd=0.0001, call=fake([[GOOD]] * 50, seen=seen),
                            chunk_chars=2000)
    assert rep["calls"] == 0 and seen == [] and rep["stopped"].startswith("budget")
    # a cap that admits some calls stops at the cap, never above it: each mocked call costs
    # exactly its worst-case estimate, which is the most a real call can cost
    def worst(prompt, system, max_tokens, model):
        return {"text": '{"rules": []}',
                "cost_usd": kg_llm.worst_case_usd(len(prompt) + len(system), max_tokens, 3.0, 15.0)}
    out, rep = kg_llm.draft(files, [], max_usd=0.04, call=worst, chunk_chars=2000, max_tokens=1000)
    assert rep["calls"] >= 1 and rep["spent_usd"] <= 0.04
    assert rep["stopped"] and rep["stopped"].startswith("budget")


def test_call_ceiling():
    files, _ = U.load("mini-cs")
    out, rep = kg_llm.draft(files, [], max_usd=100.0, call=fake([[]] * 50), chunk_chars=1500,
                            max_calls=2)
    assert rep["calls"] == 2 and rep["stopped"] == "call ceiling 2 reached"


def test_prompt_differs_from_pipeline_extraction_and_shows_known_items():
    files, _ = U.load("mini-cs")
    seen = []
    kg_llm.draft(files, [{"id": "BR-9", "statement": "Order quantity must be greater than zero"}],
                 max_usd=1.0, call=fake([[]], seen=seen), max_calls=1)
    p = seen[0]["prompt"]
    assert "BR-9: Order quantity must be greater than zero" in p
    assert "NOT in the key" in p and "verbatim" in p
    assert "  38: " in p                                   # numbered source lines
    assert "tests/UnitTests" not in p                       # tests are not drafted from


def test_default_model_is_the_eval_judge_model(monkeypatch):
    import bedrock_util
    monkeypatch.setattr(bedrock_util, "MODEL_ID", "judge-model-under-test")
    assert kg_llm.default_model() == "judge-model-under-test"


def test_unparseable_response_is_recorded():
    files, _ = U.load("mini-cs")
    out, rep = kg_llm.draft(files, [], max_usd=1.0, call=lambda *a: {"text": "no json", "cost_usd": 0},
                            max_calls=1)
    assert out == [] and rep["dropped"] == {"unparseable_response": 1}
