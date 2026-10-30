"""
test_citations.py — tests for the shared citation parse/verify primitive.

WHY THESE MATTER
----------------
A citation's only value is that a reader can follow it. If this module resolves a
reference to the wrong line, nothing downstream notices: the spec still looks evidenced,
the viewer still renders a link, and the error only surfaces when a human opens the file
and the rule is not there. So the assertions here are mostly about REFUSING — returning
None for anything unverifiable — rather than about happy-path parsing.

issues.py and the MCP trace tools both import this, so a regression here is a regression
in every consumer at once.

Run with:
    python3 -m pytest test_citations.py -v
"""

import pytest

import citations as C


# ── fixture: a run directory with paged source ───────────────────────────────


def _page(tmp_path, blocks):
    """Write one .pages/page_000/concat.txt containing `blocks` = [(marker, lines)]."""
    pdir = tmp_path / ".pages" / "page_000"
    pdir.mkdir(parents=True, exist_ok=True)
    out = []
    for marker, lines in blocks:
        out.append(f"[FILE: {marker}]")
        out.extend(f"{n:5d} | {t}" for n, t in lines)
        out.append(f"[END FILE: {marker}]")
    (pdir / "concat.txt").write_text("\n".join(out), encoding="utf-8")
    return tmp_path


@pytest.fixture
def run(tmp_path):
    _page(tmp_path, [
        ("src/Billing.vb KIND: raw", [
            (10, "Public Sub PostInvoice()"),
            (11, "    If vendor.ActiveYN <> \"Y\" Then"),
            (12, "        Throw New Exception(\"Vendor is inactive\")"),
            (13, "    End If"),
            (14, "End Sub"),
        ]),
        ("db/Tables.sql KIND: raw SIZE: 900", [
            (5, "CREATE TABLE dbo.APInvHdr ("),
            (6, "    RetgAmt numeric(12,2) NOT NULL"),
            (7, ")"),
        ]),
    ])
    return tmp_path


@pytest.fixture
def idx(run):
    return C.from_output_dir(run)


# ── page loading ─────────────────────────────────────────────────────────────


def test_load_sources_reads_paged_lines(idx):
    assert set(idx.sources) == {"src/Billing.vb", "db/Tables.sql"}
    assert idx.sources["src/Billing.vb"][12].strip().startswith("Throw New Exception")


def test_marker_attributes_are_stripped_from_the_path():
    """A [FILE: …] marker carries KIND:/SIZE:/VIA:/[PART …]; the path must come out clean.

    Leaving an attribute attached would make every citation to that file unresolvable.
    """
    for marker, expected in [
        ("a/b.vb KIND: raw", "a/b.vb"),
        ("a/b.sql KIND: inferred SIZE: 12 VIA: rpt", "a/b.sql"),
        ("a/b.cs [PART 2/3] KIND: raw", "a/b.cs"),
        ("a/b.txt KIND: referential REASON: binary", "a/b.txt"),
        ("plain.vb", "plain.vb"),
    ]:
        assert C.marker_path(marker) == expected


def test_line_numbers_come_from_the_page_not_a_file_read(idx):
    """Numbering is whatever the model was shown, so lines need not start at 1."""
    assert min(idx.sources["db/Tables.sql"]) == 5


# ── citation string parsing ──────────────────────────────────────────────────


@pytest.mark.parametrize("text,expect", [
    ("src/Billing.vb#L10-L14", ("src/Billing.vb", 10, 14)),
    ("src/Billing.vb#L10", ("src/Billing.vb", 10, 10)),
    ("src/Billing.vb:L10-L14", ("src/Billing.vb", 10, 14)),
    ("src/Billing.vb:10-14", ("src/Billing.vb", 10, 14)),
    ("src/Billing.vb lines 10-14", ("src/Billing.vb", 10, 14)),
])
def test_loc_str_accepts_the_shapes_citations_actually_use(text, expect):
    m = C.LOC_STR.match(text)
    assert m, text
    got = (m.group(1), int(m.group(2)), int(m.group(3) or m.group(2)))
    assert got == expect


def test_parse_inline_extracts_several_refs_from_one_evidence_line():
    """Real Evidence lines carry multiple refs and trailing prose annotations."""
    line = ("src/Billing.vb#L10-L14, db/Tables.sql#L5 (APInvHdr RetgAmt), "
            "src/Billing.vb#L10-L14")
    refs = C.parse_inline(line)
    assert [(r["file"], r["start"], r["end"]) for r in refs] == [
        ("src/Billing.vb", 10, 14),
        ("db/Tables.sql", 5, 5),
    ], "duplicates should collapse and the parenthetical must not become a ref"


def test_parse_inline_on_prose_without_citations_returns_nothing():
    assert C.parse_inline("No file references here at all.") == []
    assert C.parse_inline("") == []


# ── path resolution ──────────────────────────────────────────────────────────


def test_resolve_tolerates_how_citations_spell_paths(idx):
    for given in ["src/Billing.vb", "SRC/BILLING.VB", "./src/Billing.vb",
                  "src\\Billing.vb", "Billing.vb", "`src/Billing.vb`"]:
        assert idx.resolve(given) == "src/Billing.vb", given


def test_resolve_refuses_an_ambiguous_basename(tmp_path):
    """Two files sharing a basename must resolve to NEITHER.

    Guessing would attach a rule to the wrong file — a wrong answer that looks right.
    """
    _page(tmp_path, [
        ("a/Shared.vb KIND: raw", [(1, "one")]),
        ("b/Shared.vb KIND: raw", [(1, "two")]),
    ])
    i = C.from_output_dir(tmp_path)
    assert i.resolve("Shared.vb") is None
    assert i.resolve("a/Shared.vb") == "a/Shared.vb"   # unambiguous still works


def test_resolve_unknown_file_is_none(idx):
    assert idx.resolve("does/not/exist.vb") is None


# ── verification: the refusals that matter most ──────────────────────────────


def test_verified_citation_carries_ref_and_excerpt(idx):
    v = C.verify_location(idx, "src/Billing.vb#L11-L13")
    assert v["file"] == "src/Billing.vb"
    assert (v["start"], v["end"]) == (11, 13)
    assert v["ref"] == "src/Billing.vb:L11-L13"
    assert any("Vendor is inactive" in t for _, t in v["excerpt"])


def test_out_of_range_lines_are_refused(idx):
    """A line number the file does not have must NOT be reported as evidence."""
    assert C.verify_location(idx, "src/Billing.vb#L900-L950") is None
    assert C.verify_location(idx, "src/Billing.vb#L1") is None, \
        "line 1 is not in the paged range for this file"


def test_unknown_file_is_refused(idx):
    assert C.verify_location(idx, "nope.vb#L10") is None


def test_unparseable_input_is_refused(idx):
    for bad in ["just some prose", "", "L10-L20", 42, None, []]:
        assert C.verify_location(idx, bad) is None, repr(bad)


def test_reversed_range_is_normalised(idx):
    v = C.verify_location(idx, {"file": "src/Billing.vb", "start": 13, "end": 11})
    assert (v["start"], v["end"]) == (11, 13)


def test_dict_form_accepts_line_and_L_prefixes(idx):
    assert C.verify_location(idx, {"file": "src/Billing.vb", "line": 12})["start"] == 12
    v = C.verify_location(idx, {"path": "src/Billing.vb", "start": "L11", "end": "L13"})
    assert (v["start"], v["end"]) == (11, 13)


def test_dict_falls_back_to_its_ref_string(idx):
    v = C.verify_location(idx, {"ref": "src/Billing.vb#L11-L13", "start": "?"})
    assert v and (v["start"], v["end"]) == (11, 13)


# ── quote handling: the text is trusted over the line arithmetic ─────────────


def test_quote_present_in_range_verifies(idx):
    v = C.verify_location(idx, {"file": "src/Billing.vb", "start": 11, "end": 13,
                                "quote": "Vendor is inactive"})
    assert v["quote_verified"] is True


def test_in_bounds_but_wrong_range_is_relocated_to_the_quote(tmp_path):
    """A model's line arithmetic is less reliable than the text it quoted.

    When the cited range is real but the quoted text is elsewhere in the file, the
    citation is MOVED to where that text actually is, rather than dropped — losing a
    true finding to bad arithmetic would be worse than correcting it. Needs a file long
    enough that the quote falls outside the ±3 line tolerance.
    """
    _page(tmp_path, [("long/Mod.vb KIND: raw",
                      [(n, f"filler line {n}") for n in range(1, 60)]
                      + [(60, "Throw New Exception(\"Retainage must round half away\")")]
                      + [(n, f"more filler {n}") for n in range(61, 80)])])
    i = C.from_output_dir(tmp_path)

    v = C.verify_location(i, {"file": "long/Mod.vb", "start": 10, "end": 12,
                              "quote": "Retainage must round half away"})
    assert v is not None
    assert v["quote_verified"] is True
    assert v["start"] == 60, "should relocate to the line holding the quoted text"


def test_out_of_bounds_range_is_refused_even_with_a_real_quote(idx):
    """Relocation rescues a WRONG range, not an INVENTED one.

    The bounds check runs first, so a citation naming lines the file does not have is
    refused outright — even when its quote appears elsewhere in that file. A line number
    with no basis in the source should not be repaired into a plausible-looking one.
    """
    assert C.verify_location(idx, {"file": "src/Billing.vb", "start": 500, "end": 501,
                                   "quote": "Vendor is inactive"}) is None


def test_quote_absent_from_the_file_is_flagged_not_silently_accepted(idx):
    """quote_verified=False is the signal a caller needs to distrust this citation."""
    v = C.verify_location(idx, {"file": "src/Billing.vb", "start": 11, "end": 13,
                                "quote": "this text does not appear anywhere"})
    assert v is not None
    assert v["quote_verified"] is False


def test_no_quote_leaves_verification_unknown(idx):
    """None, not False: nothing was claimed, so nothing was contradicted."""
    assert C.verify_location(idx, "src/Billing.vb#L11-L13")["quote_verified"] is None


# ── excerpts ─────────────────────────────────────────────────────────────────


def test_excerpt_pads_and_caps(idx):
    rows = C.excerpt(idx.sources, "src/Billing.vb", 12, 12, pad=1, max_lines=30)
    assert [n for n, _ in rows] == [11, 12, 13]

    capped = C.excerpt(idx.sources, "src/Billing.vb", 10, 14, pad=5, max_lines=2)
    assert len(capped) <= 2


def test_excerpt_of_unknown_file_is_empty(idx):
    assert C.excerpt(idx.sources, "nope.vb", 1, 5) == []


def test_render_numbered_matches_the_page_format(idx):
    rows = C.excerpt(idx.sources, "db/Tables.sql", 5, 6, pad=0)
    out = C.render_numbered("db/Tables.sql", rows)
    assert out.startswith("[FILE: db/Tables.sql]")
    assert "    5 | CREATE TABLE dbo.APInvHdr (" in out


# ── issues.py must keep importing these under their original names ───────────


def test_issues_module_reuses_this_primitive():
    """Guards the refactor: issues.py must not reacquire a private copy.

    Two implementations of citation verification would drift, and drift produces
    confidently wrong line numbers.
    """
    issues = pytest.importorskip("issues")
    for name in ("SourceIndex", "verify_location", "load_sources", "excerpt",
                 "render_numbered", "LOC_STR"):
        assert getattr(issues, name) is getattr(C, name), \
            f"issues.{name} is not the shared citations.{name}"
