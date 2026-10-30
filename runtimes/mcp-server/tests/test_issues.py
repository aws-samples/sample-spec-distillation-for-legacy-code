# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Issues & Insights (no AWS): deterministic candidate detection on a small fixture, location
verification against the source pages, fail-on-empty, and the report writers."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import issues  # noqa: E402

FIXTURE = {
    "App/modGlobals.vb": """' module globals
Public Module modGlobals
    Public Const APPROVAL_LIMIT As Decimal = 5000D
End Module
""",
    "App/clsInvoice.vb": """Public Class clsInvoice
    Public Function NeedsApproval(ByVal amt As Decimal) As Boolean
        If amt > 5000 Then Return True
        Return False
    End Function

    Public Sub Save(ByVal amt As Decimal)
        If amt > 5000 AndAlso m_status = "O" Then RaiseApproval(amt)
        ' NOTE: vendors of type U are never charged a fee. Nothing enforces that, AP sets it by hand.
        Dim x As Integer = 0
        Exec("dbo.usp_InvoiceSave")
    End Sub

    '==========================================
    ' old fee calc -- replaced by FeeTable in 2011, left in for reference
    '==========================================
    Public Function OldFeeCalc(ByVal amt As Decimal) As Decimal
        Return amt * 0.02D
    End Function

    Private Sub RaiseApproval(ByVal amt As Decimal)
        '    If amt > 1000000 Then
        '        Call LegacyEscalate(amt)
        '    End If
        txtRef.MaxLength = 20
    End Sub
End Class
""",
    "Db/usp_InvoiceSave.sql": """CREATE PROCEDURE dbo.usp_InvoiceSave
    @Amt money
AS
BEGIN
    IF @Amt >= 5000 AND @Status = 'O'
        UPDATE dbo.Invoice SET NeedsAppr = 'Y'
    EXEC dbo.usp_AuditWrite @Amt
    IF @Amt < 0
        RAISERROR('negative amount', 16, 1)
END
GO
""",
    "Db/Tables.sql": """CREATE TABLE dbo.Invoice (
    Ref varchar(15) NOT NULL,
    NeedsAppr char(1) NULL,
    CONSTRAINT ckInvoice_Appr CHECK (NeedsAppr IN ('Y','N'))
)
GO
""",
}


def make_output(tmp: Path, files: dict[str, str] = FIXTURE, facts=None) -> Path:
    out = tmp / "out"
    page = out / ".pages" / "page_000"
    page.mkdir(parents=True)
    buf = []
    for path, text in files.items():
        buf.append(f"[FILE: {path} KIND: raw SIZE: {len(text)}]")
        for i, line in enumerate(text.splitlines(), 1):
            buf.append(f"{i:5d} | {line}")
    (page / "concat.txt").write_text("\n".join(buf) + "\n", encoding="utf-8")
    still = out / "stills" / "rule" / "final"
    still.mkdir(parents=True)
    (still / "catalog.md").write_text(
        "# rule\n\n### Rule: Approval Over Limit\n- Evidence: App/clsInvoice.vb#L3\n"
        "- Enforcement: client-side only; not re-verified in the database.\n", encoding="utf-8")
    if facts is not None:
        (out / "facts").mkdir()
        (out / "facts" / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    return out


def args(**kw):
    base = dict(out_dir="", target="fixture", model="test-model", region="us-east-1", profile="",
                max_calls=2, max_cost=3.0, max_candidates=120, max_tokens=4000,
                source_char_cap=200000, no_llm=False, batch_size=100)
    base.update(kw)
    return SimpleNamespace(**base)


# ── source loading + candidate detection ─────────────────────────────────────

def test_load_sources_parses_numbered_pages(tmp_path):
    src = issues.load_sources(make_output(tmp_path))
    assert set(src) == set(FIXTURE)
    assert src["App/clsInvoice.vb"][3].strip() == "If amt > 5000 Then Return True"


def test_candidate_detection_on_fixture(tmp_path):
    out = make_output(tmp_path)
    cands = issues.collect_candidates(out, issues.load_sources(out))
    by = {}
    for c in cands:
        by.setdefault(c.detector, []).append(c)
    text = lambda det: " ".join(c.summary for c in by.get(det, []))  # noqa: E731
    # 5000 compared with '>' in the client and '>=' in the proc
    assert any("strict and inclusive" in c.summary and "5000" in c.summary for c in by["literal_mismatch"])
    # Ref is varchar(15) in the table but the textbox allows 20
    assert any("'ref'" in c.summary for c in by["literal_mismatch"])
    assert "5000" in text("repeated_literal")
    # OldFeeCalc: obsolete comment + no callers
    assert "OldFeeCalc" in text("uncalled")
    assert any("obsolete" in c.summary and "OldFeeCalc" in c.summary for c in by["commented_code"])
    assert any("commented-out" in c.summary for c in by["commented_code"])
    # EXEC of a proc nobody defines; the defined proc called from the client is NOT missing
    assert "usp_auditwrite" in text("missing_object")
    assert "usp_invoicesave" not in text("missing_object")
    assert "usp_InvoiceSave" not in text("uncalled")
    assert any(c.hint == "Comment-only rule" and "Nothing enforces" in c.summary for c in by["comment_rule"])
    assert by.get("db_check")
    assert [c.id for c in cands] == [f"C{i}" for i in range(1, len(cands) + 1)]
    # every candidate location is itself verifiable
    idx = issues.SourceIndex(issues.load_sources(out))
    for c in cands:
        for p, a, b in c.locations:
            assert issues.verify_location(idx, {"file": p, "start": a, "end": b}), (c.summary, p, a, b)


def test_duplicate_expression_across_files(tmp_path):
    files = {
        "a.vb": "Sub A()\n    If r.Status = \"A\" AndAlso r.Amount > limitValue Then Hold(r)\nEnd Sub\n",
        "b.vb": "Sub B()\n    If r.Status = \"A\" AndAlso r.Amount > limitValue Then Hold(r)\nEnd Sub\n",
    }
    out = make_output(tmp_path, files)
    cands = issues.collect_candidates(out, issues.load_sources(out))
    dups = [c for c in cands if c.detector == "duplicate_expr"]
    assert dups and {p for p, _, _ in dups[0].locations} == {"a.vb", "b.vb"}


# ── location verification ────────────────────────────────────────────────────

def test_verify_location(tmp_path):
    idx = issues.SourceIndex(issues.load_sources(make_output(tmp_path)))
    ok = issues.verify_location(idx, {"file": "App/clsInvoice.vb", "start": 3, "end": 4, "quote": "amt > 5000"})
    assert ok["ref"] == "App/clsInvoice.vb:L3-L4" and ok["quote_verified"] is True
    assert ok["excerpt"][0][0] == 2  # one line of context
    # string forms and basename resolution
    assert issues.verify_location(idx, "clsInvoice.vb:L8-L9")["file"] == "App/clsInvoice.vb"
    assert issues.verify_location(idx, "Db/Tables.sql#L2")["ref"] == "Db/Tables.sql:L2-L2"
    # unknown file / out-of-range lines are rejected
    assert issues.verify_location(idx, {"file": "Nope.vb", "start": 1, "end": 2}) is None
    assert issues.verify_location(idx, {"file": "Db/Tables.sql", "start": 3, "end": 99}) is None
    assert issues.verify_location(idx, {"file": "Db/Tables.sql", "start": 0, "end": 1}) is None
    # wrong range but the quote exists elsewhere in the file → relocated to the real line
    moved = issues.verify_location(idx, {"file": "Db/usp_InvoiceSave.sql", "start": 1, "end": 1,
                                         "quote": "EXEC dbo.usp_AuditWrite"})
    assert moved["start"] == 7 and moved["quote_verified"] is True
    # quote nowhere in the file → kept but flagged
    bad = issues.verify_location(idx, {"file": "Db/Tables.sql", "start": 2, "end": 2, "quote": "no such text"})
    assert bad["quote_verified"] is False


def test_finalize_drops_unverifiable_and_downgrades(tmp_path):
    out = make_output(tmp_path)
    src = issues.load_sources(out)
    idx = issues.SourceIndex(src)
    raw = [
        {"category": "inconsistency", "title": "Approval boundary differs", "severity": "High",
         "evidence_class": "Verified", "candidate_ids": [],
         "locations": [{"file": "App/clsInvoice.vb", "start": 3, "end": 3, "quote": "amt > 5000"},
                       {"file": "Db/usp_InvoiceSave.sql", "start": 5, "end": 5, "quote": "not in source"}]},
        {"category": "Dead/obsolete code", "title": "Invented", "candidate_ids": [],
         "locations": [{"file": "Ghost.vb", "start": 1, "end": 2}]},
    ]
    got, meta = issues.finalize_issues(raw, idx, [])
    assert len(got) == 1 and meta["dropped"][0]["title"] == "Invented"
    it = got[0]
    assert it["id"] == "ISS-001" and it["category"] == "Inconsistency"
    assert it["evidence_class"] == "Inferred"  # one quote could not be found
    assert [v["ref"] for v in it["locations"]] == ["App/clsInvoice.vb:L3-L3", "Db/usp_InvoiceSave.sql:L5-L5"]


# ── stage behaviour ──────────────────────────────────────────────────────────

def fake_llm(payload, seen=None):
    def call(system, user, max_tokens):
        if seen is not None:
            seen.append(user)
        return json.dumps(payload), "end_turn", 1000, 200
    return call


def test_empty_output_fails_stage_when_candidates_exist(tmp_path):
    out = make_output(tmp_path)
    rc = issues.run(out, args(), llm=fake_llm({"issues": [], "rejected": []}), log=lambda *_: None)
    assert rc == 3
    doc = json.loads((out / "issues" / "issues.json").read_text())
    assert doc["status"].startswith("failed") and doc["candidates"]
    assert not (out / "issues" / "issues.md").exists()


def test_no_candidates_no_issues_is_ok(tmp_path):
    out = make_output(tmp_path, {"README.txt": "nothing to see\n"})
    rc = issues.run(out, args(), llm=fake_llm({"issues": [], "rejected": []}), log=lambda *_: None)
    assert rc == 0 and (out / "issues" / "issues.md").is_file()


def test_llm_failure_fails_stage(tmp_path):
    out = make_output(tmp_path)

    def boom(*_a):
        raise RuntimeError("throttled")
    assert issues.run(out, args(), llm=boom, log=lambda *_: None) == 2
    unparseable = lambda *_a: ("not json at all", "end_turn", 10, 10)  # noqa: E731
    assert issues.run(out, args(), llm=unparseable, log=lambda *_: None) == 2


def test_happy_path_writes_ranked_report_and_uses_facts(tmp_path):
    out = make_output(tmp_path, facts={"facts": [{"kind": "threshold", "value": "5000-FACT-MARKER"}]})
    seen = []
    payload = {"issues": [
        {"candidate_ids": ["C1"], "category": "Dead/obsolete code", "title": "Old fee calc still shipped",
         "description": "d", "business_impact": "b", "severity": "Low", "evidence_class": "Verified",
         "sme_question": "Can OldFeeCalc be deleted?",
         "locations": [{"file": "App/clsInvoice.vb", "start": 17, "end": 19, "quote": "OldFeeCalc"}]},
        {"candidate_ids": [], "category": "Inconsistency", "title": "Approval boundary differs",
         "description": "d", "business_impact": "b", "severity": "High", "evidence_class": "Verified",
         "sme_question": "Is exactly 5000 approved?",
         "locations": ["App/clsInvoice.vb:L3", "Db/usp_InvoiceSave.sql:L5-L5"]},
    ], "rejected": [{"candidate_id": "C2", "reason": "noise"}]}
    rc = issues.run(out, args(max_calls=1), llm=fake_llm(payload, seen), log=lambda *_: None)
    assert rc == 0
    assert "5000-FACT-MARKER" in seen[0] and "# SOURCE (whole corpus)" in seen[0]
    assert "client-side only" in seen[0]  # stills notes reach the model
    doc = json.loads((out / "issues" / "issues.json").read_text())
    assert [i["title"] for i in doc["issues"]] == ["Approval boundary differs", "Old fee calc still shipped"]
    assert doc["meta"]["facts_json_present"] is True and doc["meta"]["usage"]["calls"] == 1
    md = (out / "issues" / "issues.md").read_text()
    assert md.index("## Summary") < md.index("## Inconsistency") < md.index("## Dead/obsolete code")
    assert "`App/clsInvoice.vb:L3-L3`" in md and "Is exactly 5000 approved?" in md


def test_budget_guard_and_batching(tmp_path):
    out = make_output(tmp_path)
    seen = []
    payload = {"issues": [{"candidate_ids": ["C1"], "category": "Inconsistency", "title": "t",
                           "locations": ["App/clsInvoice.vb:L3"]}]}
    rc = issues.run(out, args(max_calls=1, max_cost=1000), llm=fake_llm(payload, seen), log=lambda *_: None)
    assert rc == 0 and len(seen) == 1  # everything fits one call
    seen.clear()
    rc = issues.run(out, args(max_calls=3, max_cost=1000, batch_size=3), llm=fake_llm(payload, seen),
                    log=lambda *_: None)
    assert rc == 0 and len(seen) == 3  # bounded by max_calls even though more batches were needed
    assert "add the issues you find" in seen[0] and "Do not add new issues" in seen[-1]
    assert "ALREADY REPORTED" in seen[1] and "ALREADY REPORTED" not in seen[0]
    seen.clear()
    rc = issues.run(out, args(max_calls=3, max_cost=0.0001), llm=fake_llm(payload, seen), log=lambda *_: None)
    assert seen == [] and rc == 3  # nothing affordable → no issues → stage fails loudly


def test_no_llm_mode(tmp_path):
    out = make_output(tmp_path)
    assert issues.run(out, args(no_llm=True), log=lambda *_: None) == 0
    doc = json.loads((out / "issues" / "issues.json").read_text())
    assert doc["issues"] and all(i["evidence_class"] == "Confirmation Required" for i in doc["issues"])


def test_non_issues_dropped_and_cross_batch_repeats_merged(tmp_path):
    idx = issues.SourceIndex(issues.load_sources(make_output(tmp_path)))
    loc = [{"file": "App/clsInvoice.vb", "start": 8, "end": 9}]
    raw = [
        {"category": "Comment-only rule", "title": "Vendor type U fee exemption enforced only by hand",
         "severity": "High", "evidence_class": "Verified", "locations": loc},
        {"category": "Undocumented behaviour", "title": "Fee exemption for type U vendors is unenforced",
         "severity": "Medium", "evidence_class": "Verified",
         "locations": [{"file": "App/clsInvoice.vb", "start": 9, "end": 10}]},
        {"category": "Comment-only rule", "title": "Header comments are informational only", "locations": loc},
    ]
    got, meta = issues.finalize_issues(raw, idx, [])
    assert len(got) == 1 and got[0]["severity"] == "High"
    assert got[0]["locations"][-1]["ref"] == "App/clsInvoice.vb:L9-L10"
    assert meta["dropped"][0]["reason"] == "model described a non-issue"


def test_parse_truncated_json_keeps_complete_issues():
    text = '{"issues": [{"title": "a", "locations": []}, {"title": "b", "locations": []}, {"title": "c", "loc'
    got = issues.parse_llm_json(text)
    assert [i["title"] for i in got["issues"]] == ["a", "b"]


def test_prompt_is_generic():
    prompt = issues.PROMPT_PATH.read_text()
    for word in ("invoice", "vendor", "retainage", "AP "):
        assert word.lower() not in prompt.lower(), word
