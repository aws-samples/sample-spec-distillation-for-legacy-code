# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Spec retrieval for --adapter spec (fix 6). No AWS.

Regression: SpecAdapter sent ``text[:max_spec_chars]``, so on the 556K-char JC business
spec every answer was written from its first 150K characters. Now the spec is chunked by
heading / requirement and the BM25-best chunks are packed into the budget.
"""
from __future__ import annotations

from conftest_benefits import TOY, fake_converse, no_aws  # noqa: F401  (fixtures)

import common
from retrieval import SpecRetriever, chunk_spec, tokenize


def big_spec(n_filler: int = 520) -> str:
    """A spec whose only mention of the field-app limit is near the END."""
    parts = ["# Spec", "", "Intro paragraph."]
    for i in range(n_filler):
        parts += [f"#### [RL-{i:04d}] Rule: Filler topic {i}",
                  f"Invoices for vendor group {i} are posted nightly and reconciled weekly. " * 4, ""]
    parts += ["#### [IF-9999] Endpoint: Field request item limit",
              "A field request may carry at most 50 items (FIELD_MAX_ITEMS); more returns TOO_MANY_ITEMS.",
              "Evidence: ClientSide/JC/JC/clsJCFieldSync.vb:L27-L27", ""]
    return "\n".join(parts)


def test_tokenize_splits_identifiers():
    t = tokenize("ExpFlag FIELD_MAX_ITEMS requiresExecApproval the of")
    assert {"expflag", "exp", "flag", "field_max_item", "max", "item",
            "requiresexecapproval", "require", "exec", "approval"} <= set(t)
    assert "the" not in t and "of" not in t


def test_chunks_follow_headings_and_cover_every_line():
    text = big_spec(5)
    chunks = chunk_spec(text)
    assert chunks[0].heading == "Spec"
    assert any(c.heading.endswith("[IF-9999] Endpoint: Field request item limit") for c in chunks)
    covered = set()
    for c in chunks:
        covered.update(range(c.start_line, c.end_line + 1))
        assert "\n".join(text.split("\n")[c.start_line - 1:c.end_line]) == c.text
    nonblank = {i for i, l in enumerate(text.split("\n"), 1) if l.strip()}
    assert nonblank <= covered


def test_long_sections_are_split_within_the_chunk_size():
    text = "# Index\n" + "\n".join(f"| R-{i} | something about rule {i} |" for i in range(2000))
    chunks = chunk_spec(text, max_chunk_chars=4000)
    assert len(chunks) > 5 and max(c.chars for c in chunks) <= 4000


def test_retrieval_finds_a_section_beyond_the_old_truncation_point():
    text = big_spec()
    assert len(text) > 150_000 and text.index("FIELD_MAX_ITEMS") > 150_000
    body, info = SpecRetriever(text).select(
        "How many items can a field request carry, and what error is returned?", 20_000)
    assert "FIELD_MAX_ITEMS" in body
    assert info["mode"] == "retrieval" and info["chars"] <= 20_000
    used = [c["heading"] for c in info["chunks"]]
    assert any("IF-9999" in h for h in used)
    assert all({"id", "heading", "spec_lines", "chars", "score"} <= set(c) for c in info["chunks"])


def test_small_spec_is_sent_whole():
    text = "# S\n\nOne rule."
    body, info = SpecRetriever(text).select("anything", 60_000)
    assert body == text and info["mode"] == "full"


def test_no_term_match_falls_back_to_the_opening_chunks():
    body, info = SpecRetriever(big_spec()).select("zebra quokka", 5_000)
    assert info["fallback"] == "no_term_match_leading_chunks"
    assert body.startswith("[spec chunk C1") and info["chars"] <= 5_000


def test_plural_folding_matches_singular_questions():
    assert tokenize("invoices entries boxes") == ["invoice", "entry", "box"]


def test_selection_is_deterministic():
    r = SpecRetriever(big_spec())
    q = "vendor group 7 posting"
    assert r.select(q, 15_000) == r.select(q, 15_000)


def test_spec_adapter_prompt_carries_the_retrieved_chunks(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text(big_spec())
    b = common.Budget(1.0, tmp_path)
    ad = common.SpecAdapter(b, spec, 20_000)
    prompt, info = ad.build_prompt("qa", {"id": "Q", "question": "field request item limit?"}, "INSTR")
    assert "FIELD_MAX_ITEMS" in prompt and "EXCERPTS" in prompt
    assert len(prompt) < 25_000
    assert ad.truncated is True and ad.context_summary()["mode"] == "retrieval"
    assert info["chunks_used"] >= 1
    assert ad.prompt_chars("qa", {"id": "Q", "question": "field request item limit?"}, "INSTR") \
        >= len(prompt)


def test_qa_rows_record_which_chunks_were_used(fake_converse, tmp_path):
    import qa_eval
    from conftest_benefits import ns
    spec = tmp_path / "spec.md"
    spec.write_text(big_spec())
    fake_converse()
    r = qa_eval.run(ns(tmp_path, adapter="spec", spec=spec, max_spec_chars=20_000,
                       qa=TOY / "qa.yaml", judge_batch=6))
    ctx = r["questions"][0]["context"]
    assert ctx["mode"] == "retrieval" and ctx["chunks"]
    assert r["context"]["mode"] == "retrieval"
