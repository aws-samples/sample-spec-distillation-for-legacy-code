# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The VB.NET (and C#) code-graph rig: calls, stored-procedure executions and table access.

No AWS and no network: Bedrock is replaced with a fake that returns a canned JSON payload, so what
is under test is the PROMPT CONTRACT and the deterministic disposal around it, not the model.

The defect this covers: the VB extraction prompt used to ask only for `Imports` namespaces and
remote gateway namespaces, so the code graph had a whole-language blind spot — 0% of the
deterministic parser's VB edges (table reads, table writes, proc executions) appeared in it, while
SQL recall was 97.6%. The prompt now asks for the same relations the SQL rig emits, the regex
confirms them against the source, and `<run>/facts/facts.json` corroborates or backfills them.

Run:  python3 -m pytest -q runtimes/mcp-server/tests/test_code_graph_vb.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP))

import bedrock_client  # noqa: E402
import code_graph  # noqa: E402

# ----------------------------------------------------------------------------- fixture source
VB_SOURCE = """\
Imports System.Data
Imports System.Data.SqlClient

Public Class clsAPInvoiceDAL

    Public Function GetHeader(ByVal APCo As Byte, ByVal APTrans As Integer) As DataRow
        Dim sql As String = "SELECT APCo, APTrans, Vendor, GrossAmt, Status FROM dbo.APInvHdr " & _
                            "WHERE APCo = @APCo AND APTrans = @APTrans"
        Dim cmd As New SqlCommand(sql, m_cn)
        cmd.Parameters.AddWithValue("@APCo", APCo)
        Return Fill(cmd).Rows(0)
    End Function

    Public Function PostInvoice(ByVal APCo As Byte, ByVal APTrans As Integer) As Integer
        Dim cmd As New SqlCommand("usp_APInvPost", m_cn)
        cmd.CommandType = CommandType.StoredProcedure
        Return cmd.ExecuteNonQuery()
    End Function

    Public Sub SetStatus(ByVal APCo As Byte, ByVal APTrans As Integer, ByVal Status As String)
        Dim sql As String = "UPDATE dbo.APInvHdr SET Status = @Status WHERE APTrans = @APTrans"
        ExecuteNonQuery(sql)
    End Sub

End Class
"""

# What the model is asked to return, in the shape the prompt specifies.
LLM_REPLY = {
    "file_role": "repository",
    "module": "AP",
    "business_intent": "Reads and updates AP invoice headers and posts invoices.",
    "usings": ["System.Data", "System.Data.SqlClient"],
    "declarations": [{"name": "clsAPInvoiceDAL", "kind": "class", "base": None, "implements": []}],
    "calls": [
        {"caller": "GetHeader", "callee": "Fill", "receiver": None, "confidence": 0.8},
        {"caller": "SetStatus", "callee": "ExecuteNonQuery", "receiver": None, "confidence": 0.8},
    ],
    "data_refs": [
        {"name": "dbo.APInvHdr", "kind": "table", "operation": "select", "direction": "read",
         "via": "inline_sql", "resolution": "static", "confidence": 0.95},
        {"name": "APInvHdr", "kind": "table", "operation": "update", "direction": "write",
         "via": "inline_sql", "resolution": "static", "confidence": 0.95},
        {"name": "usp_APInvPost", "kind": "stored_procedure", "operation": "exec",
         "direction": "call", "via": "sqlcommand", "resolution": "static", "confidence": 0.98},
        {"name": "APInvHdrNotHere", "kind": "table", "operation": "select", "direction": "read",
         "via": "dal", "resolution": "static", "confidence": 0.4},
    ],
    "references": [
        {"symbol": "SqlCommand", "kind": "instantiate", "resolution": "remote",
         "gateway": "System.Data.SqlClient", "confidence": 0.9},
    ],
    "business_entities": [
        {"code_name": "APInvHdr", "business_name": "AP Invoice Header", "confidence": 0.9},
        {"code_name": "LienWvrNope", "business_name": "Lien Waiver", "confidence": 0.4},
    ],
}


def _edges(node, etype=None, kind=None):
    out = node["edges"]
    if etype:
        out = [e for e in out if e["type"] == etype]
    if kind:
        out = [e for e in out if (e.get("kind") or "") == kind]
    return out


def _rel(node):
    """{(dst bare, kind, operation, direction)} — the comparable projection."""
    return {(code_graph._normalize_dst(e["dst"]), e.get("kind"), e.get("operation"),
             e.get("direction")) for e in node["edges"]}


# ----------------------------------------------------------------------------- the prompt
def test_vb_prompt_asks_for_calls_procs_and_tables():
    """A prompt that never mentions callees, procedures or tables cannot produce them. This is the
    regression guard for the blind spot itself, independent of any model behaviour."""
    p = code_graph.VB_USER
    for required in ('"calls"', '"data_refs"', '"callee"', "stored_procedure", "CommandText",
                     "SqlCommand", "INSERT INTO", "DELETE FROM", "dynamic_sql", "trigger"):
        assert required in p, required
    assert "verbatim in the source" in p
    # the C# rig asks for the same relations, so the two CLR languages cannot drift apart
    for required in ('"calls"', '"data_refs"', "stored_procedure"):
        assert required in code_graph.CS_USER, required
    assert code_graph._LANG_PROMPTS["vbnet"][1] is code_graph.VB_USER


# ----------------------------------------------------------------------------- assembly
def test_vb_assemble_emits_call_proc_and_table_edges():
    node = code_graph._assemble("ClientSide/AP/clsAPInvoiceDAL.vb", "vbnet", LLM_REPLY,
                                VB_SOURCE, "page_000", "test-model")
    rel = _rel(node)
    # table read, table write and proc exec — none of which the old prompt could produce
    assert ("apinvhdr", "table", "select", "read") in rel
    assert ("apinvhdr", "table", "update", "write") in rel
    assert ("usp_apinvpost", "stored_procedure", "exec", "call") in rel
    # method -> method calls, as their own relation (not folded into proc_exec)
    calls = _edges(node, etype="CALLS", kind="method")
    assert {code_graph._normalize_dst(e["dst"]) for e in calls} == {"fill", "executenonquery"}
    # imports are still emitted, unchanged
    assert {e["dst"] for e in _edges(node, etype="IMPORTS")} == {"system.data",
                                                                 "system.data.sqlclient"}
    assert node["role"] == "repository"


def test_absent_name_is_kept_and_flagged_not_dropped():
    """Regex silence is not refutation: an unconfirmed edge stays in the graph, downweighted."""
    node = code_graph._assemble("x.vb", "vbnet", LLM_REPLY, VB_SOURCE, "p", "m")
    ghost = [e for e in node["edges"] if code_graph._normalize_dst(e["dst"]) == "apinvhdrnothere"]
    assert len(ghost) == 1
    assert ghost[0]["verdict"] == "unconfirmed"
    assert ghost[0]["validated"] == 0
    assert ghost[0]["derived_confidence"] <= 0.5
    confirmed = [e for e in node["edges"]
                 if code_graph._normalize_dst(e["dst"]) == "usp_apinvpost"][0]
    assert confirmed["verdict"] == "confirmed" and confirmed["validated"] == 1
    # the same rule for entities
    ents = {e["code_name"]: e for e in node["business_entities"]}
    assert ents["APInvHdr"]["anchor_verdict"] == "confirmed"
    assert ents["LienWvrNope"]["anchor_verdict"] == "unconfirmed"


def test_dynamic_sql_is_exempt_from_confirmation():
    llm = json.loads(json.dumps(LLM_REPLY))
    llm["data_refs"] = [{"name": "WhateverRuntimeBuilt", "kind": "table", "operation": "select",
                         "direction": "read", "via": "inline_sql", "resolution": "dynamic_sql",
                         "confidence": 0.5}]
    node = code_graph._assemble("x.vb", "vbnet", llm, VB_SOURCE, "p", "m")
    e = [x for x in node["edges"] if x["type"] == "REFERENCES" and x.get("kind") == "table"][0]
    assert e["verdict"] == "dynamic" and e["dynamic"] == 1
    assert e["derived_confidence"] <= 0.6


def test_framework_members_are_not_reported_as_system_calls():
    llm = json.loads(json.dumps(LLM_REPLY))
    llm["calls"] = [{"caller": "GetHeader", "callee": "ToString", "receiver": "x"},
                    {"caller": "GetHeader", "callee": "AddWithValue", "receiver": "cmd.Parameters"},
                    {"caller": "GetHeader", "callee": "Fill", "receiver": None}]
    node = code_graph._assemble("x.vb", "vbnet", llm, VB_SOURCE, "p", "m")
    assert {code_graph._normalize_dst(e["dst"])
            for e in _edges(node, etype="CALLS")} == {"fill"}


# ----------------------------------------------------------------------------- parser facts
PARSER_FACTS = {
    "files": [{"file": "ClientSide/AP/clsAPInvoiceDAL.vb", "language": "vb.net", "start": 1, "end": 26}],
    "vb": {
        "calls": [{"from": "clsAPInvoiceDAL.GetHeader", "to": "clsAPInvoiceDAL.Fill",
                   "file": "ClientSide/AP/clsAPInvoiceDAL.vb", "line": 11}],
        "proc_refs": [{"from": "clsAPInvoiceDAL.PostInvoice", "proc": "usp_APInvPost",
                       "file": "ClientSide/AP/clsAPInvoiceDAL.vb", "line": 16,
                       "defined_in_corpus": True}],
        "inline_sql": [
            {"from": "clsAPInvoiceDAL.GetHeader", "file": "ClientSide/AP/clsAPInvoiceDAL.vb",
             "reads": [{"table": "APInvHdr", "lines": [7]}], "writes": []},
            {"from": "clsAPInvoiceDAL.SetStatus", "file": "ClientSide/AP/clsAPInvoiceDAL.vb",
             "reads": [], "writes": [{"table": "APInvHdr", "op": "UPDATE", "lines": [22]}]},
            {"from": "clsAPInvoiceDAL.GetCompliance", "file": "ClientSide/AP/clsAPInvoiceDAL.vb",
             "reads": [{"table": "APVendCompliance", "lines": [30]}], "writes": []},
            # the parser sees APHold ONLY written — a model that calls it a read is overturned
            {"from": "clsAPInvoiceDAL.AddHold", "file": "ClientSide/AP/clsAPInvoiceDAL.vb",
             "reads": [], "writes": [{"table": "APHold", "op": "INSERT", "lines": [35]}]},
        ],
    },
    "sql": {"procs": [], "triggers": [], "scripts": [], "tables": []},
}


def test_facts_backfill_adds_what_the_model_missed_and_tags_its_provenance():
    facts = code_graph.FactsIndex(PARSER_FACTS)
    llm = json.loads(json.dumps(LLM_REPLY))
    llm["data_refs"] = [r for r in llm["data_refs"] if r["kind"] != "table"]   # model misses tables
    node = code_graph._assemble("ClientSide/AP/clsAPInvoiceDAL.vb", "vbnet", llm, VB_SOURCE,
                                "p", "m", facts)
    rel = _rel(node)
    assert ("apinvhdr", "table", "select", "read") in rel      # backfilled
    assert ("apinvhdr", "table", "update", "write") in rel     # backfilled
    assert ("apvendcompliance", "table", "select", "read") in rel
    backfilled = [e for e in node["edges"]
                  if str(e.get("symbol") or "").startswith(code_graph.FACTS_PROVENANCE)]
    assert backfilled, "a parser-derived edge must be tagged so it stays separable for measurement"
    assert all(e["verdict"] == "confirmed" for e in backfilled)
    assert {e["symbol"] for e in backfilled} <= {"facts:vb_inline_sql", "facts:vb_call",
                                                 "facts:vb_proc_ref"}


def test_facts_corroboration_upgrades_a_model_edge_and_does_not_duplicate_it():
    facts = code_graph.FactsIndex(PARSER_FACTS)
    node = code_graph._assemble("ClientSide/AP/clsAPInvoiceDAL.vb", "vbnet", LLM_REPLY, VB_SOURCE,
                               "p", "m", facts)
    proc = [e for e in node["edges"]
            if code_graph._normalize_dst(e["dst"]) == "usp_apinvpost"]
    assert len(proc) == 1, "the model edge and the parser edge must merge, not duplicate"
    assert proc[0]["direction_verdict"] == "parser_confirmed"
    assert not str(proc[0]["symbol"] or "").startswith(code_graph.FACTS_PROVENANCE)
    # both relations survive when a file reads AND writes the same table
    hdr = {(e["operation"], e["direction"]) for e in node["edges"]
           if code_graph._normalize_dst(e["dst"]) == "apinvhdr"}
    assert ("select", "read") in hdr and ("update", "write") in hdr

    # the parser's direction OVERTURNS the model's when the parser only ever saw the other one
    llm = json.loads(json.dumps(LLM_REPLY))
    llm["data_refs"].append({"name": "APHold", "kind": "table", "operation": "select",
                             "direction": "read", "via": "dal", "resolution": "static",
                             "confidence": 0.5})
    node2 = code_graph._assemble("ClientSide/AP/clsAPInvoiceDAL.vb", "vbnet", llm, VB_SOURCE,
                                 "p", "m", facts)
    hold = [e for e in node2["edges"] if code_graph._normalize_dst(e["dst"]) == "aphold"]
    assert len(hold) == 1
    assert (hold[0]["operation"], hold[0]["direction"]) == ("insert", "write")
    assert hold[0]["direction_verdict"] == "parser_confirmed"


def test_trigger_reference_is_relabelled_not_counted_as_a_table_write():
    """A reference from a trigger's own file to its table, with no parser write, is the trigger
    attachment relation. Mislabelling it as a write was the only false positive on the reference
    run; the parser disposes of it deterministically."""
    facts = code_graph.FactsIndex({
        "sql": {"triggers": [{"name": "trg_APInvHdr_u", "table": "APInvHdr",
                              "file": "Scripts/Triggers/trg_APInvHdr_u.sql",
                              "reads": [], "writes": []}],
                "procs": [], "scripts": [], "tables": []},
        "vb": {}, "files": []})
    src = "CREATE TRIGGER trg_APInvHdr_u ON dbo.APInvHdr AFTER UPDATE AS SELECT 1\n"
    llm = {"file_role": "trigger", "module": None, "business_intent": "guard",
           "defines": [{"name": "trg_APInvHdr_u", "kind": "trigger"}],
           "references": [{"name": "APInvHdr", "kind": "table", "operation": "update",
                           "direction": "write", "resolution": "static", "confidence": 0.9}],
           "business_entities": []}
    node = code_graph._assemble("Scripts/Triggers/trg_APInvHdr_u.sql", "sql", llm, src, "p", "m",
                                facts)
    e = [x for x in node["edges"] if code_graph._normalize_dst(x["dst"]) == "apinvhdr"]
    assert len(e) == 1
    assert e[0]["kind"] == "trigger" and e[0]["operation"] == "trigger_on_table"
    # the edge type matters: arbitrate.resolve_kind_by_create() rewrites the `kind` of a SQL
    # REFERENCES edge to its target's CREATE kind ("table"), which would silently undo the relabel.
    assert e[0]["type"] == "TRIGGERS"


def test_arbitration_does_not_overturn_a_trigger_attachment(tmp_path):
    """Belt to the TRIGGERS braces: even a trigger edge that stayed REFERENCES must survive."""
    import arbitrate
    db = tmp_path / "g.db"
    conn = sqlite3.connect(str(db))
    code_graph.ensure_tables(conn)
    conn.execute("INSERT INTO code_objects (path,object,kind) VALUES "
                 "('Scripts/Tables/AP_Tables.sql','dbo.apinvhdr','table')")
    for etype, op in (("TRIGGERS", "trigger_on_table"), ("REFERENCES", "trigger_on_table"),
                      ("REFERENCES", "update")):
        conn.execute("INSERT INTO code_edges (src,dst,type,lang,kind,operation,direction,verdict) "
                     "VALUES ('t.sql','dbo.apinvhdr',?,'sql','trigger',?,'write','confirmed')",
                     (etype, op))
    conn.commit()
    conn.close()
    arbitrate.arbitrate(str(db))
    conn = sqlite3.connect(str(db))
    got = conn.execute("SELECT type, operation, kind FROM code_edges ORDER BY type, operation").fetchall()
    conn.close()
    assert ("REFERENCES", "trigger_on_table", "trigger") in got
    assert ("TRIGGERS", "trigger_on_table", "trigger") in got
    # an ordinary SQL reference IS still corrected to the CREATE ground-truth kind
    assert ("REFERENCES", "update", "table") in got


def test_a_dal_method_is_not_a_stored_procedure():
    """Asked for "stored procedures", a model readily labels a DAL method as one. The parser knows
    which names are methods of this system, so it settles it — and the relation it SHOULD have been
    (a method call) is emitted instead of being lost."""
    facts = code_graph.FactsIndex({
        "vb": {"methods": [{"name": "AddHold", "id": "clsAPInvoiceDAL.AddHold"},
                           {"name": "GetOpenHolds", "id": "clsAPInvoiceDAL.GetOpenHolds"}]},
        "sql": {"procs": [{"name": "usp_APInvPost"}], "triggers": [], "scripts": [], "tables": []},
        "files": []})
    src = "Call m_DAL.AddHold(APCo, APTrans)\nDim d = m_DAL.GetOpenHolds(APCo)\n"
    llm = {"file_role": "form", "module": None, "business_intent": "x", "declarations": [],
           "business_entities": [],
           "data_refs": [
               {"name": "AddHold", "kind": "stored_procedure", "operation": "exec",
                "direction": "call", "via": "proc_literal", "resolution": "static"},
               {"name": "GetOpenHolds", "kind": "stored_procedure", "operation": "exec",
                "direction": "call", "via": "proc_literal", "resolution": "static"}]}
    node = code_graph._assemble("ClientSide/frmAPPaymentHold.vb", "vbnet", llm, src, "p", "m", facts)
    assert not [e for e in node["edges"] if e.get("kind") == "stored_procedure"]
    calls = _edges(node, etype="CALLS", kind="method")
    assert {code_graph._normalize_dst(e["dst"]) for e in calls} == {"addhold", "getopenholds"}
    assert all(e["symbol"] == "arbitrated:method_not_proc" for e in calls)
    # a real proc is untouched
    llm["data_refs"] = [{"name": "usp_APInvPost", "kind": "stored_procedure", "operation": "exec",
                         "direction": "call", "via": "sqlcommand", "resolution": "static"}]
    node2 = code_graph._assemble("x.vb", "vbnet", llm, 'New SqlCommand("usp_APInvPost", cn)\n',
                                 "p", "m", facts)
    assert [e["kind"] for e in node2["edges"] if e["type"] == "REFERENCES"] == ["stored_procedure"]


def test_a_name_only_in_a_comment_is_a_mention_not_a_dependency():
    """`' --- invoice status codes (APInvHdr.Status) ---` describes a table; it does not access it.
    Comment-only names were the largest false-positive class once the prompt started looking for
    tables, so the guard is deterministic: strip the comments and re-test."""
    src = ("Module modAPGlobals\n"
           "    '--- invoice status codes (APInvHdr.Status) ------------------\n"
           "    ' approval limits by role. Should match APApprLim table.\n"
           "    Public Const APPROVAL_THRESHOLD As Decimal = 5000D\n"
           '    Public g_sql As String = "SELECT 1 FROM dbo.APCoParm"\n'
           "End Module\n")
    llm = {"file_role": "utility", "module": "AP", "business_intent": "x", "declarations": [],
           "business_entities": [], "data_refs": [
               {"name": "APInvHdr", "kind": "table", "operation": "select", "direction": "read",
                "via": "dal", "resolution": "static", "confidence": 0.5},
               {"name": "APApprLim", "kind": "table", "operation": "select", "direction": "read",
                "via": "dal", "resolution": "static", "confidence": 0.5},
               {"name": "APCoParm", "kind": "table", "operation": "select", "direction": "read",
                "via": "inline_sql", "resolution": "static", "confidence": 0.9}]}
    node = code_graph._assemble("ClientSide/modAPGlobals.vb", "vbnet", llm, src, "p", "m")
    by = {code_graph._normalize_dst(e["dst"]): e for e in node["edges"]}
    for name in ("apinvhdr", "apapprlim"):
        assert by[name]["kind"] == "comment_mention"
        assert by[name]["type"] == "MENTIONS"
        assert by[name]["verdict"] == "refuted"
        assert by[name]["resolution"] == "comment_only"
        assert by[name]["validated"] == 0
    # the one that really is in code stays a table read
    assert by["apcoparm"]["kind"] == "table" and by["apcoparm"]["verdict"] == "confirmed"


def test_strip_comments_keeps_code_and_respects_strings():
    vb = ("Dim s As String = \"it's fine -- really\"   ' a trailing comment about APInvHdr\n"
          "REM a whole-line remark about APVend\n"
          "Dim t = GetHeader()\n")
    code = code_graph.strip_comments(vb, "vbnet")
    assert "apinvhdr" not in code.lower()
    assert "apvend" not in code.lower()
    assert "it's fine -- really" in code       # an apostrophe inside a string is not a comment
    assert "GetHeader()" in code
    # `rem` is a comment only as a WHOLE-LINE keyword: stripping it mid-line would strip real code
    kept = code_graph.strip_comments("Dim rem As Integer = 5\nCall Trem (APVend)\n", "vbnet")
    assert "rem As Integer" in kept and "APVend" in kept
    sql = "SELECT 1 FROM dbo.T -- mentions APVend\n/* block about APHold */ SELECT 2 FROM U\n"
    scode = code_graph.strip_comments(sql, "sql")
    assert "apvend" not in scode.lower() and "aphold" not in scode.lower()
    assert "dbo.T" in scode and "FROM U" in scode
    cs = "var x = 1; // about APVend\n/* APHold */ Go();\n"
    ccode = code_graph.strip_comments(cs, "csharp")
    assert "apvend" not in ccode.lower() and "aphold" not in ccode.lower() and "Go();" in ccode


def test_facts_index_absent_is_a_graceful_degrade(tmp_path):
    facts, how = code_graph.load_facts(tmp_path / ".pages")
    assert facts is None and "absent" in how
    assert code_graph.FactsIndex(None).present is False
    node = code_graph._assemble("x.vb", "vbnet", LLM_REPLY, VB_SOURCE, "p", "m",
                                code_graph.FactsIndex(None))
    assert _rel(node)                      # the model's own proposals still land


def test_load_facts_reads_the_run_layout(tmp_path):
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "facts.json").write_text(json.dumps(PARSER_FACTS))
    (tmp_path / ".pages").mkdir()
    facts, how = code_graph.load_facts(tmp_path / ".pages")
    assert facts is not None and how.endswith("facts.json")
    assert code_graph.load_facts(tmp_path / ".pages", "")[0] is None     # explicitly disabled


# ----------------------------------------------------------------------------- end to end
class FakeLLM:
    def __init__(self, reply: dict):
        self.reply = reply
        self.prompts: list[str] = []


def test_build_code_graph_persists_vb_relations(tmp_path, monkeypatch):
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    body = "\n".join(f"{i + 1:5d} | {ln}" for i, ln in enumerate(VB_SOURCE.splitlines()))
    (pages / "concat.txt").write_text(
        "[FILE: ClientSide/AP/clsAPInvoiceDAL.vb KIND: raw]\n" + body + "\n"
        "[END FILE: ClientSide/AP/clsAPInvoiceDAL.vb]\n")
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "facts.json").write_text(json.dumps(PARSER_FACTS))

    fake = FakeLLM(LLM_REPLY)

    def fake_call(llm, model_id, system, user, max_tokens=0, temperature=0.0):
        fake.prompts.append(user)
        return bedrock_client.ConverseResult(output_text=json.dumps(fake.reply),
                                             input_tokens=10, output_tokens=20,
                                             stop_reason="end_turn")

    monkeypatch.setattr(code_graph.bedrock_client, "create_llm_client",
                        lambda profile, region: fake)
    monkeypatch.setattr(code_graph.bedrock_client, "call_llm", fake_call)

    db = tmp_path / "shared_vectors.db"
    stats = code_graph.build_code_graph(str(tmp_path / ".pages"), str(db), min_chars=100)
    assert stats["files"] == 1 and stats["ok"] == 1 and stats["errors"] == 0
    assert stats["parser_facts"]["present"] is True
    assert stats["edge_provenance"]["model_proposed"] > 0
    assert stats["edge_provenance"]["parser_backfill"] > 0
    # the prompt that actually went out asks for the new relations
    assert '"data_refs"' in fake.prompts[0] and '"calls"' in fake.prompts[0]

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT dst, type, kind, operation, direction, verdict, symbol "
                        "FROM code_edges").fetchall()
    conn.close()
    got = {(code_graph._normalize_dst(r[0]), r[2], r[3], r[4]) for r in rows}
    assert ("apinvhdr", "table", "select", "read") in got
    assert ("apinvhdr", "table", "update", "write") in got
    assert ("usp_apinvpost", "stored_procedure", "exec", "call") in got
    assert any(k == "method" for _, k, _, _ in got)


def test_disabling_facts_leaves_only_model_proposals(tmp_path, monkeypatch):
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    (pages / "concat.txt").write_text(
        "[FILE: a.vb KIND: raw]\n" + VB_SOURCE + "\n[END FILE: a.vb]\n")
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "facts.json").write_text(json.dumps(PARSER_FACTS))

    def fake_call(llm, model_id, system, user, max_tokens=0, temperature=0.0):
        return bedrock_client.ConverseResult(output_text=json.dumps(LLM_REPLY), input_tokens=1,
                                             output_tokens=1, stop_reason="end_turn")

    monkeypatch.setattr(code_graph.bedrock_client, "create_llm_client",
                        lambda profile, region: object())
    monkeypatch.setattr(code_graph.bedrock_client, "call_llm", fake_call)
    stats = code_graph.build_code_graph(str(tmp_path / ".pages"), str(tmp_path / "db.sqlite"),
                                        min_chars=100, facts_path="")
    assert stats["parser_facts"]["present"] is False
    assert stats["edge_provenance"].get("parser_backfill", 0) == 0
