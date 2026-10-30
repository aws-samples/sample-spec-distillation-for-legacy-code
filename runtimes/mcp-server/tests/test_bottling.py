# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bottling (no AWS): section-bounded splitting, deterministic citation carry-through and
verification, customer clean-up (jargon, titles, duplicate headings), requirement de-duplication
with no loss, split-and-retry / fail-closed on max_tokens, and a small exec-summary input."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bedrock_client  # noqa: E402
import bottling  # noqa: E402

REAL_RUN = HERE.parents[2].parent.parent.parent / "output" / "ap-invoices"   # main checkout
if not (REAL_RUN / "master_blend" / "master_blend.md").exists():
    REAL_RUN = HERE.parents[2] / "output" / "ap-invoices"


def _blend(sections=4, blocks=30, body=300):
    out = ["# T — Master Blend Specification\n\n**Sections:** 4  \n\n---\n\n"]
    for s in range(sections):
        out.append(f"## Section {s}\n\n")
        for b in range(blocks):
            out.append(f"### S{s} item {b}\n\n" + ("x" * body) + "\n\n")
        out.append("---\n\n")
    return "".join(out)


# A miniature master blend in the real layout: duplicated generated ## heading, intro prose with
# pipeline vocabulary, an included still document, a requirement repeated in two sections, score
# lines, and an internal methodology section.
MINI = """# acme — Master Blend Specification

**Generated:** 2026-10-05
**Sections:** 3

---

## Business Object Model

## Business Object Model

This section captures the highest-convergence findings across all 11 stills. Confidence is high.

<!-- From data/persistent_objects.md -->
### data — Persistent Objects

<!-- BLOCK_MANIFEST: count=2 verify=true -->

This document specifies the invoice data model.

### Invoice Entities

#### [DT-0001] Entity: Invoice
<!-- ref:aaaa -->
- Definition: A vendor bill. The Data Still found it in 9/11 stills.
- Statement:
  ```formal
  REQUIRE Invoice.Amount <> 0
  ```
- Evidence: src/inv.vb#L2-L4, db/tables.sql#L1
- Business Relevance: 5
- Evidence Diversity: 4
- Confidence: 5

#### [RL-0001] Rule: Vendor Must Be Active
<!-- ref:bbbb -->
- Definition: Invoices need an active vendor; the certificate is still expired check is separate.
- Evidence: src/inv.vb#L5-L6; nowhere/missing.vb#L1-L2; src/inv.vb#L90-L99
- Confidence: 4

---

## Business Rules

## Business Rules

Rules section intro.

<!-- From rule/business_rule_catalog.md -->
### rule — Business Rule Catalog

#### [RL-0001] Rule: Vendor Must Be Active
<!-- ref:bbbb -->
- Definition: Invoices need an active vendor; the certificate is still expired check is separate.
- Evidence: src/inv.vb#L5-L6; nowhere/missing.vb#L1-L2; src/inv.vb#L90-L99
- Confidence: 4

---

## Confidence Map and Methodology

### Specification Distillation — Reconciliation Report

| Still | Concepts | Avg Confidence |
|---|---|---|
| data | 4 | 4.6 |
"""


def _mini_run(tmp_path, blend=MINI):
    run = tmp_path / "run"
    (run / "master_blend").mkdir(parents=True)
    (run / "master_blend" / "master_blend.md").write_text(blend, encoding="utf-8")
    page = run / ".pages" / "page_000"
    page.mkdir(parents=True)
    lines = ["[FILE: src/inv.vb KIND: raw SIZE: 10]"]
    lines += [f"{i:5d} | code {i}" for i in range(1, 11)]
    lines += ["[END FILE: src/inv.vb]", "[FILE: db/tables.sql KIND: raw SIZE: 5]",
              "    1 | create table", "    2 | x", "[END FILE: db/tables.sql]"]
    (page / "concat.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


class _R:
    def __init__(self, text, stop="end_turn"):
        self.output_text, self.stop_reason = text, stop
        self.input_tokens, self.output_tokens = len(text) // 4, len(text) // 4
        self.cache_read_tokens, self.cache_write_tokens = 0, 0


class _FakeLLM:
    """Echo model: restates each block (keeps heading, writes a bogus Evidence line and a
    jargon sentence that the deterministic clean-up must remove). Records every call."""

    def __init__(self, drop=(), truncate_over=None):
        self.calls, self.drop, self.truncate_over = [], set(drop), truncate_over

    def __call__(self, _m, _model, system_blocks, user, max_tokens):
        self.calls.append({"system": system_blocks, "user": user})
        if "REQUIREMENT BLOCKS:" in user:
            body = user.split("REQUIREMENT BLOCKS:\n\n", 1)[1]
            if self.truncate_over and len(body) > self.truncate_over:
                return _R("TRUNC", stop="max_tokens")
            out = []
            for m in re.finditer(r"(?ms)^(#### \[([A-Z]+-\d+)\][^\n]*)\n(.*?)(?=^#### \[|\Z)", body):
                if m.group(2) in self.drop:
                    continue
                out.append(f"{m.group(1)}\n**Requirement:** The system SHALL keep {m.group(2)}. "
                           f"This was confirmed across 5 stills.\n{m.group(3).strip()}\n"
                           f"Evidence: wrong/file.vb:L1-L2\n")
            return _R("\n".join(out))
        if user.startswith("==="):
            titles = re.findall(r"^=== (.+) ===$", user, flags=re.M)
            return _R("\n".join(f"=== {t} ===\nThis section specifies {t}. Convergence was high."
                                for t in titles))
        return _R("## System Overview\nAn AP system.\n\n## Critical Findings\n- [RL-0001] active "
                  "vendors only.\n\n## Known Gaps\nNone recorded.")


@pytest.fixture
def fake(monkeypatch):
    f = _FakeLLM()
    monkeypatch.setattr(bedrock_client, "call_llm_blocks", f)
    monkeypatch.setattr(bedrock_client, "create_llm_client", lambda *a: None)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    return f


# ── splitting (kept from the chunked-bottling design) ────────────────────────

def test_split_is_lossless_and_section_bounded():
    blend = _blend()
    chunks = bottling._split_blend(blend, limit=2000)
    assert "".join(t for _, t in chunks) == blend
    assert all(len(t) <= 2000 for _, t in chunks)
    for title, text in chunks:
        assert len([ln for ln in text.splitlines() if ln.startswith("## ")]) <= 1
        if title.startswith("Section "):
            assert f"### S{title.split()[1]} " in text
    assert sum(t.count("\n### ") + t.startswith("### ") for _, t in chunks) == 4 * 30


def test_oversized_block_split_on_paragraphs():
    blend = "## Big\n\n### one\n\n" + "\n\n".join("p" * 500 for _ in range(20)) + "\n"
    chunks = bottling._split_blend(blend, limit=1500)
    assert "".join(t for _, t in chunks) == blend
    assert len(chunks) > 1 and all(len(t) <= 1500 for _, t in chunks)


def test_heading_retention():
    r = bottling.heading_retention("### a\n### b\n", "### a\n")
    assert r == {"blend_h3": 2, "spec_h3": 1, "ratio": 0.5}


def test_section_heading_restored():
    chunk = "## Interface Contract Summary\n\nbody\n"
    assert bottling._restore_section_heading(chunk, "### Interface Contract Summary\n\nb") == \
        "## Interface Contract Summary\n\nb"
    assert bottling._restore_section_heading(chunk, "b only") == "## Interface Contract Summary\nb only"
    assert bottling._restore_section_heading("### x\n", "### y\n") == "### y\n"


# ── citations ────────────────────────────────────────────────────────────────

def test_parse_and_format_citations():
    c = bottling.parse_citations("a/b.vb#L1-L5, c.sql#L9; Reports/X (412).txt#L2-L3, prose only")
    assert c == [("a/b.vb", 1, 5), ("c.sql", 9, 9), ("Reports/X (412).txt", 2, 3)]
    assert bottling.format_citation("a/b.vb", 1, 5) == "a/b.vb:L1-L5"
    assert bottling.format_citation("c.sql", 9, 9) == "c.sql:L9"


def test_bold_evidence_label_with_colon_inside():
    # `**Evidence:** path` (colon inside the bold) left "** " in front of the first citation, so
    # it failed CITE_RE / resolution and was dropped (18 of 437 in the ap-invoices demo run).
    for ln in ("**Evidence:** clsAPInvoiceBL.vb#L181-L185, b.sql#L3",
               "- **Evidence**: clsAPInvoiceBL.vb#L181-L185, b.sql#L3",
               "- Evidence: clsAPInvoiceBL.vb#L181-L185, b.sql#L3"):
        m = bottling.EVIDENCE_LINE_RE.match(ln)
        assert m, ln
        assert bottling.parse_citations(m.group(1)) == [("clsAPInvoiceBL.vb", 181, 185), ("b.sql", 3, 3)]
    body, cites = bottling._clean_block_body(["Rule text.", "", "**Evidence:** a/x.vb#L2-L4"])
    assert body == "Rule text." and cites == [("a/x.vb", 2, 4)]


def test_citation_verification_against_pages(tmp_path):
    run = _mini_run(tmp_path)
    idx = bottling.load_source_index(run)
    assert idx == {"src/inv.vb": 10, "db/tables.sql": 2}
    assert bottling.resolve_citation(("inv.vb", 2, 4), idx) == ("src/inv.vb", 2, 4)   # suffix match
    assert bottling.resolve_citation(("src/inv.vb", 9, 12), idx) is None              # past EOF
    assert bottling.resolve_citation(("nowhere/missing.vb", 1, 2), idx) is None


def test_pages_phantom_trailing_line_and_whole_file_clamp(tmp_path):
    page = tmp_path / ".pages" / "page_000"
    page.mkdir(parents=True)
    # the pager numbers an empty line after the final newline: a 3-line file shows 4 numbers
    page.joinpath("concat.txt").write_text(
        "[FILE: a.vb KIND: raw SIZE: 3]\n    1 | x\n    2 | y\n    3 | z\n    4 | \n[END FILE: a.vb]\n")
    idx = bottling.load_source_index(tmp_path)
    assert idx == {"a.vb": 3}
    assert bottling.resolve_citation(("a.vb", 1, 4), idx) == ("a.vb", 1, 3)   # whole-file cite
    assert bottling.resolve_citation(("a.vb", 1, 5), idx) is None
    src = tmp_path / "src"
    src.mkdir()
    src.joinpath("a.vb").write_text("x\ny\nz\n")
    assert bottling.load_source_index(tmp_path, src) == {"a.vb": 3}      # source root preferred


def test_citations_carried_deterministically(tmp_path, fake):
    run = _mini_run(tmp_path)
    out = tmp_path / "out"
    res = bottling.run_bottling(str(run / "master_blend"), str(out), "acme")
    spec = (out / "bottled_spec.md").read_text()
    # the model's bogus Evidence line is gone; the blend's citations are carried and verified
    assert "wrong/file.vb" not in spec
    assert "Evidence: src/inv.vb:L2-L4, db/tables.sql:L1" in spec
    assert "Evidence: src/inv.vb:L5-L6\n" in spec                  # unresolvable ones dropped
    assert len(re.findall(r"(?m)^Evidence: ", spec)) == 2
    rep = (out / "bottling_report.json").read_text()
    assert "nowhere/missing.vb:L1-L2" in rep and "src/inv.vb:L90-L99" in rep
    assert res["citations_kept"] == 3 and res["requirements_with_citation"] == 2
    # the model never sees citations (they are stripped from its input)
    chunk_users = [c["user"] for c in fake.calls if "REQUIREMENT BLOCKS:" in c["user"]]
    assert chunk_users and not any("#L" in u or "Evidence:" in u for u in chunk_users)


# ── customer clean-up ────────────────────────────────────────────────────────

def test_jargon_cleanup():
    txt = "\n".join([
        "# acme — Master Blend Specification",
        "### rule — Business Rule Catalog",
        "- Definition: Invoices need a vendor. This was confirmed across 5 stills.",
        "- Confidence: 5",
        "- Evidence Diversity: 3",
        "The certificate is still expired, so release is denied.",
        "Convergence across stills was high.",
        "| Still | Concepts |",
        "<!-- ref:abc -->",
        "See clsAPInvoiceBL.vb#L133-L136 for the check.",
        "```formal",
        "REQUIRE x  -- confidence: 5 stays verbatim in code",
        "```",
    ])
    out = bottling.clean_customer_text(txt)
    assert out.splitlines()[0] == "# acme — Business Specification"
    assert "### Business Rule Catalog" in out
    assert "- Definition: Invoices need a vendor." in out
    assert "stills" not in out.split("```")[0].lower()
    assert "Confidence: 5" not in out and "Evidence Diversity" not in out
    assert "The certificate is still expired" in out            # English 'still' is kept
    assert "Convergence" not in out and "<!--" not in out and "| Still |" not in out
    assert "clsAPInvoiceBL.vb:L133-L136" in out
    assert "REQUIRE x  -- confidence: 5 stays verbatim in code" in out
    assert bottling.jargon_hits(out) == []


def test_heading_dedupe():
    spec = "## A\n\n## A\n\nbody\n\n## B\n\ntext\n\n## B\nmore\n"
    out = bottling.dedupe_headings(spec)
    assert out.count("## A") == 1
    assert out.count("## B") == 2        # not adjacent-empty: both kept


def test_spec_is_customer_presentable(tmp_path, fake):
    run = _mini_run(tmp_path)
    out = tmp_path / "out"
    bottling.run_bottling(str(run / "master_blend"), str(out), "acme")
    spec = (out / "bottled_spec.md").read_text()
    readme = (out / "README.md").read_text()
    assert spec.startswith("# acme — Business Specification\n")
    h2 = re.findall(r"(?m)^## (.+)$", spec)
    assert h2 == ["Requirements Index", "Business Object Model", "Business Rules"]
    assert "Confidence Map" not in spec and "Reconciliation Report" not in spec
    assert bottling.jargon_hits(spec) == [] and bottling.jargon_hits(readme) == []
    assert "### Persistent Objects" in spec and "data —" not in spec
    # requirements index: one line per requirement with id, title, section and first citation
    assert "| DT-0001 | Entity: Invoice | Business Object Model | src/inv.vb:L2-L4 (+1) |" in spec
    assert "| RL-0001 | Rule: Vendor Must Be Active | Business Rules | src/inv.vb:L5-L6 |" in spec
    # a requirement repeated in two sections is specified once (home = Business Rules) and
    # cross-referenced elsewhere
    assert spec.count("#### [RL-0001]") == 1
    assert "- See [RL-0001] Rule: Vendor Must Be Active (specified in Business Rules)." in spec
    assert "REQUIRE Invoice.Amount <> 0" in spec
    assert "| **Total** | **2** |" in readme


# ── no loss / truncation ─────────────────────────────────────────────────────

def test_missing_requirement_retried_then_falls_back(tmp_path, monkeypatch):
    f = _FakeLLM(drop={"DT-0001"})
    monkeypatch.setattr(bedrock_client, "call_llm_blocks", f)
    monkeypatch.setattr(bedrock_client, "create_llm_client", lambda *a: None)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    run = _mini_run(tmp_path)
    out = tmp_path / "out"
    bottling.run_bottling(str(run / "master_blend"), str(out), "acme")
    spec = (out / "bottled_spec.md").read_text()
    assert "#### [DT-0001] Entity: Invoice" in spec
    assert "A vendor bill." in spec and "REQUIRE Invoice.Amount <> 0" in spec   # source text kept
    assert "Data Still" not in spec
    rep = (out / "bottling_report.json").read_text()
    assert '"fallback_to_source_text": [\n      "DT-0001"' in rep
    retry_calls = [c for c in f.calls if "section: retry" in c["user"]]
    assert len(retry_calls) == 1


def _many_blocks(n=12, body=400):
    out = ["# T — Master Blend Specification\n\n## Business Rules\n\nintro\n\n"]
    for i in range(n):
        out.append(f"#### [RL-{i:04d}] Rule {i}\n- Definition: {'y' * body}\n"
                   f"- Evidence: src/inv.vb#L1-L2\n\n")
    return "".join(out)


def test_batches_split_on_max_tokens(tmp_path, monkeypatch):
    f = _FakeLLM(truncate_over=1500)
    monkeypatch.setattr(bedrock_client, "call_llm_blocks", f)
    monkeypatch.setattr(bedrock_client, "create_llm_client", lambda *a: None)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    monkeypatch.setattr(bottling, "MIN_SPLIT_CHARS", 100)
    run = _mini_run(tmp_path, _many_blocks())
    out = tmp_path / "out"
    res = bottling.run_bottling(str(run / "master_blend"), str(out), "T")
    spec = (out / "bottled_spec.md").read_text()
    assert "TRUNC" not in spec
    ids = re.findall(r"(?m)^#### \[(RL-\d+)\]", spec)
    assert ids == [f"RL-{i:04d}" for i in range(12)]                      # blend order kept
    assert res["spec_calls"] > res["spec_chunks"]


def test_fails_closed_when_still_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(bedrock_client, "call_llm_blocks",
                        lambda *a, **k: _R("x", stop="max_tokens"))
    monkeypatch.setattr(bedrock_client, "create_llm_client", lambda *a: None)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: None)
    run = _mini_run(tmp_path, _many_blocks(n=1, body=5000))
    with pytest.raises(RuntimeError, match="truncated"):
        bottling.run_bottling(str(run / "master_blend"), str(tmp_path / "out"), "T")


def test_unclosed_fence_in_truncated_block_does_not_swallow_the_rest():
    blend = ("## Business Rules\n\n#### [RL-0001] A\n- Statement:\n  ```formal\n  REQUIRE (x, y\n\n"
             "<!-- From rule/deps.md -->\n### rule — Deps\n\n#### [RL-0002] B\n- Definition: b\n")
    _, secs, variants = bottling.parse_blend(blend)
    assert list(variants) == ["RL-0001", "RL-0002"]
    body = variants["RL-0001"][0].body
    assert body.count("```") == 2                                          # fence closed


# ── cost: exec summary input and prompt caching ─────────────────────────────

def test_exec_summary_input_is_small_and_system_prompt_cached(tmp_path, fake):
    blend = _many_blocks(n=200, body=1500)
    run = _mini_run(tmp_path, blend)
    out = tmp_path / "out"
    bottling.run_bottling(str(run / "master_blend"), str(out), "T")
    readme_call = fake.calls[-1]
    assert "## System Overview" in readme_call["system"][0]["text"]
    assert len(readme_call["user"]) < 0.1 * len(blend)
    assert "y" * 200 not in readme_call["user"]                            # no block bodies
    for c in fake.calls:
        assert c["system"][0]["cache_control"] == {"type": "ephemeral"}
    chunk_systems = {c["system"][0]["text"] for c in fake.calls if "REQUIREMENT BLOCKS:" in c["user"]}
    assert len(chunk_systems) == 1                                         # one shared, cacheable prefix


def test_bottling_logs_each_call_once_with_cache_tokens(tmp_path, monkeypatch):
    f = _FakeLLM()
    logged = []
    monkeypatch.setattr(bedrock_client, "call_llm_blocks", f)
    monkeypatch.setattr(bedrock_client, "create_llm_client", lambda *a: None)
    monkeypatch.setattr(bedrock_client, "log_llm_call", lambda **k: logged.append(k))
    run = _mini_run(tmp_path)
    bottling.run_bottling(str(run / "master_blend"), str(tmp_path / "out"), "acme")
    assert len(logged) == len(f.calls)
    assert all(k["still"] == "bottling" and "cache_read_tokens" in k for k in logged)
    assert {k["phase"] for k in logged} == {"chunk", "overview", "readme"}


# ── the real ap-invoices run (read-only; skipped when absent) ────────────────

@pytest.mark.skipif(not (REAL_RUN / "master_blend" / "master_blend.md").exists(),
                    reason="output/ap-invoices not present")
def test_real_blend_offline(tmp_path, fake):
    blend = (REAL_RUN / "master_blend" / "master_blend.md").read_text(encoding="utf-8")
    ids = set(re.findall(r"(?m)^#### \[([A-Z]+-\d+)\]", blend))
    _, sections, variants = bottling.parse_blend(blend)
    assert set(variants) == ids
    assert len([s for s in sections if s.title == "Architecture Principles"]) == 1
    canon = bottling.choose_canonical(variants, sections)
    rep = bottling.verify_citations(canon, bottling.load_source_index(REAL_RUN))
    assert rep["verified_against_source"] and not rep["citations_dropped"]
    assert rep["requirements_with_citation"] >= len(ids) - 2
    readme_in = bottling.build_readme_input("ap-invoices", sections, canon, {}, rep)
    assert len(readme_in) < 0.05 * len(blend)
