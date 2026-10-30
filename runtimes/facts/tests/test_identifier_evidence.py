# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase 1.3 identifier evidence (no AWS, no dotnet): abbreviated names on samples/ap-invoices and
a synthetic fixture get the evidence of their meaning; a supported expansion is distinguishable
from an unsupported one; the per-page block is bounded and lands in the map prompt."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
FACTS = REPO / "runtimes" / "facts"
MCP = REPO / "runtimes" / "mcp-server"
AP = REPO / "samples" / "ap-invoices"
FIX = Path(__file__).resolve().parent / "fixtures" / "ident-sample"
sys.path.insert(0, str(FACTS))
sys.path.insert(0, str(MCP))
import identifier_evidence as ie  # noqa: E402
import jobs  # noqa: E402


def _rels(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != "README.md")


@pytest.fixture(scope="module")
def ap():
    rels = _rels(AP)
    return {n: e.to_json() for n, e in ie.collect(AP, rels).items()}, rels


@pytest.fixture(scope="module")
def fx():
    return {n: e.to_json() for n, e in ie.collect(FIX, _rels(FIX)).items()}


def test_abbreviation_shape_is_generic():
    assert ie.abbreviation_score("LienWvr") > 0 and ie.abbreviation_score("RetgAmt") > 0
    assert ie.abbreviation_score("UIMth") > 0 and ie.abbreviation_score("XmitFlg") > 0
    for plain in ("Vendor", "InvoiceDate", "Description", "SELECT", "String", "i", "total"):
        assert ie.abbreviation_score(plain) == 0, plain
    assert ie.canonical("chkLienWvr") == "LienWvr" and ie.canonical("@RetgAmt") == "RetgAmt"
    assert ie.canonical("m_Parms") == "Parms"
    # no corpus vocabulary in the module: none of the sample's abbreviations are hard-coded
    src = (FACTS / "identifier_evidence.py").read_text()
    for term in ("LienWvr", "Retg", "UIMth", "Wvr", "Xmit", "lien", "retainage", "waiver"):
        assert not re.search(rf"\b{term}", src, re.I), term


def test_ap_sample_evidence(ap):
    g, _ = ap
    lw = g["LienWvr"]
    assert lw["status"] == "stated"
    assert any("Lien waiver received" in it["text"] for it in lw["evidence"]["label"])
    retg = g["RetgAmt"]
    assert retg["status"] == "stated" and retg["column_or_parameter"]
    assert any("retainage withheld" in it["text"] for it in retg["evidence"]["comment"])
    assert any(it["file"].endswith("AP_Tables.sql") for it in retg["evidence"]["def"])
    uim = g["UIMth"]
    assert uim["status"] == "stated"
    assert any("month the user had open in the UI" in it["text"] for it in uim["evidence"]["comment"])
    # usage-only: set to 'Y' when a hold is released, tested <> 'Y' in approval
    rcvd = g["LienWvrRcvd"]
    assert rcvd["status"] == "usage"
    assert any(it.get("in") == "usp_APHoldRelease" for it in rcvd["evidence"]["set"])
    # every cited line really holds the identifier (or its comment / label)
    for name in ("LienWvr", "RetgAmt", "UIMth", "LienWvrRcvd"):
        for items in g[name]["evidence"].values():
            for it in items:
                line = (AP / it["file"]).read_text().splitlines()[it["line"] - 1]
                assert name in line or it["text"].strip("…")[:20] in line, (name, it)


def test_supported_vs_unsupported_expansion(ap, fx):
    g, _ = ap
    # supported: the evidence states it
    assert ie.expansion_supported(g["LienWvr"], "lien waiver")
    assert ie.expansion_supported(g["RetgAmt"], "retainage amount")
    assert ie.expansion_supported(g["UIMth"], "month open in the UI")
    # unsupported: a plausible-sounding guess from the letters
    assert not ie.expansion_supported(g["LienWvr"], "executive sign-off")
    assert not ie.expansion_supported(g["UIMth"], "user interface method")
    # fixture: XmitFlg has no comment, only how it is set (MarkTransmitted) and tested
    # (GetUntransmitted), so "transmitted" is supported by usage and "executive" is not
    x = fx["XmitFlg"]
    assert x["status"] == "usage"
    assert {it.get("in") for it in x["evidence"]["set"]} == {"MarkTransmitted"}
    assert {it.get("in") for it in x["evidence"]["tested"]} == {"GetUntransmitted"}
    assert ie.expansion_supported(x, "transmitted flag")
    assert not ie.expansion_supported(x, "executive approval flag")
    # stated by a same-line comment
    assert fx["PkgWgt"]["status"] == "stated"
    assert ie.expansion_supported(fx["PkgWgt"], "package weight")
    # rendered: a usage-only entry says how it is set and where
    line = ie.render_entry(x)
    assert line.startswith("- XmitFlg [usage]") and "set in MarkTransmitted" in line
    assert "tested in GetUntransmitted" in line


def _fake_pages(root: Path, rels: list[str], per_page: int) -> Path:
    pd = root / ".pages"
    for i in range(0, len(rels), per_page):
        d = pd / f"page_{i // per_page:03d}"
        d.mkdir(parents=True)
        with open(d / "units.jsonl", "w") as f:
            for r in rels[i:i + per_page]:
                n = len((AP / r).read_text(errors="replace").splitlines())
                f.write(json.dumps({"path": r, "line_start": 1, "line_end": n, "status": "emitted"}) + "\n")
    return pd


def test_build_bounded_page_blocks_and_hash(tmp_path):
    rels = _rels(AP)
    man = tmp_path / "manifest.txt"
    man.write_text("\n".join(rels) + "\n")
    pd = _fake_pages(tmp_path, rels, 6)
    out = tmp_path / "identifiers"
    st = ie.build(AP, man, pd, out, max_chars=1500, max_entries=8)
    assert st["status"] == "ok" and st["counts"]["identifiers"] > 20
    blocks = sorted((out / "pages").glob("page_*.md"))
    assert blocks
    for b in blocks:
        text = b.read_text()
        assert len(text) <= 1500 + 1
        assert text.startswith("IDENTIFIER EVIDENCE") and 'meaning: Unknown' in text
        assert len(re.findall(r"^- \w+ \[(?:stated|usage|none)\]", text, re.M)) <= 8
    h1 = ie.blocks_hash(out)
    assert h1 and h1.startswith("sha256:")
    st2 = ie.build(AP, man, pd, tmp_path / "again", max_chars=1500, max_entries=8)
    assert ie.blocks_hash(tmp_path / "again") == h1                # deterministic
    ie.build(AP, man, pd, tmp_path / "small", max_chars=900, max_entries=8)
    assert ie.blocks_hash(tmp_path / "small") != h1                # bound is part of the content
    assert st2["counts"] == st["counts"]


def test_block_is_appended_to_the_map_page_after_facts(tmp_path):
    fd, idd = tmp_path / "facts", tmp_path / "ident"
    fd.mkdir(); idd.mkdir()
    (fd / "page_000.md").write_text("FACTS BLOCK")
    (idd / "page_000.md").write_text("IDENTIFIER EVIDENCE ...\n- X [none] no evidence found")
    out = jobs.page_with_facts("PAGE", "page_000", str(fd), str(idd))
    assert out.index("FACTS BLOCK") < out.index("[IDENTIFIER EVIDENCE]") < out.index("- X [none]")
    assert jobs.page_with_facts("PAGE", "page_000", "", str(idd)).startswith("PAGE\n\n[IDENTIFIER EVIDENCE]")
    assert jobs.page_with_facts("PAGE", "page_001", str(fd), str(idd)) == "PAGE"
