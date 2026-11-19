# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Java and JavaScript/TypeScript code-graph rigs and their Tree-sitter facts. No AWS, no network.

Bedrock is replaced with a fake that returns a canned reply; the facts come from
runtimes/facts/treesitter_facts.py run on runtimes/facts/tests/fixtures/ts-sample. What is under
test: `.java` / `.js .jsx .mjs .cjs` / `.ts .tsx` map to their own prompts, the reply goes through
the same CLR-shaped disposal as C#/VB/Python, comments and strings are stripped with each language's
syntax, and Tree-sitter facts confirm model edges and backfill the ones it missed (calls, imports
incl. external libraries, SQL-string tables, code objects), tagged `facts:java_*` / `facts:js_*`.

Run:  python3 -m pytest -q runtimes/mcp-server/tests/test_code_graph_java_js.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

MCP = Path(__file__).resolve().parents[1]
FACTS = MCP.parent / "facts"
TS = FACTS / "tests" / "fixtures" / "ts-sample"
sys.path.insert(0, str(MCP))
sys.path.insert(0, str(FACTS))

import bedrock_client  # noqa: E402
import code_graph  # noqa: E402

JSVC = "java/com/acme/billing/InvoiceService.java"
JREPO = "java/com/acme/billing/InvoiceRepository.java"
CART = "web/services/cart.ts"
FMT = "web/services/format.js"


@pytest.fixture(scope="module")
def ts_facts():
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_java")
    pytest.importorskip("tree_sitter_javascript")
    pytest.importorskip("tree_sitter_typescript")
    import treesitter_facts
    rels = sorted(str(p.relative_to(TS)) for p in TS.rglob("*") if p.is_file())
    return treesitter_facts.extract(TS, rels)


def _src(rel):
    return (TS / rel).read_text()


# The model finds some relations in InvoiceService.java and misses others.
JAVA_SVC_REPLY = {
    "file_role": "service", "module": None, "business_intent": "Pays open invoices.",
    "usings": ["java.util.List", "org.slf4j.Logger"],
    "declarations": [{"name": "InvoiceService", "kind": "class", "base": "BaseService", "implements": []}],
    "calls": [
        {"caller": "InvoiceService.pay", "callee": "InvoiceRepository.markPaid", "receiver": "repo",
         "confidence": 0.9},
        {"caller": "InvoiceService.pay", "callee": "isOpen", "receiver": None, "confidence": 0.9},
        {"caller": "InvoiceService.pay", "callee": "refund", "receiver": None, "confidence": 0.3},
    ],
    "data_refs": [], "references": [], "business_entities": [],
}

JAVA_REPO_REPLY = {
    "file_role": "repository", "module": None, "business_intent": "Marks invoices paid.",
    "usings": ["java.sql.Connection"],
    "declarations": [{"name": "InvoiceRepository", "kind": "class"}],
    "calls": [],
    "data_refs": [{"name": "invoices", "kind": "table", "operation": "update", "direction": "write",
                   "via": "inline_sql", "resolution": "static", "confidence": 0.9}],
    "references": [], "business_entities": [],
}

CART_REPLY = {
    "file_role": "service", "module": None, "business_intent": "Checks out a cart.",
    "usings": ["./format"],
    "declarations": [{"name": "CartService", "kind": "class"}],
    "calls": [
        {"caller": "CartService.checkout", "callee": "PriceApi.price", "receiver": "this.api",
         "confidence": 0.9},
        {"caller": "CartService.checkout", "callee": "fmt", "receiver": None, "confidence": 0.8},
    ],
    "data_refs": [], "references": [], "business_entities": [],
}


def _by(node, etype):
    return {code_graph._normalize_dst(e["dst"]): e for e in node["edges"] if e["type"] == etype}


# ── extension mapping and prompt selection ───────────────────────────────────────────────

def test_java_and_js_ts_extensions_map_to_their_languages():
    assert code_graph.CODE_EXTS["java"] == "java"
    for ext in ("js", "jsx", "mjs", "cjs"):
        assert code_graph.CODE_EXTS[ext] == "javascript"
    for ext in ("ts", "tsx"):
        assert code_graph.CODE_EXTS[ext] == "typescript"
    # the existing languages are unchanged
    assert {k: code_graph.CODE_EXTS[k] for k in ("sql", "cs", "vb", "cob", "cbl", "cpy", "py")} == {
        "sql": "sql", "cs": "csharp", "vb": "vbnet", "cob": "cobol", "cbl": "cobol", "cpy": "cobol",
        "py": "python"}
    for ext in ("jsx", "mjs", "cjs", "tsx"):
        assert code_graph._modality_of(ext) == "text-imperative"


@pytest.mark.parametrize("lang,label,fence,libs", [
    ("java", "Java", "```java", ("org.springframework", "@Table", "Spring Data")),
    ("javascript", "JavaScript", "```javascript", ("require(", "Sequelize", "Prisma")),
    ("typescript", "TypeScript", "```typescript", ("TypeORM", "@Entity", "Prisma")),
])
def test_each_language_has_its_own_prompt_with_the_shared_relations(lang, label, fence, libs):
    system, user = code_graph._LANG_PROMPTS[lang]
    assert label in system and label in user
    for key in ('"usings"', '"calls"', '"data_refs"', '"declarations"', "external libraries"
                if lang == "java" else "npm packages"):
        assert key in user
    for lib in libs:
        assert lib in user or lib in system
    out = user.format(path="a." + lang, source="x = 1")      # the template formats (braces escaped)
    assert fence in out and "@@" not in out and "x = 1" in out


def test_extract_one_selects_the_language_prompt(monkeypatch):
    seen = []

    def fake_call(llm, model_id, system, user, max_tokens=0, temperature=0.0):
        seen.append((system, user))
        return bedrock_client.ConverseResult(output_text=json.dumps(CART_REPLY), input_tokens=1,
                                             output_tokens=1, stop_reason="end_turn")

    monkeypatch.setattr(code_graph.bedrock_client, "call_llm", fake_call)
    for lang, fence in (("java", "```java"), ("javascript", "```javascript"),
                        ("typescript", "```typescript")):
        node, _ = code_graph._extract_one(object(), "m", "a", lang, "x", "page_000")
        assert node["lang"] == lang and fence in seen[-1][1]
        assert seen[-1][0] == code_graph._LANG_PROMPTS[lang][0]


# ── comment and string syntax ─────────────────────────────────────────────────────────────

def test_strip_comments_java():
    src = ('String u = "http://x"; // orders table\n'
           "char c = '/'; int i = n--; /* audit */ save(i);\n"
           "/* only_comment_table */\n"
           'String q = "say \\"hi\\" // still string"; // c')
    out = code_graph.strip_comments(src, "java").splitlines()
    assert out[0].rstrip() == 'String u = "http://x";'
    assert out[1].rstrip() == "char c = '/'; int i = n--;  save(i);"      # `--` is not a comment
    assert out[2] == ""
    assert out[3].rstrip() == 'String q = "say \\"hi\\" // still string";'


@pytest.mark.parametrize("lang", ["javascript", "typescript"])
def test_strip_comments_js_ts_quotes_and_template_literals(lang):
    src = ("const a = 'it\\'s // here'; // orders\n"
           "const b = `http://${host}/x`; /* note */ go(b);\n"
           "// only_comment_table\n"
           'const c = "/* not */";')
    out = code_graph.strip_comments(src, lang).splitlines()
    assert out[0].rstrip() == "const a = 'it\\'s // here';"
    assert out[1].rstrip() == "const b = `http://${host}/x`;  go(b);"
    assert out[2] == ""
    assert out[3] == 'const c = "/* not */";'


def test_strip_comments_other_languages_unchanged():
    assert code_graph.strip_comments("x = 1 ' note", "vbnet").rstrip() == "x = 1"
    assert code_graph.strip_comments("select 1 -- c", "sql").rstrip() == "select 1"
    assert code_graph.strip_comments("x = 'a' -- c", "csharp") == "x = 'a' -- c"   # no `--` for C#
    assert code_graph.strip_comments("x = 1 // c", "unknownlang").rstrip() == "x = 1"


# ── disposal without facts ────────────────────────────────────────────────────────────────

def test_java_reply_is_disposed_like_the_clr_rigs_without_facts():
    node = code_graph._assemble(JSVC, "java", JAVA_SVC_REPLY, _src(JSVC), "page_000", "m",
                                code_graph.FactsIndex(None))
    calls = _by(node, "CALLS")
    assert calls["markpaid"]["verdict"] == "confirmed"
    assert calls["refund"]["verdict"] == "unconfirmed"           # absent: kept and flagged
    assert set(_by(node, "IMPORTS")) == {"list", "logger"}
    assert not any((e.get("symbol") or "").startswith("facts:") for e in node["edges"])


# ── Tree-sitter confirmation and backfill: Java ───────────────────────────────────────────

def test_tree_sitter_facts_confirm_and_backfill_java_edges(ts_facts):
    idx = code_graph.FactsIndex(ts_facts)
    node = code_graph._assemble(JSVC, "java", JAVA_SVC_REPLY, _src(JSVC), "page_000", "m", idx)
    calls = _by(node, "CALLS")
    assert calls["markpaid"]["direction_verdict"] == "parser_confirmed"
    assert calls["isopen"]["direction_verdict"] == "parser_confirmed"
    # missed by the model, found by the parser: the inherited call and the constructor
    assert calls["audit"]["symbol"] == "facts:java_call" and calls["audit"]["verdict"] == "confirmed"
    assert calls["invoicerepository"]["symbol"] == "facts:java_call"
    imps = [e for e in node["edges"] if e["type"] == "IMPORTS"]
    # `import org.slf4j.Logger;` is confirmed by the parser's `org.slf4j` and NOT backfilled again
    assert [e["dst"] for e in imps if "slf4j" in e["dst"]] == ["org.slf4j.logger"]
    assert all(e["direction_verdict"] == "parser_confirmed" for e in imps)
    assert len(imps) == 2
    names = {d["name"] for d in node["defines"]}
    assert {"InvoiceService", "BaseService", "InvoiceService.pay", "BaseService.audit"} <= names


def test_java_external_import_and_sql_table_are_backfilled(ts_facts):
    idx = code_graph.FactsIndex(ts_facts)
    empty = dict(JAVA_SVC_REPLY, usings=[], calls=[])
    node = code_graph._assemble(JSVC, "java", empty, _src(JSVC), "page_000", "m", idx)
    imps = {e["dst"]: e for e in node["edges"] if e["type"] == "IMPORTS"}
    assert imps["org.slf4j"]["symbol"] == "facts:java_import_external"
    assert imps["java.util"]["symbol"] == "facts:java_import_stdlib"
    node = code_graph._assemble(JREPO, "java", dict(JAVA_REPO_REPLY, data_refs=[]), _src(JREPO),
                                "page_000", "m", idx)
    refs = {(code_graph._normalize_dst(e["dst"]), e["operation"], e["direction"]): e
            for e in node["edges"] if e["type"] == "REFERENCES"}
    assert refs[("invoices", "update", "write")]["symbol"] == "facts:java_inline_sql"
    # and a model table the parser also found is confirmed, not duplicated
    node = code_graph._assemble(JREPO, "java", JAVA_REPO_REPLY, _src(JREPO), "page_000", "m", idx)
    inv = [e for e in node["edges"] if e["type"] == "REFERENCES" and e["dst"] == "invoices"]
    assert len(inv) == 1 and inv[0]["direction_verdict"] == "parser_confirmed"


# ── Tree-sitter confirmation and backfill: JS / TS ────────────────────────────────────────

def test_tree_sitter_facts_confirm_and_backfill_ts_edges(ts_facts):
    idx = code_graph.FactsIndex(ts_facts)
    node = code_graph._assemble(CART, "typescript", CART_REPLY, _src(CART), "page_000", "m", idx)
    calls = _by(node, "CALLS")
    assert calls["price"]["direction_verdict"] == "parser_confirmed"
    # missed by the model: `this.log(...)` and the aliased import `fmt` -> formatTotal
    logc = [e for e in node["edges"] if e["type"] == "CALLS" and e["dst"].endswith("CartService.log")]
    assert len(logc) == 1 and logc[0]["symbol"] == "facts:js_call"
    ft = [e for e in node["edges"] if e["type"] == "CALLS" and e["dst"].endswith(":formatTotal")]
    assert len(ft) == 1 and ft[0]["symbol"] == "facts:js_call"
    imps = {e["dst"]: e for e in node["edges"] if e["type"] == "IMPORTS"}
    assert imps["./format"]["direction_verdict"] == "parser_confirmed"
    assert imps["axios"]["symbol"] == "facts:js_import_external"
    assert len(imps) == 2
    names = {d["name"] for d in node["defines"]}
    assert {"CartService", "PriceApi", "CartService.checkout", "PriceApi.price"} <= names


def test_js_module_function_ids_resolve_to_their_bare_name(ts_facts):
    idx = code_graph.FactsIndex(ts_facts)
    reply = {"file_role": "utility", "usings": ["path"], "calls": [
        {"caller": "formatTotal", "callee": "helper", "receiver": None, "confidence": 0.9}],
        "data_refs": [], "references": [], "business_entities": []}
    node = code_graph._assemble(FMT, "javascript", reply, _src(FMT), "page_000", "m", idx)
    calls = [e for e in node["edges"] if e["type"] == "CALLS"]
    # `web/services/format:helper` matches the model's `helper`: confirmed, not backfilled twice
    assert len(calls) == 1 and calls[0]["direction_verdict"] == "parser_confirmed"
    imps = [e for e in node["edges"] if e["type"] == "IMPORTS"]
    assert len(imps) == 1 and imps[0]["direction_verdict"] == "parser_confirmed"
    assert "helper" in idx.method_names and "formattotal" in idx.method_names


def test_js_inline_sql_tables_are_backfilled(tmp_path):
    pytest.importorskip("tree_sitter_javascript")
    import treesitter_facts
    (tmp_path / "repo.js").write_text(
        "const db = require('pg');\n"
        "async function ship(id) {\n"
        "  await db.query(`UPDATE orders SET status = 'SHIPPED' WHERE id = $1`, [id]);\n"
        "  return db.query('SELECT * FROM order_lines WHERE order_id = $1', [id]);\n"
        "}\nmodule.exports = { ship };\n")
    facts = treesitter_facts.extract(tmp_path, ["repo.js"])
    idx = code_graph.FactsIndex(facts)
    empty = {"usings": [], "calls": [], "data_refs": [], "references": [], "business_entities": []}
    node = code_graph._assemble("repo.js", "javascript", empty, (tmp_path / "repo.js").read_text(),
                                "page_000", "m", idx)
    refs = {(e["dst"], e["direction"]): e["symbol"] for e in node["edges"] if e["type"] == "REFERENCES"}
    assert refs[("orders", "write")] == "facts:js_inline_sql"
    assert refs[("order_lines", "read")] == "facts:js_inline_sql"
    imps = {e["dst"]: e["symbol"] for e in node["edges"] if e["type"] == "IMPORTS"}
    assert imps["pg"] == "facts:js_import_external"


# ── end to end through build_code_graph (Bedrock faked) ───────────────────────────────────

def test_build_code_graph_persists_java_and_ts_relations(tmp_path, monkeypatch, ts_facts):
    pages = tmp_path / ".pages" / "page_000"
    pages.mkdir(parents=True)
    chunks = []
    for rel in (JSVC, CART):
        body = "\n".join(f"{i + 1:5d} | {ln}" for i, ln in enumerate(_src(rel).splitlines()))
        chunks.append(f"[FILE: {rel} KIND: raw]\n{body}\n[END FILE: {rel}]\n")
    (pages / "concat.txt").write_text("".join(chunks))
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "facts.json").write_text(json.dumps(ts_facts))
    prompts = []

    def fake_call(llm, model_id, system, user, max_tokens=0, temperature=0.0):
        prompts.append((system, user))
        reply = JAVA_SVC_REPLY if "```java" in user else CART_REPLY
        return bedrock_client.ConverseResult(output_text=json.dumps(reply), input_tokens=10,
                                             output_tokens=20, stop_reason="end_turn")

    monkeypatch.setattr(code_graph.bedrock_client, "create_llm_client", lambda profile, region: object())
    monkeypatch.setattr(code_graph.bedrock_client, "call_llm", fake_call)
    db = tmp_path / "shared_vectors.db"
    stats = code_graph.build_code_graph(str(tmp_path / ".pages"), str(db), min_chars=100)
    assert stats["files"] == 2 and stats["ok"] == 2 and stats["errors"] == 0
    assert stats["parser_facts"]["present"] is True
    assert {"Java" in s for s, _ in prompts} == {True, False}
    assert any("TypeScript" in s for s, _ in prompts)
    conn = sqlite3.connect(str(db))
    edges = conn.execute("SELECT src, dst, type, symbol FROM code_edges").fetchall()
    langs = dict(conn.execute("SELECT path, lang FROM source_files").fetchall())
    objs = {r[0] for r in conn.execute("SELECT object FROM code_objects")}
    conn.close()
    assert langs == {JSVC: "java", CART: "typescript"}
    assert (CART, "axios", "IMPORTS", "facts:js_import_external") in edges
    assert any(s == JSVC and t == "CALLS" and sym == "facts:java_call" for s, _, t, sym in edges)
    assert {"invoiceservice", "baseservice.audit", "cartservice", "cartservice.checkout"} <= objs
