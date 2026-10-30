# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Paging conservation tests for concat-batch.sh + check-page-conservation.py (P0 blocker 1).

Run:  python3 -m pytest runtimes/pipeline/tests -q
No AWS calls; needs only bash + python3.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIRO = Path(__file__).resolve().parents[1]
MCP = KIRO.parent / "mcp-server"
CONCAT = KIRO / "concat-batch.sh"
CHECK = KIRO / "check-page-conservation.py"
DEFAULT_CAP = 380000


# ── helpers ──────────────────────────────────────────────────────────────────

def write(root: Path, rel: str, data) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_bytes(data.encode("utf-8"))
    return p


def build_pages(src: Path, entries: list[str], work: Path, budget: int = 300000, env=None,
                max_pages: int = 500) -> Path:
    """Same page loop as run_distillation.sh Phase 1."""
    manifest = work / "manifest.txt"
    manifest.write_text("".join(e + "\n" for e in entries), encoding="utf-8")
    pages = work / "pages"
    if pages.exists():
        shutil.rmtree(pages)
    pages.mkdir()
    full_env = {**os.environ, **(env or {})}
    offset, page = "0", 0
    while True:
        assert page < max_pages, "paging did not terminate"
        d = pages / f"page_{page:03d}"
        d.mkdir()
        subprocess.run(["bash", str(CONCAT), str(src), str(manifest), str(d), offset, str(budget)],
                       check=True, env=full_env, capture_output=True)
        files_in = (d / "stats.txt").read_text().split()[0]
        if files_in == "0":
            shutil.rmtree(d)
            break
        offset = (d / "next_offset.txt").read_text().strip()
        page += 1
    return pages


def run_check(src: Path, work: Path, env=None) -> tuple[int, dict, str]:
    full_env = {**os.environ, **(env or {})}
    r = subprocess.run([sys.executable, str(CHECK), "--pages", str(work / "pages"),
                        "--manifest", str(work / "manifest.txt"), "--source-root", str(src)],
                       env=full_env, capture_output=True, text=True)
    rep = json.loads((work / "pages" / "conservation_report.json").read_text(encoding="utf-8"))
    return r.returncode, rep, r.stdout + r.stderr


def pages_of(pages: Path):
    return sorted(p for p in pages.glob("page_*") if p.is_dir())


def units(pages: Path) -> list[dict]:
    out = []
    for p in pages_of(pages):
        out += [json.loads(ln) for ln in (p / "units.jsonl").read_text(encoding="utf-8").splitlines() if ln]
    return out


def emitted_lines(pages: Path, rel: str) -> dict[int, str]:
    """Reassemble line_no -> text for one file from page bodies (segments concatenated)."""
    got: dict[int, str] = {}
    hdr_re = re.compile(r"^\[FILE: " + re.escape(rel) + r"(?: \[PART \d+/\d+\])?(?: |\])")
    for p in pages_of(pages):
        inside = False
        for ln in (p / "concat.txt").read_text(encoding="utf-8").split("\n"):
            if hdr_re.match(ln):
                inside = True
                continue
            if ln.startswith("[END FILE: "):
                inside = False
                continue
            if inside:
                m = re.match(r"^ *(\d+) \| (.*)\Z", ln, re.S)
                assert m, ln[:80]
                n = int(m.group(1))
                got[n] = got.get(n, "") + m.group(2)
    return got


def assert_page_caps(pages: Path, budget: int, cap: int = DEFAULT_CAP):
    for p in pages_of(pages):
        size = (p / "concat.txt").stat().st_size
        assert size <= cap, f"{p.name}: {size} > {cap}"
        stats = (p / "stats.txt").read_text().split()
        assert int(stats[1]) == size
        assert int(stats[2]) <= budget, f"{p.name}: tokens {stats[2]} > budget {budget}"
        (p / "concat.txt").read_bytes().decode("utf-8")  # strict: no broken multibyte chars


def big_cobol(n=16000) -> str:
    return "\n".join(f"       MOVE WS-FIELD-{i:05d} TO OUT-REC-{i:05d}.  *> line {i}" for i in range(1, n + 1))


@pytest.fixture
def ws(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src, tmp_path


# ── below / at / above limits ────────────────────────────────────────────────

def test_below_limit_small_files_single_page(ws):
    src, work = ws
    write(src, "a.txt", "alpha\nbeta\n")
    write(src, "dir/b.vb", "Sub Main()\nEnd Sub")
    write(src, "empty.txt", "")
    pages = build_pages(src, ["a.txt", "dir/b.vb", "empty.txt"], work)
    assert len(pages_of(pages)) == 1
    u = units(pages)
    assert [(r["path"], r["parts"]) for r in u] == [("a.txt", 1), ("dir/b.vb", 1), ("empty.txt", 1)]
    concat = (pages / "page_000" / "concat.txt").read_text()
    assert "[FILE: a.txt KIND: raw]\n    1 | alpha\n    2 | beta\n    3 | \n[END FILE: a.txt]\n" in concat
    rc, rep, out = run_check(src, work)
    assert rc == 0, out
    assert rep["stats"]["emitted_files"] == 3


def _single_line_block(rel: str, text: str) -> int:
    return len(f"[FILE: {rel} KIND: raw]\n    1 | {text}\n[END FILE: {rel}]\n".encode())


@pytest.mark.parametrize("extra,expect_pages", [(0, 1), (1, 2)])
def test_exactly_at_and_one_byte_over_page_cap(ws, extra, expect_pages):
    src, work = ws
    cap = 4000
    env = {"CONCAT_PAGE_BYTE_CAP": str(cap)}
    a = "a" * 1500
    write(src, "a.txt", a)
    # choose b so that block(a) + "\n" + block(b) == cap exactly (+extra)
    fixed = _single_line_block("a.txt", a) + 1 + _single_line_block("b.txt", "")
    b = "b" * (cap - fixed + extra)
    write(src, "b.txt", b)
    pages = build_pages(src, ["a.txt", "b.txt"], work, env=env)
    assert len(pages_of(pages)) == expect_pages
    if extra == 0:
        assert (pages / "page_000" / "concat.txt").stat().st_size == cap
    assert all(r["parts"] == 1 for r in units(pages))  # each still a single unit
    assert_page_caps(pages, 300000, cap)
    rc, _, out = run_check(src, work, env)
    assert rc == 0, out


def test_file_exactly_filling_an_empty_page_is_not_split_and_one_more_line_is(ws):
    src, work = ws
    cap = 6000
    env = {"CONCAT_PAGE_BYTE_CAP": str(cap)}
    lines = [f"line {i:04d} " + "x" * 40 for i in range(1, 200)]
    write(src, "f.txt", "\n".join(lines))
    pages = build_pages(src, ["f.txt"], work, env=env)
    u = units(pages)
    assert len(u) > 1 and all(r["parts"] == len(u) for r in u)
    assert_page_caps(pages, 300000, cap)
    rc, _, out = run_check(src, work, env)
    assert rc == 0, out


@pytest.mark.parametrize("first", ["a_small.txt", "a_medium.txt"])
def test_16000_line_file_every_line_once(ws, first):
    """The two reproductions from pipeline-assessment.md Blocker 1 (Case A / Case B)."""
    src, work = ws
    write(src, "a_small.txt", "x" * 1400)
    write(src, "a_medium.txt", ("m" * 80 + "\n") * 520)
    big = big_cobol()
    write(src, "b_big.cbl", big)
    write(src, "c_tail.txt", "tail\n")
    pages = build_pages(src, [first, "b_big.cbl", "c_tail.txt"], work, budget=300000)
    u = [r for r in units(pages) if r["path"] == "b_big.cbl"]
    assert len(u) >= 3 and [r["part"] for r in u] == list(range(1, len(u) + 1))
    got = emitted_lines(pages, "b_big.cbl")
    src_lines = big.split("\n")
    assert sorted(got) == list(range(1, 16001))
    assert [got[i] for i in range(1, 16001)] == src_lines
    assert_page_caps(pages, 300000)
    # resume cursors: mid-file pages hand off "N:K"
    offsets = [(p / "next_offset.txt").read_text().strip() for p in pages_of(pages)]
    assert any(":" in o for o in offsets)
    rc, rep, out = run_check(src, work)
    assert rc == 0, out
    assert rep["stats"]["lines"] >= 16000


def test_token_budget_respected_for_oversized_files(ws):
    src, work = ws
    write(src, "b_big.cbl", big_cobol())
    budget = 50000
    pages = build_pages(src, ["b_big.cbl"], work, budget=budget)
    assert len(pages_of(pages)) >= 8  # ~980 KB numbered / 100 KB chars per page
    assert_page_caps(pages, budget)
    rc, _, out = run_check(src, work)
    assert rc == 0, out


def test_token_budget_splits_file_under_byte_cap(ws):
    src, work = ws
    write(src, "m.txt", "\n".join("y" * 100 for _ in range(1500)))  # ~160 KB numbered
    pages = build_pages(src, ["m.txt"], work, budget=20000)
    assert len(units(pages)) >= 4
    assert_page_caps(pages, 20000)
    rc, _, out = run_check(src, work)
    assert rc == 0, out


# ── multibyte ────────────────────────────────────────────────────────────────

def test_multibyte_oversized_file(ws):
    src, work = ws
    text = "\n".join(f"{i} 請求書 Rechnungsprüfung 🧾 ÆØÅ — façade" * 3 for i in range(9000))
    write(src, "mb.txt", text)
    pages = build_pages(src, ["mb.txt"], work)
    assert len(units(pages)) >= 2
    assert_page_caps(pages, 300000)
    got = emitted_lines(pages, "mb.txt")
    assert [got[i] for i in range(1, len(got) + 1)] == text.split("\n")
    rc, _, out = run_check(src, work)
    assert rc == 0, out


def test_multibyte_single_line_larger_than_page_is_segmented_without_breaking_chars(ws):
    src, work = ws
    line = "€漢🧾" * 60000  # 180k chars, 600 KB in UTF-8 (> 380 KB page)
    write(src, "one.txt", "head\n" + line + "\ntail")
    pages = build_pages(src, ["one.txt"], work)
    segs = [r for r in units(pages) if r["seg"]]
    assert len(segs) >= 2 and all(r["line_start"] == 2 for r in segs)
    assert_page_caps(pages, 300000)
    got = emitted_lines(pages, "one.txt")
    assert got == {1: "head", 2: line, 3: "tail"}
    rc, _, out = run_check(src, work)
    assert rc == 0, out


# ── a single part larger than an empty page ──────────────────────────────────

def test_single_line_larger_than_empty_page(ws):
    src, work = ws
    line = "Z" * 1_000_000
    write(src, "huge.sql", line)
    write(src, "after.txt", "still here")
    pages = build_pages(src, ["huge.sql", "after.txt"], work)
    u = units(pages)
    huge = [r for r in u if r["path"] == "huge.sql"]
    assert len(huge) >= 3 and [r["seg"] for r in huge] == list(range(1, len(huge) + 1))
    assert any(r["path"] == "after.txt" for r in u)
    assert_page_caps(pages, 300000)
    assert emitted_lines(pages, "huge.sql") == {1: line}
    rc, _, out = run_check(src, work)
    assert rc == 0, out


def test_single_part_larger_than_empty_page_with_tiny_page_cap(ws):
    src, work = ws
    cap = 1200
    env = {"CONCAT_PAGE_BYTE_CAP": str(cap)}
    write(src, "w.txt", "\n".join(["short"] * 3 + ["Q" * 5000] + ["end"]))
    pages = build_pages(src, ["w.txt"], work, env=env)
    assert_page_caps(pages, 300000, cap)
    rc, _, out = run_check(src, work, env)
    assert rc == 0, out


# ── binary / unreadable / referential ────────────────────────────────────────

def test_binary_and_mixed_files_are_recorded_not_dropped(ws):
    src, work = ws
    write(src, "pure.bin", bytes(range(0, 32)) * 200)
    write(src, "mixed.frx", b"Form1 caption\n" + bytes(range(1, 9)) * 50 + b"\nLabel1 text\n")
    write(src, "ok.txt", "ok")
    pages = build_pages(src, ["pure.bin", "mixed.frx", "ok.txt"], work)
    u = {r["path"]: r for r in units(pages)}
    assert u["pure.bin"]["status"] == "skipped" and u["pure.bin"]["content"] == "binary"
    assert u["mixed.frx"]["status"] == "emitted" and u["mixed.frx"]["content"] == "cleaned"
    concat = (pages / "page_000" / "concat.txt").read_text()
    assert "[FILE: pure.bin KIND: binary REASON: binary: no readable text]\n[END FILE: pure.bin]" in concat
    assert "pure.bin\tbinary: no readable text" in (pages / "page_000" / "skipped.tsv").read_text()
    assert "pure.bin" in (pages / "page_000" / "included.txt").read_text()
    rc, rep, out = run_check(src, work)
    assert rc == 0, out
    assert [s["path"] for s in rep["skipped"]] == ["pure.bin"]
    assert "pure.bin\tbinary" in (pages / "skipped_manifest.tsv").read_text()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read chmod 000 files")
def test_unreadable_and_missing_files_are_recorded(ws):
    src, work = ws
    p = write(src, "locked.vb", "secret")
    os.chmod(p, 0)
    write(src, "ok.txt", "ok")
    try:
        pages = build_pages(src, ["locked.vb", "gone.vb", "ok.txt"], work)
        u = {r["path"]: r for r in units(pages)}
        assert u["locked.vb"]["content"] == "unreadable" and "PermissionError" in u["locked.vb"]["reason"]
        assert u["gone.vb"]["content"] == "unreadable" and "FileNotFoundError" in u["gone.vb"]["reason"]
        assert u["ok.txt"]["status"] == "emitted"
        rc, rep, out = run_check(src, work)
        assert rc == 0, out
        assert {s["path"] for s in rep["skipped"]} == {"locked.vb", "gone.vb"}
    finally:
        os.chmod(p, 0o644)


def test_missing_file_recorded(ws):
    src, work = ws
    pages = build_pages(src, ["gone.vb"], work)
    assert units(pages)[0]["content"] == "unreadable"
    rc, _, out = run_check(src, work)
    assert rc == 0, out


def test_only_skipped_files_do_not_stop_paging(ws):
    """A page made only of marker-only units must still advance (FILES_IN > 0)."""
    src, work = ws
    for i in range(5):
        write(src, f"b{i}.bin", b"\x00\x01\x02" * 100)
    write(src, "last.txt", "last")
    pages = build_pages(src, [f"b{i}.bin" for i in range(5)] + ["last.txt"], work)
    assert any(r["path"] == "last.txt" for r in units(pages))
    rc, _, out = run_check(src, work)
    assert rc == 0, out


def test_referential_and_inferred_from_materialized_manifest(ws):
    src, work = ws
    write(src, "rpt/Inv.rpt", b"\x00\x01binary crystal")
    write(src, "pic.png", b"\x89PNG\x00\x00")
    lifted = write(work, "lifted/Inv.rpt.txt", "Report: Invoice\nField: Amount")
    mat = work / "materialized.tsv"
    mat.write_text(f"rpt/Inv.rpt\tinferred\t{lifted}\t30\t16\tcrystal\t0.9\t\n"
                   f"pic.png\treferential\t-\t0\t6\tnone\t0\t\n", encoding="utf-8")
    env = {"MATERIALIZED_MANIFEST": str(mat)}
    pages = build_pages(src, ["rpt/Inv.rpt", "pic.png"], work, env=env)
    concat = (pages / "page_000" / "concat.txt").read_text()
    assert "[FILE: rpt/Inv.rpt KIND: inferred SIZE: 16 VIA: crystal]\n    1 | Report: Invoice" in concat
    assert "[FILE: pic.png KIND: referential SIZE: 6]\n[END FILE: pic.png]" in concat
    rc, rep, out = run_check(src, work, env)
    assert rc == 0, out
    assert [s["kind"] for s in rep["skipped"]] == ["referential"]


# ── argument safety / resume ─────────────────────────────────────────────────

def test_paths_with_quotes_and_dollars_are_not_interpolated(ws, tmp_path):
    src = tmp_path / 'we"ird $HOME `x` src'
    src.mkdir()
    write(src, 'a "q" $USER.txt', "fine")
    work = tmp_path / "w$1"
    work.mkdir()
    pages = build_pages(src, ['a "q" $USER.txt'], work)
    assert units(pages)[0]["path"] == 'a "q" $USER.txt'
    rc, _, out = run_check(src, work)
    assert rc == 0, out


def test_invalid_offset_and_stale_cursor_fail(ws):
    src, work = ws
    write(src, "a.txt", "a")
    (work / "m.txt").write_text("a.txt\n")
    d = work / "o"
    r = subprocess.run(["bash", str(CONCAT), str(src), str(work / "m.txt"), str(d), "0; rm -rf /", "1000"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "invalid offset" in r.stderr
    r = subprocess.run(["bash", str(CONCAT), str(src), str(work / "m.txt"), str(d), "0:5", "1000"],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "out of range" in r.stderr


def test_resume_from_mid_file_cursor_continues_exactly(ws):
    src, work = ws
    write(src, "b_big.cbl", big_cobol())
    (work / "m.txt").write_text("b_big.cbl\n")
    d0, d1 = work / "p0", work / "p1"
    subprocess.run(["bash", str(CONCAT), str(src), str(work / "m.txt"), str(d0), "0", "300000"], check=True,
                   capture_output=True)
    nxt = (d0 / "next_offset.txt").read_text().strip()
    assert nxt == "0:1"
    subprocess.run(["bash", str(CONCAT), str(src), str(work / "m.txt"), str(d1), nxt, "300000"], check=True,
                   capture_output=True)
    u0 = json.loads((d0 / "units.jsonl").read_text().splitlines()[-1])
    u1 = json.loads((d1 / "units.jsonl").read_text().splitlines()[0])
    assert u1["part"] == u0["part"] + 1 and u1["line_start"] == u0["line_end"] + 1


# ── the check fails closed ───────────────────────────────────────────────────

def _good_multi_page(ws):
    src, work = ws
    write(src, "a.txt", "a\nb")
    write(src, "b_big.cbl", big_cobol())
    write(src, "c.txt", "c")
    pages = build_pages(src, ["a.txt", "b_big.cbl", "c.txt"], work)
    assert len(pages_of(pages)) >= 3
    assert run_check(src, work)[0] == 0
    return src, work, pages


def test_check_fails_on_missing_page(ws):
    src, work, pages = _good_multi_page(ws)
    shutil.rmtree(pages_of(pages)[1])
    rc, rep, _ = run_check(src, work)
    assert rc == 1 and any("parts" in e for e in rep["errors"])


def test_check_fails_on_duplicated_page(ws):
    src, work, pages = _good_multi_page(ws)
    shutil.copytree(pages_of(pages)[1], pages / "page_999")
    rc, rep, _ = run_check(src, work)
    assert rc == 1


def test_check_fails_on_dropped_line(ws):
    src, work, pages = _good_multi_page(ws)
    c = pages_of(pages)[1] / "concat.txt"
    text = c.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^ *\d+ \| .*\n", "", text, count=1)
    c.write_text(text, encoding="utf-8")
    rc, _, _ = run_check(src, work)
    assert rc == 1


def test_check_fails_on_altered_text(ws):
    src, work, pages = _good_multi_page(ws)
    c = pages_of(pages)[2] / "concat.txt"
    c.write_text(c.read_text(encoding="utf-8").replace("OUT-REC", "OUT-RXC", 1), encoding="utf-8")
    rc, rep, _ = run_check(src, work)
    assert rc == 1 and any("differs from source" in e for e in rep["errors"])


def test_check_fails_on_entry_missing_from_pages(ws):
    src, work, pages = _good_multi_page(ws)
    with open(work / "manifest.txt", "a") as fh:
        fh.write("never_paged.txt\n")
    rc, rep, _ = run_check(src, work)
    assert rc == 1 and any("never_paged.txt" in e for e in rep["errors"])


def test_check_fails_on_legacy_page_without_ledger(ws):
    src, work, pages = _good_multi_page(ws)
    (pages_of(pages)[0] / "units.jsonl").unlink()
    rc, rep, _ = run_check(src, work)
    assert rc == 1 and any("pre-fix" in e for e in rep["errors"])


def test_check_fails_on_false_skip_claim(ws):
    src, work = ws
    write(src, "a.txt", "readable")
    pages = build_pages(src, ["a.txt"], work)
    p = pages_of(pages)[0]
    rec = dict(idx=0, path="a.txt", part=1, parts=1, line_start=0, line_end=0, seg=0, segs=0,
               status="skipped", content="binary", total_lines=0, reason="binary: no readable text")
    (p / "units.jsonl").write_text(json.dumps(rec) + "\n")
    (p / "concat.txt").write_text("[FILE: a.txt KIND: binary REASON: binary: no readable text]\n[END FILE: a.txt]\n")
    rc, rep, _ = run_check(src, work)
    assert rc == 1 and any("not confirmed" in e for e in rep["errors"])


# ── downstream marker parsers still parse ────────────────────────────────────

def test_downstream_parsers_read_new_pages(ws):
    src, work = ws
    write(src, "a.cs", "class A { }")
    write(src, "b_big.cs", "\n".join(f"class C{i} {{ void M() {{ }} }}" for i in range(16000)))
    write(src, "huge.sql", "SELECT " + "x," * 300000 + "1")
    write(src, "pure.bin", b"\x00\x01\x02" * 50)
    pages = build_pages(src, ["a.cs", "b_big.cs", "huge.sql", "pure.bin"], work)
    assert run_check(src, work)[0] == 0

    # code_graph imports bedrock_client (boto3) at module load; the parsers never call it.
    # Stub boto3/botocore when absent so this runs on hosts without the venv (no AWS calls).
    try:
        import boto3  # noqa: F401
    except ImportError:
        import types
        stubs = {n: types.ModuleType(n) for n in ("boto3", "botocore", "botocore.config", "botocore.exceptions")}
        stubs["botocore.config"].Config = object
        for exc in ("ClientError", "ConnectTimeoutError", "ReadTimeoutError"):
            setattr(stubs["botocore.exceptions"], exc, type(exc, (Exception,), {}))
        for n, mod in stubs.items():
            sys.modules.setdefault(n, mod)
    sys.path.insert(0, str(MCP))
    try:
        import code_graph
        import rectify
        import source_index
        import symbol_backmap
    finally:
        sys.path.pop(0)

    headers = [ln for p in pages_of(pages) for ln in (p / "concat.txt").read_text().split("\n")
               if ln.startswith("[FILE: ")]
    parsed = [code_graph.parse_file_marker(code_graph.FILE_MARKER.match(h).group(1)) for h in headers]
    assert {p for p, _ in parsed} == {"a.cs", "b_big.cs", "huge.sql", "pure.bin"}
    parts = [a for p, a in parsed if p == "b_big.cs"]
    assert parts[0]["part"][0] == 1 and all("original_line_start" in a for a in parts)
    seg = [a for p, a in parsed if p == "huge.sql"]
    assert seg and all(a.get("line_segment") for a in seg)
    binary = [a for p, a in parsed if p == "pure.bin"][0]
    assert binary["kind"] == "binary" and binary["reason"].startswith("binary")

    idx = source_index.build_page_file_index(str(pages))
    assert set(sum(idx.values(), [])) == {"a.cs", "b_big.cs", "huge.sql", "pure.bin"}

    loc = symbol_backmap.index_pages(str(pages))["locator"]
    assert {"a.cs", "b_big.cs", "huge.sql", "pure.bin"} <= set(loc)

    inst = rectify._collect_instances(pages, "cs", max_bytes=10_000_000)
    by_path = dict(inst)
    assert by_path["a.cs"].strip() == "class A { }"
    assert by_path["b_big.cs"].startswith("class C0 ")  # no marker tail leaking into the body

    recon = list(code_graph.reconstruct_code_files(pages))
    assert any(p == "a.cs" for p, _, _ in recon)
    assert all(p != "pure.bin" for p, _, _ in recon)
