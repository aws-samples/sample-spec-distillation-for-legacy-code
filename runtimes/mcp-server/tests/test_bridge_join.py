# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The concept<->code bridge join, and the one canonical concept key both ends use.

Two defects are covered.

1. `export_graph._norm()` keyed the join on the LAST dotted segment, so a concept that wrote
   `h.Vendor`, `inserted.APRef` or `APInvHdr.Job` was joined on `vendor` / `apref` / `job` and
   matched ANY file containing those letters — including a Designer file where `vendor` exists only
   inside `cboVendor` / `txtVendor`. That is a plausible but WRONG file and the link asserted
   something false. The join now carries the qualifier, resolves it, and refuses to emit a link
   whose assertion it cannot verify.

2. The bridge and the concept graph never shared a key: `concept_symbols.py` keys on the FORMATTED
   still title (`data::[DT-0006] Invoice Number ...`), the graph keeps the RAW map-stage header
   (`Data Invariant: Invoice Number ...`). One canonical key (concept_key.py) is now written by
   both, so links resolve by equality instead of through a ladder of normalisations.

No AWS, no network.  Run:  python3 -m pytest -q runtimes/mcp-server/tests/test_bridge_join.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP))

import concept_key as ckey  # noqa: E402
import concept_symbols  # noqa: E402
import export_graph as eg  # noqa: E402

# ----------------------------------------------------------------------------- fixture corpus
BL_VB = """\
Public Class clsAPInvoiceBL
    Public Function ValidateHeader(ByVal VendGrp As Byte, ByVal Vendor As Integer) As ArrayList
        ' Reads dbo.APInvHdr for the duplicate check
        Dim hdr As DataRow = m_DAL.GetHeader(g_APCo, Vendor)
        If hdr("Job") Is Nothing Then errs.Add("Job is required")
        Return errs
    End Function
End Class
"""
# The trap: `vendor` occurs here ONLY inside longer control names.
DESIGNER_VB = """\
Partial Class frmAPInvoiceEntry
    Friend WithEvents cboVendor As System.Windows.Forms.ComboBox
    Friend WithEvents txtVendorName As System.Windows.Forms.TextBox
End Class
"""
TRIGGER_SQL = """\
CREATE TRIGGER trg_APInvHdr_u ON dbo.APInvHdr AFTER UPDATE AS
BEGIN
    IF EXISTS (SELECT 1 FROM inserted i JOIN deleted d ON i.APTrans = d.APTrans
               WHERE i.APRef <> d.APRef AND i.Status = 'P')
        RAISERROR('APRef is frozen once posted', 16, 1)
END
"""
TABLES_SQL = """\
CREATE TABLE dbo.APInvHdr (APCo tinyint, APTrans int, Vendor int, APRef varchar(15),
                           Job varchar(10), Status char(1))
"""

CODE = {
    "source_files": [
        {"path": "ClientSide/AP/clsAPInvoiceBL.vb", "lang": "vbnet"},
        {"path": "ClientSide/AP/frmAPInvoiceEntry.Designer.vb", "lang": "vbnet"},
        {"path": "Scripts/Triggers/trg_APInvHdr_u.sql", "lang": "sql"},
        {"path": "Scripts/Tables/AP_Tables.sql", "lang": "sql"},
    ],
    "code_objects": [
        {"path": "Scripts/Tables/AP_Tables.sql", "object": "dbo.apinvhdr", "kind": "table"},
        {"path": "ClientSide/AP/clsAPInvoiceBL.vb", "object": "clsAPInvoiceBL", "kind": "class"},
    ],
    "code_edges": [
        {"src": "ClientSide/AP/clsAPInvoiceBL.vb", "dst": "apinvhdr", "type": "REFERENCES",
         "lang": "vbnet", "kind": "table", "operation": "select", "direction": "read",
         "dynamic": 0, "verdict": "confirmed", "symbol": "inline_sql"},
    ],
    "code_entities": [
        # the same decoded entity in three files: one legitimate, one qualified-but-resolvable,
        # one where the name is only a substring of an unrelated control
        {"path": "ClientSide/AP/clsAPInvoiceBL.vb", "code_name": "Vendor",
         "business_name": "Vendor", "anchor_verdict": "confirmed"},
        {"path": "ClientSide/AP/frmAPInvoiceEntry.Designer.vb", "code_name": "Vendor",
         "business_name": "Vendor", "anchor_verdict": "confirmed"},
        {"path": "Scripts/Triggers/trg_APInvHdr_u.sql", "code_name": "APRef",
         "business_name": "AP Reference", "anchor_verdict": "confirmed"},
        {"path": "ClientSide/AP/clsAPInvoiceBL.vb", "code_name": "Job",
         "business_name": "Job", "anchor_verdict": "confirmed"},
    ],
}

FACTS = {
    "files": [],
    "sql": {
        "tables": [{"name": "APInvHdr", "file": "Scripts/Tables/AP_Tables.sql",
                    "columns": [{"name": "APCo"}, {"name": "APTrans"}, {"name": "Vendor"},
                                {"name": "APRef"}, {"name": "Job"}, {"name": "Status"}]}],
        "triggers": [{"name": "trg_APInvHdr_u", "table": "APInvHdr",
                      "file": "Scripts/Triggers/trg_APInvHdr_u.sql",
                      "reads": [{"table": "APInvHdr", "lines": [1]}], "writes": [],
                      "columns": [{"table": "APInvHdr", "column": "APRef", "mode": "read",
                                   "lines": [4]}]}],
        "procs": [], "scripts": [],
    },
    "vb": {
        "types": [{"name": "clsAPInvoiceBL", "file": "ClientSide/AP/clsAPInvoiceBL.vb"}],
        "members": [], "methods": [],
        "column_refs": [{"file": "ClientSide/AP/clsAPInvoiceBL.vb", "name": "Job", "how": "read"}],
        "inline_sql": [],
    },
}


@pytest.fixture()
def corpus(tmp_path) -> Path:
    root = tmp_path / "src"
    for rel, text in (("ClientSide/AP/clsAPInvoiceBL.vb", BL_VB),
                      ("ClientSide/AP/frmAPInvoiceEntry.Designer.vb", DESIGNER_VB),
                      ("Scripts/Triggers/trg_APInvHdr_u.sql", TRIGGER_SQL),
                      ("Scripts/Tables/AP_Tables.sql", TABLES_SQL)):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _symbols(by_symbol: dict) -> dict:
    return {"by_concept": {c: {"title": c.split("::")[-1], "still": c.split("::")[0],
                               "canonical_key": ckey.canonicalize_key(c)}
                           for cs in by_symbol.values() for c in cs},
            "by_symbol": by_symbol}


def _join(corpus: Path, by_symbol: dict, facts=FACTS, code=CODE):
    src = eg.SourceText(corpus)
    return eg.build_bridge(_symbols(by_symbol), code, src, facts)


def _index(links):
    return {(lk["symbol"], lk["code_path"]): lk for lk in links}


# ----------------------------------------------------------------------------- the join
def test_substring_only_match_is_refused_not_guessed(corpus):
    """`vendor` in the Designer file exists only inside `cboVendor`/`txtVendorName`. The old join
    cited that file; the new one drops the link and says why."""
    links, dropped, stats = _join(corpus, {"h.Vendor": ["data::Entity: AP Invoice Header"]})
    paths = {lk["code_path"] for lk in links}
    assert "ClientSide/AP/frmAPInvoiceEntry.Designer.vb" not in paths
    bad = [d for d in dropped
           if d["code_path"] == "ClientSide/AP/frmAPInvoiceEntry.Designer.vb"]
    assert len(bad) == 1
    assert bad[0]["reason"] == "substring_only"
    assert "only inside a longer identifier" in bad[0]["detail"]
    assert stats["dropped_substring_only"] == 1


def test_sql_alias_resolves_to_its_table_and_keeps_the_link(corpus):
    """`h.Vendor` in clsAPInvoiceBL.vb: the bare name IS a real identifier there and the qualifier
    resolves to the table that declares the column, which the file also names."""
    links, dropped, _ = _join(corpus, {"h.Vendor": ["data::Entity: AP Invoice Header"]})
    lk = _index(links)[("h.Vendor", "ClientSide/AP/clsAPInvoiceBL.vb")]
    assert lk["qualifier"] == "h"
    assert lk["join_name"] == "Vendor"
    assert lk["qualifier_resolved_to"] == "APInvHdr"
    assert lk["resolution"].startswith("qualifier_")
    assert lk["qualifier_corroboration"] == "named"
    assert "flags" not in lk


def test_trigger_pseudo_table_resolves_to_the_triggers_own_table(corpus):
    links, dropped, _ = _join(corpus, {"inserted.APRef": ["data::Rule: APRef is frozen"],
                                       "deleted.APRef": ["data::Rule: APRef is frozen"]})
    for sym in ("inserted.APRef", "deleted.APRef"):
        lk = _index(links)[(sym, "Scripts/Triggers/trg_APInvHdr_u.sql")]
        assert lk["qualifier_resolved_to"] == "APInvHdr"
        assert lk["resolution"] == "qualifier_trigger_table"
        assert lk["join_name"] == "APRef"
    assert not dropped


def test_object_prefix_resolves_to_its_declaring_type(corpus):
    """`clsAPInvoiceBL.ValidateHeader` — the qualifier names a declared type, which is in the file."""
    links, dropped, _ = _join(
        corpus, {"clsAPInvoiceBL": ["workflow::Workflow: Validate a header"]})
    lk = _index(links)[("clsAPInvoiceBL", "ClientSide/AP/clsAPInvoiceBL.vb")]
    assert lk["resolution"] in ("bare", "verbatim")
    assert not dropped


def test_qualified_object_name_matches_the_object_exactly(corpus):
    links, _, _ = _join(corpus, {"dbo.APInvHdr": ["data::Entity: AP Invoice Header"]})
    lk = _index(links)[("dbo.APInvHdr", "Scripts/Tables/AP_Tables.sql")]
    assert lk["resolution"] == "qualified_object"
    assert lk["match"] == "code_object"


def test_strong_qualifier_corroboration_comes_from_the_code_graph(corpus):
    """clsAPInvoiceBL.vb never writes the word `APInvHdr`, but the code graph connects the file to
    that table, which is corroboration enough — the bridge uses the graph, not only the text."""
    links, _, _ = _join(corpus, {"APInvHdr.Job": ["data::Rule: Job is required"]})
    lk = _index(links)[("APInvHdr.Job", "ClientSide/AP/clsAPInvoiceBL.vb")]
    assert lk["join_name"] == "Job"
    assert lk["qualifier_resolved_to"] == "APInvHdr"
    assert lk["qualifier_corroboration"] == "named"
    assert "flags" not in lk


def test_weak_qualifier_is_flagged_not_silently_upgraded(corpus):
    """A file that touches the COLUMN (per the parser) but neither names the table nor is linked to
    it by the code graph: the link is KEPT, because its assertion holds, and FLAGGED, because the
    corroboration only reaches the column."""
    entry = corpus / "ClientSide" / "AP" / "frmAPInvoiceEntry.vb"
    entry.write_text('Public Class frmAPInvoiceEntry\n'
                     '    Private Sub Fill(ByVal r As DataRow)\n'
                     '        txtJob.Text = r("Job").ToString()\n'
                     '    End Sub\n'
                     'End Class\n')
    code = json.loads(json.dumps(CODE))
    code["code_entities"].append({"path": "ClientSide/AP/frmAPInvoiceEntry.vb", "code_name": "Job",
                                 "business_name": "Job", "anchor_verdict": "confirmed"})
    facts = json.loads(json.dumps(FACTS))
    facts["vb"]["column_refs"].append(
        {"file": "ClientSide/AP/frmAPInvoiceEntry.vb", "name": "Job", "how": "read"})
    links, dropped, _ = _join(corpus, {"APInvHdr.Job": ["data::Rule: Job is required"]},
                              facts=facts, code=code)
    lk = _index(links)[("APInvHdr.Job", "ClientSide/AP/frmAPInvoiceEntry.vb")]
    assert lk["join_name"] == "Job"
    assert lk["qualifier_resolved_to"] == "APInvHdr"
    assert lk["qualifier_corroboration"] == "column"
    assert lk["flags"] == ["weak_qualifier"]


def test_unresolvable_qualifier_is_dropped_with_a_reason(corpus):
    """A qualifier that names no table, pseudo-table or type, for a bare name that is not a known
    column, cannot justify a file. Fail closed."""
    code = json.loads(json.dumps(CODE))
    code["code_entities"].append({"path": "ClientSide/AP/clsAPInvoiceBL.vb",
                                  "code_name": "errs", "business_name": "Errors",
                                  "anchor_verdict": "confirmed"})
    links, dropped, stats = _join(corpus, {"somethingUnknown.errs": ["x::y"]}, code=code)
    assert links == []
    assert [d["reason"] for d in dropped] == ["qualifier_unresolved"]
    assert "names no table" in dropped[0]["detail"]


def test_name_absent_is_dropped(corpus):
    code = json.loads(json.dumps(CODE))
    code["code_entities"] = [{"path": "ClientSide/AP/clsAPInvoiceBL.vb", "code_name": "LienWaiver",
                              "business_name": "Lien Waiver",
                              "anchor_verdict": "arbitrated_uncorroborated"}]
    links, dropped, _ = _join(corpus, {"LienWaiver": ["x::y"]}, code=code)
    assert links == []
    assert dropped[0]["reason"] == "name_absent"


def test_one_link_per_concept_and_candidate(corpus):
    links, _, _ = _join(corpus, {"h.Vendor": ["data::A", "data::B"]})
    assert len(links) == 2                       # one per concept, same candidate file
    assert {lk["concept"] for lk in links} == {"data::a", "data::b"}


def test_without_a_source_tree_nothing_claims_to_be_verified(tmp_path):
    """A degraded bridge says so rather than asserting checks it did not run."""
    src = eg.SourceText(None)
    assert src.available is False
    links, dropped, stats = eg.build_bridge(_symbols({"h.Vendor": ["data::A"]}), CODE, src, FACTS)
    assert dropped == []
    assert {lk["resolution"] for lk in links} == {"unverified"}
    assert stats["links_unverified"] == 2


def test_pages_are_a_fallback_source_tree(tmp_path):
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    body = "\n".join(f"{i + 1:5d} | {ln}" for i, ln in enumerate(BL_VB.splitlines()))
    (pages / "concat.txt").write_text(
        "[FILE: ClientSide/AP/clsAPInvoiceBL.vb KIND: raw]\n" + body + "\n"
        "[END FILE: ClientSide/AP/clsAPInvoiceBL.vb]\n")
    src, how = eg.SourceText.discover(tmp_path)
    assert src.available and "reconstructed" in how
    assert src.occurs("ClientSide/AP/clsAPInvoiceBL.vb", "vendor", word=True)
    assert not src.occurs("ClientSide/AP/clsAPInvoiceBL.vb", "nosuchname")


def test_split_symbol_and_norms():
    assert eg.split_symbol("h.Vendor") == ("h", "vendor")
    assert eg.split_symbol("[APInvHdr].[Job]") == ("apinvhdr", "job")
    assert eg.split_symbol("a.b.c") == ("b", "c")      # the IMMEDIATE qualifier owns the name
    assert eg.split_symbol("Vendor") == ("", "vendor")
    assert eg.norm_full("dbo.AP-Inv_Hdr") == "dbo.ap_inv_hdr"
    assert eg._norm("dbo.APInvHdr") == "apinvhdr"
    assert eg.word_re("vendor").search("cboVendor") is None
    assert eg.word_re("vendor").search("hdr(\"Vendor\")".lower()) is not None


# ----------------------------------------------------------------------------- canonical key
def test_canonical_key_round_trips_from_both_spellings():
    formatted = "[DT-0006] Invoice Number Required and Bounded"
    raw_header = "Data Invariant: Invoice Number Required and Bounded"
    assert ckey.canonical_title(formatted) == ckey.canonical_title(raw_header)
    assert ckey.concept_key("data", formatted) == ckey.concept_key("data", raw_header)
    key = ckey.concept_key("data", formatted)
    assert key == "data::invoice number required and bounded"
    # idempotent: canonicalising an already-canonical key is a no-op
    assert ckey.canonicalize_key(key) == key
    assert ckey.canonical_title(ckey.canonical_title(formatted)) == ckey.canonical_title(formatted)
    assert ckey.split_key(key) == ("data", "invoice number required and bounded")
    assert ckey.split_key("no still here") == ("", "no still here")
    # the still is part of the identity: dedup is documented as within-still
    assert ckey.concept_key("rule", formatted) != ckey.concept_key("data", formatted)
    # a title that merely CONTAINS a colon keeps its first clause
    assert ckey.canonical_title("Rule: Posting blocked: closed month") == \
        "posting blocked: closed month"
    # a lowercase prefix is not a type label
    assert ckey.canonical_title("foo: bar") == "foo: bar"
    assert ckey.concept_key("", "Entity: X") == "x"


def test_canonical_key_ignores_a_ref_comment_on_the_heading_line():
    # JC run 2026-10-07: the formatter kept `<!-- ref:… -->` on the `### ` line for 24 runtime
    # concepts, so their bridge keys never equalled the graph's key (104 links unresolved).
    formatted = "[RT-0038] Operational Rule: Change Order Lifecycle State Machine <!-- ref:45c60566 -->"
    raw_header = "Behavioral Pattern: Change Order Lifecycle State Machine"
    assert ckey.concept_key("runtime", formatted) == ckey.concept_key("runtime", raw_header) == \
        "runtime::change order lifecycle state machine"
    assert ckey.canonical_title("<!-- ref:1 --> [DT-1] Rule: X") == "x"
    assert ckey.canonicalize_key("runtime::Foo <!-- ref:ab -->") == "runtime::foo"


def test_concept_symbols_emits_the_canonical_key(tmp_path):
    fmt = tmp_path / "data" / "final" / "formatted" / "signal"
    fmt.mkdir(parents=True)
    (fmt / "x.md").write_text(
        "### [DT-0006] Invoice Number Required and Bounded\n"
        "- Statement:\n```\nREQUIRE APInvHdr.APRef IS NOT NULL\n```\n")
    idx = concept_symbols.build_index(str(tmp_path))
    (key, info), = idx["by_concept"].items()
    assert key == "data::[DT-0006] Invoice Number Required and Bounded"      # index key unchanged
    assert info["canonical_key"] == "data::invoice number required and bounded"
    assert idx["key_schema"]["canonical_key_source"].endswith("concept_key.py")
    assert "apinvhdr.apref" in {s.lower() for s in info["symbols"]}


def test_bridge_links_carry_the_canonical_concept_key(corpus):
    links, _, _ = _join(corpus, {"h.Vendor": ["data::[DT-0037] Entity: AP Invoice Header"]})
    lk = links[0]
    assert lk["concept"] == "data::entity: ap invoice header".replace("entity: ", "")
    assert lk["concept"] == "data::ap invoice header"
    assert lk["concept_index_key"] == "data::[DT-0037] Entity: AP Invoice Header"


def test_legacy_index_without_a_canonical_key_is_still_canonicalised(corpus):
    """A concept_symbols.json written before concept_key.py existed must still produce canonical
    links — the consumer canonicalises with the same function rather than passing the old key on."""
    cs = {"by_concept": {"data::[DT-0037] Entity: AP Invoice Header": {"title": "x"}},
          "by_symbol": {"h.Vendor": ["data::[DT-0037] Entity: AP Invoice Header"]}}
    links, _, _ = eg.build_bridge(cs, CODE, eg.SourceText(corpus), FACTS)
    assert links and links[0]["concept"] == "data::ap invoice header"


# ----------------------------------------------------------------------------- export CLI
def test_export_writes_the_dropped_ledger_and_meta(tmp_path, corpus):
    out = tmp_path / "run"
    stills = out / "stills"
    stills.mkdir(parents=True)
    (stills / "knowledge_graph.json").write_text(json.dumps(
        {"nodes": [{"id": "a" * 16, "title": "Entity: AP Invoice Header", "source_still": "data"}],
         "edges": []}))
    (stills / "concept_symbols.json").write_text(json.dumps(
        _symbols({"h.Vendor": ["data::Entity: AP Invoice Header"]})))
    db = stills / "shared_vectors.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE source_files (path TEXT, lang TEXT)")
    conn.execute("CREATE TABLE code_objects (path TEXT, object TEXT, kind TEXT)")
    conn.execute("CREATE TABLE code_edges (src TEXT, dst TEXT, type TEXT)")
    conn.execute("CREATE TABLE code_entities (path TEXT, code_name TEXT, business_name TEXT, "
                 "anchor_verdict TEXT)")
    for f in CODE["source_files"]:
        conn.execute("INSERT INTO source_files VALUES (?,?)", (f["path"], f["lang"]))
    for o in CODE["code_objects"]:
        conn.execute("INSERT INTO code_objects VALUES (?,?,?)",
                     (o["path"], o["object"], o["kind"]))
    for e in CODE["code_entities"]:
        conn.execute("INSERT INTO code_entities VALUES (?,?,?,?)",
                     (e["path"], e["code_name"], e["business_name"], e["anchor_verdict"]))
    conn.commit()
    conn.close()
    (out / "facts").mkdir()
    (out / "facts" / "facts.json").write_text(json.dumps(FACTS))

    r = subprocess.run([sys.executable, str(MCP / "export_graph.py"), str(out),
                        "--source-root", str(corpus)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bridge = json.loads((out / "knowledge_graph" / "concept_code_bridge.json").read_text())
    assert bridge["source_available"] is True
    assert bridge["parser_facts_used"] is True
    assert bridge["concept_key"].startswith("canonical")
    assert any(d["reason"] == "substring_only" for d in bridge["dropped"])
    assert all(lk["concept"] == "data::ap invoice header" for lk in bridge["links"])
    idx = json.loads((out / "knowledge_graph" / "index.json").read_text())
    assert idx["artifacts"]["concept_code_bridge"]["dropped"] >= 1
    assert idx["sources"]["source_text"].endswith(str(corpus))
