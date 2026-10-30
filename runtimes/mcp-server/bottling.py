#!/usr/bin/env python3
"""
bottling.py — Final transformation: Master Blend → customer-facing Business Specification.

Architecture Decision: deterministic skeleton, LLM only for the voice
──────────────────────────────────────────────────────────────────────
Bottling recasts analytical findings into authoritative specification language
(SHALL/MUST/SHOULD). Only that voice shift needs a model. Everything a customer relies on is
done deterministically so it cannot be lost or hallucinated:

  * Structure: ## sections (duplicate generated headings collapsed), ### groups (pipeline
    prefixes like "rule — " removed) and the order of requirements come from the blend.
  * Requirements: every `#### [ID]` block in the blend appears in the spec. A requirement the
    blend repeats in several sections is specified ONCE (in its home section) and
    cross-referenced elsewhere, so the spec is much smaller than the blend with nothing dropped.
  * Traceability: each requirement's `Evidence:` citations are carried from the blend block,
    normalized to `path:Lx-Ly`, and checked against the run's source pages (or a source root).
  * Clean-up: pipeline vocabulary (stills, convergence, confidence scores, master blend, ...),
    HTML comments and internal sections (Confidence Map and Methodology) are removed.
  * Requirements index: one line per requirement (id, title, section, first citation).

LLM calls: (1) requirement batches, chunked within a ## section, with the shared system prompt
prompt-cached; a batch that hits max_tokens is halved and retried (fail closed at depth); a
requirement the model omits is retried once, then falls back to the deterministically cleaned
blend text. (2) one small call for section overviews (from the blend's per-section intros) and
(3) one small call for the README narrative, built from those overviews, the requirements index
and deterministic counts, never from the whole blend.

Usage:
    python3 bottling.py <master_blend_dir> <output_dir> [target_name] [--source-root DIR]
"""

import json
import re
import sys
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import bedrock_client


# ── Prompts ──────────────────────────────────────────────────────────────────

def _load_bottling_prompt() -> str:
    """Load the bottling prompt from still_specs/bottling/prompt.md."""
    candidates = [
        Path(__file__).parent.parent.parent / "still_specs" / "bottling" / "prompt.md",
        Path("still_specs/bottling/prompt.md"),
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return BOTTLING_SYSTEM_PROMPT


BOTTLING_SYSTEM_PROMPT = """You restate requirement blocks recovered from a legacy system's source code
as an authoritative business specification (SHALL/MUST/SHOULD/MAY/TBD).

- One output block per input block, same order, each starting with its `#### [ID] Title` heading verbatim.
- Never drop business content (rules, values, states, transitions, steps, exceptions, permissions).
- Copy fenced formal statements and tables verbatim.
- Do not write Evidence lines, file paths or line numbers (citations are attached automatically).
- Never mention how the specification was produced (stills, convergence, confidence, blend, distillation).
- Do not invent requirements. About 120 words per block, excluding formal statements and tables.
"""

CHUNK_PROMPT = """Restate the {k} requirement block(s) below (part {i} of {n}; section: {section}).
Output ONLY the restated blocks, in the same order, each starting with its `#### [ID] Title` heading
copied verbatim. No preamble, no title, no summary, no other headings, no Evidence lines."""

OVERVIEW_PROMPT = """Below are the introductions of the sections of a business specification for the
system "{system}". They were written for an internal audience and mention the tooling that produced
the specification. For EACH section, write a customer-facing overview of 2-3 sentences that says what
the section specifies, in plain business language.

Rules: do not mention how the specification was produced (no "still", "convergence", "confidence",
"blend", "distillation", "extraction", "scores"); do not state requirements that are not in the
introduction; no file names. Output exactly one block per section, in this format and nothing else:

=== <section title copied verbatim> ===
<overview>
"""

README_PROMPT = """Write the narrative part of the executive summary of the business specification of
the system "{system}". You are given the section overviews, the requirements index (id and title of
every requirement) and deterministic counts. Use only this input; do not invent capabilities.

Output exactly these three markdown sections and nothing else:

## System Overview
(one paragraph: what the system does and its main business areas)

## Critical Findings
(3-5 bullets: the most important business rules or risks for a modernization team; cite requirement
ids in brackets, e.g. [RL-0001])

## Known Gaps
(bullets: open points marked TBD or listed as gaps in the input; say "None recorded." if there are none)

Never mention how the specification was produced (no "still", "convergence", "confidence", "blend",
"distillation", "extraction", "scores")."""


# ── Tunables ─────────────────────────────────────────────────────────────────

# Requirement batch size (chars of cleaned block text). Output is capped at ~120 words per block,
# so output < input; CHUNK_MAX_TOKENS leaves headroom.
CHUNK_CHARS = 40_000
CHUNK_MAX_TOKENS = 32_000
CHUNK_CONCURRENCY = 4
MAX_SPLIT_DEPTH = 3
MIN_SPLIT_CHARS = 2000
OVERVIEW_MAX_TOKENS = 4096
README_MAX_TOKENS = 4096

# Sections that describe how the spec was produced, not what the system does.
INTERNAL_SECTION_RE = re.compile(r"confidence map|methodology|reconciliation report", re.I)

# Preferred home section for a requirement kind (ID prefix -> section-title regex). A requirement
# repeated across sections is specified in full in its home section and cross-referenced elsewhere.
HOME_SECTION = {
    "DT": r"object model", "DL": r"object model|glossary", "ST": r"state machine|lifecycle",
    "WF": r"workflow", "RL": r"business rules", "EX": r"edge case|exception",
    "TS": r"protected|acceptance", "IF": r"interface", "PM": r"authori[sz]ation|permission",
}

KIND_NAMES = OrderedDict([
    ("RL", "Business rules"), ("DT", "Data entities and invariants"),
    ("ST", "Lifecycle states and guards"), ("WF", "Workflows"),
    ("EX", "Exceptions and edge cases"), ("TS", "Acceptance criteria"),
    ("IF", "Interface contracts"), ("PM", "Authorization rules"), ("DL", "Domain terms"),
])

STILL_NAMES = ("data", "domain_language", "exception", "history", "interface", "permission",
               "rule", "runtime", "state", "test", "workflow", "boundary", "business",
               "capability", "concept", "lifecycle", "naming", "obligation", "persistent",
               "protected", "role")

ID_HEAD_RE = re.compile(r"^#### \[([A-Z]{2,4}-\d+)\]\s*(.*?)\s*$")
# `- Evidence: x`, `Evidence: x`, `**Evidence**: x` and `**Evidence:** x` (colon inside the bold:
# the closing `**` must not end up in front of the first citation).
EVIDENCE_LINE_RE = re.compile(r"^\s*[-*]?\s*\**Evidence\**\s*:\s*(?:\*\*\s*)?(.*)$", re.I)
SCORE_LINE_RE = re.compile(
    r"^\s*[-*]?\s*\**(Business Relevance|Evidence Diversity|Confidence|Convergence|Divergence)"
    r"\**\s*:", re.I)
CITE_RE = re.compile(r"^(?P<path>.+?\.[A-Za-z0-9]+)\s*(?:#|:)\s*L(?P<a>\d+)(?:\s*-\s*L?(?P<b>\d+))?$")
INLINE_REF_RE = re.compile(r"([\w./()\- ]*?[\w)]\.[A-Za-z0-9]+)#L(\d+)(?:-L?(\d+))?")


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Block:
    rid: str
    title: str
    body: str                       # cleaned body (no evidence / scores / comments)
    citations: list                 # [(path, a, b)]
    section: str = ""               # home section title
    text: str = ""                  # final bottled body


@dataclass
class Section:
    title: str
    intro: str = ""
    items: list = field(default_factory=list)   # ("h3", text) | ("h4", text) | ("prose", text) | ("block", rid)
    internal: bool = False


# ── Parsing the blend ────────────────────────────────────────────────────────

_FENCE_BREAK_RE = re.compile(r"^(#{1,4} \S|<!-- From )")


def _fence_aware_lines(text: str):
    """Yield (line, in_fence) so headings inside ``` fences are never treated as structure."""
    fence = False
    for ln in text.split("\n"):
        if fence and _FENCE_BREAK_RE.match(ln):
            fence = False                 # unclosed fence (truncated upstream block): recover
        if ln.lstrip().startswith("```"):
            yield ln, True
            fence = not fence
            continue
        yield ln, fence


def parse_citations(evidence: str) -> list:
    """`a.vb#L1-L5, b.sql#L9; c.sql:L3-L4` -> [(path, a, b)]. Tokens that are not a
    path+line reference are ignored."""
    out = []
    for tok in re.split(r"[;,]\s*", evidence):
        tok = tok.strip().strip("`").strip()
        m = CITE_RE.match(tok)
        if m:
            a = int(m.group("a"))
            b = int(m.group("b") or a)
            out.append((m.group("path").strip(), a, max(a, b)))
    return out


def format_citation(path: str, a: int, b: int) -> str:
    return f"{path}:L{a}" if a == b else f"{path}:L{a}-L{b}"


def _clean_block_body(lines: list) -> tuple[str, list]:
    """Drop evidence/score/comment lines from a block body; return (body, citations)."""
    cites, keep = [], []
    for ln, fence in _fence_aware_lines("\n".join(lines)):
        if not fence:
            m = EVIDENCE_LINE_RE.match(ln)
            if m:
                cites += parse_citations(m.group(1))
                continue
            if SCORE_LINE_RE.match(ln) or re.match(r"^\s*<!--.*-->\s*$", ln):
                continue
        keep.append(ln)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    if sum(1 for ln in body.split("\n") if ln.lstrip().startswith("```")) % 2:
        body += "\n  ```"                 # close a fence left open by a truncated upstream block
    return body, cites


def strip_pipeline_prefix(heading: str) -> str:
    """`rule — Business Rule Catalog` -> `Business Rule Catalog`."""
    m = re.match(r"^(\w+)\s+—\s+(.+)$", heading.strip())
    if m and m.group(1).lower() in STILL_NAMES:
        return m.group(2).strip()
    return heading.strip()


def parse_blend(blend: str):
    """Return (doc_title, [Section], {rid: [variant Block, ...]} in first-seen order)."""
    m = re.search(r"^# (.+)$", blend, flags=re.M)
    doc_title = m.group(1).strip() if m else ""
    pieces = _split_at(blend, r"^## ")
    sections: list[Section] = []
    variants: "OrderedDict[str, list[Block]]" = OrderedDict()
    for piece in pieces:
        if not piece.startswith("## "):
            continue                                   # document header
        head, _, body = piece.partition("\n")
        title = head[3:].strip()
        if sections and sections[-1].title == title:
            sec = sections[-1]                         # duplicated generated heading: merge
        else:
            sec = Section(title=title, internal=bool(INTERNAL_SECTION_RE.search(title)))
            sections.append(sec)
        _parse_section_body(sec, body, variants)
    return doc_title, sections, variants


def _parse_section_body(sec: Section, body: str, variants) -> None:
    prose: list[str] = []
    block: list[str] | None = None
    block_head = None
    seen_structure = bool(sec.items)

    def flush_prose():
        txt = "\n".join(prose).strip()
        prose.clear()
        if not txt:
            return
        if not seen_structure and not sec.intro:
            sec.intro = txt
        else:
            sec.items.append(("prose", txt))

    def flush_block():
        nonlocal block, block_head
        if block_head is None:
            return
        rid, title = block_head
        b, cites = _clean_block_body(block)
        variants.setdefault(rid, []).append(Block(rid, title, b, cites, section=sec.title))
        sec.items.append(("block", rid))
        block, block_head = None, None

    for ln, fence in _fence_aware_lines(body):
        if not fence:
            mid = ID_HEAD_RE.match(ln)
            if mid:
                flush_prose(); flush_block()
                seen_structure = True
                block_head, block = (mid.group(1), mid.group(2)), []
                continue
            if re.match(r"^#{1,4} ", ln):
                flush_prose(); flush_block()
                seen_structure = True
                level = len(ln) - len(ln.lstrip("#"))
                text = ln[level:].strip()
                if level <= 2:
                    continue                      # a stray ## line inside a section body
                sec.items.append(("h3" if level == 3 else "h4", strip_pipeline_prefix(text)))
                continue
            s = ln.strip()
            if s in ("---", "*No content to format.*"):
                continue
            if s.startswith("<!--") and block is None:
                if s.startswith("<!-- From"):       # start of an included document: intro ends
                    flush_prose()
                    seen_structure = True
                continue
        if block is not None:
            block.append(ln)
        else:
            prose.append(ln)
    flush_prose(); flush_block()


def choose_canonical(variants, sections) -> "OrderedDict[str, Block]":
    """One Block per requirement id: home section by kind (else first occurrence), the longest
    body among the variants (they are near-identical repeats), citations unioned."""
    titles = [s.title for s in sections if not s.internal]
    canon = OrderedDict()
    for rid, vs in variants.items():
        pat = HOME_SECTION.get(rid.split("-")[0])
        home = None
        if pat:
            home = next((v.section for v in vs if re.search(pat, v.section, re.I)
                         and v.section in titles), None)
        if home is None:
            home = next((v.section for v in vs if v.section in titles), vs[0].section)
        best = max(vs, key=lambda v: len(v.body))
        cites = list(OrderedDict.fromkeys(c for v in vs for c in v.citations))
        canon[rid] = Block(rid, best.title, best.body, cites, section=home)
    return canon


# ── Citation verification ────────────────────────────────────────────────────

def load_source_index(run_dir: str | Path | None = None, source_root: str | Path | None = None) -> dict:
    """{relative path: line count} from a source root (preferred) or the run's .pages concats
    (numbered `[FILE: path ...]` dumps written by the paging phase). {} when neither exists."""
    idx: dict[str, int] = {}
    if source_root and Path(source_root).is_dir():
        root = Path(source_root)
        for p in root.rglob("*"):
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts):
                try:
                    idx[p.relative_to(root).as_posix()] = len(p.read_bytes().splitlines())
                except OSError:
                    continue
        if idx:
            return idx
    if run_dir:
        for concat in sorted(Path(run_dir).glob(".pages/*/concat.txt")):
            cur, last_empty = None, False
            for ln in concat.read_text(encoding="utf-8", errors="replace").split("\n"):
                mf = re.match(r"^\[FILE: (.+?) KIND: ", ln)
                if mf:
                    cur, last_empty = mf.group(1), False
                    idx.setdefault(cur, 0)
                    continue
                if ln.startswith("[END FILE:"):
                    # the pager numbers a phantom empty line after the file's final newline
                    if cur and last_empty and idx[cur] > 0:
                        idx[cur] -= 1
                    cur = None
                    continue
                if cur:
                    mn = re.match(r"^\s*(\d+) \|(.*)$", ln)
                    if mn and int(mn.group(1)) >= idx[cur]:
                        idx[cur] = int(mn.group(1))
                        last_empty = not mn.group(2).strip()
    return idx


def resolve_citation(cite: tuple, index: dict):
    """Return the citation with its path normalized to a real source file, or None if the file
    is unknown/ambiguous or the line range is outside the file."""
    path, a, b = cite
    path = path.strip().removeprefix("./")
    if path in index:
        real = path
    else:
        cands = [p for p in index if p.endswith("/" + path) or p == path]
        if len(cands) != 1:
            return None
        real = cands[0]
    n = index[real]
    if b == n + 1 and a <= n:
        b = n                      # range ends on the trailing newline ("whole file"): clamp
    if a < 1 or b > n:
        return None
    return (real, a, b)


def verify_citations(canon, index: dict) -> dict:
    """Keep only citations that resolve (when an index exists). Returns a report."""
    rep = {"verified_against_source": bool(index), "requirements": len(canon),
           "citations_in": 0, "citations_kept": 0, "citations_dropped": [],
           "requirements_with_citation": 0, "requirements_without_citation": []}
    for b in canon.values():
        rep["citations_in"] += len(b.citations)
        if index:
            kept = []
            for c in b.citations:
                r = resolve_citation(c, index)
                if r is None:
                    rep["citations_dropped"].append({"id": b.rid, "citation": format_citation(*c)})
                elif r not in kept:
                    kept.append(r)
            b.citations = kept
        rep["citations_kept"] += len(b.citations)
        if b.citations:
            rep["requirements_with_citation"] += 1
        else:
            rep["requirements_without_citation"].append(b.rid)
    return rep


# ── Customer clean-up (deterministic) ────────────────────────────────────────

_STILL_ALT = "|".join(STILL_NAMES)
JARGON_RE = re.compile(
    r"\bstills\b|\b(?:cross|per|multi)-still\b|\bstill-(?:specific|level|name|output)s?\b"
    rf"|\b(?:{_STILL_ALT}) still (?:found|identified|reported|extracted|surfaced|captured|output)\b"
    r"|\bStill (?:found|identified|reported|extracted)\b"
    r"|\bconvergen\w*|\bdivergence (?:point|count|score)s?\b|\bdivergen\w* (?:across|between) "
    r"|\bconfidence (?:score|level|map|rating|overview|qualifier|value)s?\b"
    r"|\b(?:high|low|medium|moderate|highest|lower)[- ]confidence\b|\bconfidence:\s*\d"
    r"|\bevidence diversity\b|\bbusiness relevance\s*:?\s*\d|\bmaster[- ]blend\w*\b"
    r"|\bthe blend\b|\bblend(?:ing)? (?:specification|step|phase|process)\b"
    r"|\bdistill\w*|\bbottl(?:ing|ed)\b|\bextraction (?:pass|step|phase|run)\w*"
    r"|\b(?:heads|hearts|tails)(?:/(?:heads|hearts|tails))+\b|\b(?:heads|hearts?|tails) cuts?\b"
    r"|\|\s*Stills?\s*\||\bknowledge graph\b|\bLLM\b|\banalysis pass(?:es)?\b|\bindependent(?:ly)? (?:analysis|pass)",
    re.I)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(*\"'`\[])")
_LABEL_RE = re.compile(r"^(\s*(?:[-*+]|\d+\.)?\s*(?:\*\*[^*]{1,60}\*\*:?|[A-Z][\w /&()-]{0,40}:)?\s*)")


def _clean_prose_line(ln: str) -> str | None:
    """Remove sentences with pipeline vocabulary; None if nothing of value remains."""
    if not JARGON_RE.search(ln):
        return ln
    if ln.lstrip().startswith("|"):
        return None                                   # table row about pipeline metrics
    lab = _LABEL_RE.match(ln).group(1)
    rest = ln[len(lab):]
    kept = [s for s in _SENT_SPLIT.split(rest) if not JARGON_RE.search(s)]
    if not kept:
        return None
    return lab + " ".join(kept)


def clean_heading(text: str) -> str:
    t = strip_pipeline_prefix(text)
    t = re.sub(r"\s*[—-]?\s*Master Blend( Specification)?", " — Business Specification", t, flags=re.I)
    t = re.sub(r"\bConfidence Map and Methodology\b", "Assurance Notes", t)
    return t.strip()


def clean_customer_text(text: str) -> str:
    """Deterministic clean-up for customer text: HTML comments, score lines, pipeline vocabulary,
    `still — ` heading prefixes, inline `file#Lx-Ly` refs normalized to `file:Lx-Ly`."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out = []
    for ln, fence in _fence_aware_lines(text):
        if fence:
            out.append(ln)
            continue
        if SCORE_LINE_RE.match(ln) or ln.strip() == "*No content to format.*":
            continue
        mh = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if mh:
            out.append(f"{mh.group(1)} {clean_heading(mh.group(2))}")
            continue
        ln = INLINE_REF_RE.sub(lambda m: format_citation(m.group(1).strip(), int(m.group(2)),
                                                         int(m.group(3) or m.group(2))), ln)
        c = _clean_prose_line(ln)
        if c is not None:
            out.append(c)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def dedupe_headings(spec: str) -> str:
    """Collapse a ## heading that repeats the previous ## heading with nothing but whitespace (or
    another identical heading) in between — the generated-intro duplicates."""
    out, last_h2, since = [], None, []
    for ln, fence in _fence_aware_lines(spec):
        if not fence and ln.startswith("## "):
            if last_h2 == ln.strip() and not any(s.strip() for s in since):
                since = []
                continue
            last_h2, since = ln.strip(), []
            out.append(ln)
            continue
        since.append(ln)
        out.append(ln)
    return "\n".join(out)


def jargon_hits(text: str) -> list[str]:
    """Lines outside code fences that still contain pipeline vocabulary (for reporting/tests)."""
    return [ln for ln, fence in _fence_aware_lines(text)
            if not fence and JARGON_RE.search(ln)]


# ── Model output handling ────────────────────────────────────────────────────

def _render_block_input(b: Block) -> str:
    return f"#### [{b.rid}] {b.title}\n{b.body}\n"


def parse_model_blocks(output: str) -> dict:
    """{rid: body} from model output; heading lines and Evidence lines inside bodies dropped."""
    parts = re.split(r"(?m)^#### \[([A-Z]{2,4}-\d+)\][^\n]*\n?", output)
    res = {}
    for i in range(1, len(parts) - 1, 2):
        rid, body = parts[i], parts[i + 1]
        keep = []
        for ln, fence in _fence_aware_lines(body):
            if not fence and (re.match(r"^#{1,6} ", ln) or EVIDENCE_LINE_RE.match(ln)):
                continue
            keep.append(ln)
        txt = "\n".join(keep).strip()
        if txt and rid not in res:
            res[rid] = txt
    return res


class _Agg:
    """Token totals across calls."""

    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.calls = 0
        self.lock = threading.Lock()

    def add(self, r):
        with self.lock:
            self.input_tokens += r.input_tokens
            self.output_tokens += r.output_tokens
            self.cache_read_tokens += getattr(r, "cache_read_tokens", 0) or 0
            self.cache_write_tokens += getattr(r, "cache_write_tokens", 0) or 0
            self.calls += 1

    def as_dict(self):
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens}


def _llm(llm, model_id, system, user, max_tokens, phase, agg, label=""):
    """One model call with the system prompt marked for prompt caching (shared by every chunk
    call), retried on transient errors, logged ONCE to telemetry as still=bottling."""
    # Converse API shape: a text block followed by a cachePoint (not Anthropic's cache_control).
    blocks = [{"text": system}, {"cachePoint": {"type": "default"}}]
    for attempt in range(5):
        t0 = time.time()
        try:
            r = bedrock_client.call_llm_blocks(llm, model_id, blocks, user, max_tokens=max_tokens)
            break
        except Exception as e:
            err = str(type(e).__name__).lower()
            if "credential" in err or "validation" in err or attempt == 4:
                bedrock_client.log_llm_call(status="error", model_id=model_id, still="bottling",
                                            phase=phase, error_code=type(e).__name__)
                raise
            print(f"    {label} retry {attempt+1}/5: {type(e).__name__}")
            time.sleep(15 * (attempt + 1))
    bedrock_client.log_llm_call(
        status="success", input_tokens=r.input_tokens, output_tokens=r.output_tokens,
        latency_ms=int((time.time() - t0) * 1000), model_id=model_id, still="bottling",
        phase=phase, cache_read_tokens=getattr(r, "cache_read_tokens", 0) or 0,
        cache_write_tokens=getattr(r, "cache_write_tokens", 0) or 0)
    agg.add(r)
    return r


def _bottle_batch(llm, model_id, system, target, section, blocks, i, n, agg, depth=0) -> dict:
    """Bottle one batch of blocks -> {rid: text}. Halve on max_tokens; fail closed at depth."""
    user = (f"TARGET SYSTEM: {target or 'Unknown'}\n\n"
            + CHUNK_PROMPT.format(k=len(blocks), i=i, n=n, section=section)
            + "\n\nREQUIREMENT BLOCKS:\n\n" + "\n".join(_render_block_input(b) for b in blocks))
    r = _llm(llm, model_id, system, user, CHUNK_MAX_TOKENS, "chunk", agg, f"chunk {i}/{n}")
    if r.stop_reason == "max_tokens":
        size = sum(len(b.body) for b in blocks)
        if depth >= MAX_SPLIT_DEPTH or len(blocks) < 2 or size < MIN_SPLIT_CHARS:
            raise RuntimeError(f"bottling chunk {i}/{n} ({section}) truncated at max_tokens "
                               f"even after {depth} splits")
        print(f"    chunk {i}/{n} hit max_tokens — splitting (depth {depth+1})")
        h = len(blocks) // 2
        out = _bottle_batch(llm, model_id, system, target, section, blocks[:h], i, n, agg, depth + 1)
        out.update(_bottle_batch(llm, model_id, system, target, section, blocks[h:], i, n, agg, depth + 1))
        return out
    return parse_model_blocks(r.output_text)


def _batches(canon, sections, limit=CHUNK_CHARS) -> list:
    """[(section, [Block])] in spec order; never crosses a home section; packed to <= limit chars."""
    out = []
    for sec in sections:
        cur, size = [], 0
        for b in (b for b in canon.values() if b.section == sec.title):
            ln = len(b.body) + len(b.title) + 20
            if cur and size + ln > limit:
                out.append((sec.title, cur))
                cur, size = [], 0
            cur.append(b)
            size += ln
        if cur:
            out.append((sec.title, cur))
    return out


def bottle_blocks(llm, model_id, system, target, canon, sections, agg) -> dict:
    """Bottle every canonical block. Returns stats; fills Block.text."""
    batches = _batches(canon, sections)
    n = len(batches)
    results: dict = {}
    jobs = [(sec, blks, i + 1) for i, (sec, blks) in enumerate(batches)]
    if jobs:
        # Warm the prompt cache with one call, then fan out (concurrent first calls would all write).
        sec, blks, i = jobs[0]
        results.update(_bottle_batch(llm, model_id, system, target, sec, blks, i, n, agg))
    with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as ex:
        futs = [ex.submit(_bottle_batch, llm, model_id, system, target, sec, blks, i, n, agg)
                for sec, blks, i in jobs[1:]]
        for f in futs:
            results.update(f.result())               # any failure fails bottling (fail closed)
    missing = [b for rid, b in canon.items() if rid not in results]
    retried = len(missing)
    if missing:
        print(f"    {len(missing)} requirement(s) missing from model output — retrying once")
        for j in range(0, len(missing), 20):
            try:
                results.update(_bottle_batch(llm, model_id, system, target, "retry",
                                             missing[j:j + 20], 1, 1, agg))
            except RuntimeError:
                pass
    fallback = []
    for rid, b in canon.items():
        txt = results.get(rid)
        if not txt:
            fallback.append(rid)
            txt = b.body
        b.text = clean_customer_text(txt)
    return {"batches": n, "retried": retried, "fallback_to_source_text": fallback}


def parse_overviews(output: str) -> dict:
    res = {}
    for m in re.finditer(r"^===\s*(.+?)\s*===\s*\n(.*?)(?=^===|\Z)", output, flags=re.M | re.S):
        res[m.group(1).strip().lower()] = m.group(2).strip()
    return res


# ── Assembly ─────────────────────────────────────────────────────────────────

def system_name(doc_title: str, target_name: str) -> str:
    if target_name:
        return target_name
    return re.split(r"\s+—\s+", doc_title)[0].strip() or "System"


def build_index(canon) -> list[str]:
    rows = ["| ID | Requirement | Section | Evidence |", "|---|---|---|---|"]
    for b in canon.values():
        cite = format_citation(*b.citations[0]) if b.citations else "—"
        if len(b.citations) > 1:
            cite += f" (+{len(b.citations) - 1})"
        title = clean_heading(b.title).replace("|", "/")
        rows.append(f"| {b.rid} | {title} | {b.section} | {cite} |")
    return rows


def kind_counts(canon) -> "OrderedDict[str, int]":
    c = Counter(rid.split("-")[0] for rid in canon)
    out = OrderedDict((KIND_NAMES[k], c[k]) for k in KIND_NAMES if c.get(k))
    for k, v in c.items():
        if k not in KIND_NAMES:
            out[k] = v
    return out


def assemble_spec(system: str, sections, canon, overviews: dict) -> str:
    """Deterministic assembly of the customer spec from parsed structure + bottled blocks."""
    n_cited = sum(1 for b in canon.values() if b.citations)
    out = [f"# {system} — Business Specification", "",
           f"This document specifies the business behavior of {system}, recovered from its source "
           f"code. It contains {len(canon)} requirements; each has a stable identifier and an "
           f"`Evidence:` line citing the source file and line range(s) that implement it "
           f"({n_cited} of {len(canon)} cited). A requirement that applies to several areas is "
           f"specified once, in its home section, and cross-referenced elsewhere.", "",
           "## Requirements Index", ""]
    out += build_index(canon)
    out += ["", "---", ""]
    emitted, prose_seen = set(), set()
    for sec in sections:
        if sec.internal:
            continue
        out += [f"## {sec.title}", ""]
        ov = overviews.get(sec.title.lower()) or ""
        ov = clean_customer_text(ov) if ov else ""
        if ov:
            out += [ov, ""]
        items = list(sec.items)
        body: list[str] = []
        for kind, val in items:
            if kind in ("h3", "h4"):
                body += [("### " if kind == "h3" else "#### ") + val, ""]
            elif kind == "prose":
                key = val.strip()
                if key in prose_seen:
                    continue
                prose_seen.add(key)
                c = clean_customer_text(val.replace("This document", "This part"))
                if c:
                    body += [c, ""]
            elif kind == "block":
                b = canon[val]
                if b.section == sec.title and val not in emitted:
                    emitted.add(val)
                    ev = ", ".join(format_citation(*c) for c in b.citations) or "not traced to source"
                    body += [f"#### [{b.rid}] {clean_heading(b.title)}", "", b.text, "",
                             f"Evidence: {ev}", ""]
                else:
                    body += [f"- See [{b.rid}] {clean_heading(b.title)} (specified in {b.section}).", ""]
        out += _drop_empty_headings(body)
        if not any(k == "block" for k, _ in items) and not ov:
            out += ["No requirements were identified for this section.", ""]
        out += ["---", ""]
    spec = "\n".join(out)
    spec = re.sub(r"(- See [^\n]+)\n\n(?=- See )", r"\1\n", spec)   # compact xref lists
    spec = re.sub(r"\n{3,}", "\n\n", spec)
    return dedupe_headings(spec).rstrip() + "\n"


def _drop_empty_headings(lines: list[str]) -> list[str]:
    """Drop ###/#### group headings (not requirement headings) with nothing under them."""
    out = []
    for idx, ln in enumerate(lines):
        if re.match(r"^#{3,4} (?!\[)", ln):
            nxt = next((x for x in lines[idx + 1:] if x.strip()), "")
            lvl = len(ln) - len(ln.lstrip("#"))
            if not nxt or (re.match(r"^#{1,6} ", nxt) and not nxt.startswith("#### [")
                           and len(nxt) - len(nxt.lstrip("#")) <= lvl):
                continue
        out.append(ln)
    return out


def check_no_loss(blend_ids, spec: str) -> dict:
    """Every requirement id in the blend has a `#### [ID]` block in the spec."""
    spec_ids = set(re.findall(r"(?m)^#### \[([A-Z]{2,4}-\d+)\]", spec))
    missing = [r for r in blend_ids if r not in spec_ids]
    return {"blend_ids": len(set(blend_ids)), "spec_ids": len(spec_ids), "missing": missing}


def build_readme_input(system: str, sections, canon, overviews: dict, cite_rep: dict) -> str:
    """Small README input: overviews + index titles + counts. Never the whole blend."""
    lines = [f"SYSTEM: {system}", "", "COUNTS:"]
    lines += [f"- {k}: {v}" for k, v in kind_counts(canon).items()]
    lines += [f"- Total requirements: {len(canon)}",
              f"- Requirements with source citations: {cite_rep.get('requirements_with_citation', 0)}",
              "", "SECTION OVERVIEWS:"]
    for s in sections:
        if not s.internal:
            lines += [f"=== {s.title} ===", overviews.get(s.title.lower(), "") or "(none)"]
    lines += ["", "REQUIREMENTS INDEX:"]
    lines += [f"[{b.rid}] {clean_heading(b.title)}" for b in canon.values()]
    tbd = [b.rid for b in canon.values() if "TBD" in b.text]
    lines += ["", f"REQUIREMENTS MARKED TBD: {', '.join(tbd) or 'none'}"]
    return "\n".join(lines)


def assemble_readme(system: str, narrative: str, canon, cite_rep: dict, spec_kb: float) -> str:
    out = [f"# {system} — Specification Summary", "", "## Key Metrics", "",
           "| Requirement type | Count |", "|---|---:|"]
    out += [f"| {k} | {v} |" for k, v in kind_counts(canon).items()]
    out += [f"| **Total** | **{len(canon)}** |", "",
            "## Traceability", "",
            f"- {cite_rep.get('requirements_with_citation', 0)} of {len(canon)} requirements cite "
            f"source lines ({cite_rep.get('citations_kept', 0)} citations"
            + (", each checked against the source files" if cite_rep.get("verified_against_source")
               else ", not checked against source") + ").",
            f"- Full specification: `bottled_spec.md` ({spec_kb:.0f} KB).", ""]
    out.append(clean_customer_text(narrative).strip())
    return "\n".join(out).rstrip() + "\n"


# ── Core Logic ───────────────────────────────────────────────────────────────


def run_bottling(
    master_blend_dir: str,
    output_dir: str,
    target_name: str = "",
    profile: str = "",
    region: str = "us-east-1",
    model_id: str = "us.anthropic.claude-sonnet-4-6",
    source_root: str = "",
    only_sections: list | None = None,
) -> dict:
    """Transform the master blend into a customer-facing bottled specification.

    Args:
        master_blend_dir: Path to master_blend/ directory (its parent is the run dir whose
            .pages/ are used to verify citations when source_root is not given).
        output_dir: Path to write bottled_spec/ outputs.
        target_name: Display name for the system.
        source_root: Optional source tree to verify citations against.
        only_sections: Optional list of section-title substrings to bottle (scratch checks).
    """
    blend_path = Path(master_blend_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    blend_file = blend_path / "master_blend.md"
    if not blend_file.exists():
        md_files = sorted(blend_path.glob("*.md"))
        if not md_files:
            return {"error": f"No master blend found in {master_blend_dir}"}
        blend_content = "\n\n".join(f.read_text(encoding="utf-8") for f in md_files)
    else:
        blend_content = blend_file.read_text(encoding="utf-8")

    print(f"[bottling] Master blend: {len(blend_content)/1024:.0f} KB")
    print(f"[bottling] Output: {output_path}")

    doc_title, sections, variants = parse_blend(blend_content)
    if only_sections:
        keep = [s for s in sections if any(o.lower() in s.title.lower() for o in only_sections)]
        keep_titles = {s.title for s in keep}
        variants = OrderedDict((k, [v for v in vs if v.section in keep_titles])
                               for k, vs in variants.items())
        variants = OrderedDict((k, vs) for k, vs in variants.items() if vs)
        sections = keep
    system = system_name(doc_title, target_name)
    canon = choose_canonical(variants, sections)
    index = load_source_index(blend_path.parent, source_root or None)
    cite_rep = verify_citations(canon, index)
    print(f"  {len(canon)} requirements ({sum(len(v) for v in variants.values())} blocks in the blend); "
          f"citations kept {cite_rep['citations_kept']}/{cite_rep['citations_in']} "
          f"(verified={cite_rep['verified_against_source']})")

    system_prompt = _load_bottling_prompt()
    llm = bedrock_client.create_llm_client(profile, region)
    agg_chunks, agg_ov, agg_readme = _Agg(), _Agg(), _Agg()

    # Call group 1: requirement batches.
    t0 = time.time()
    bstats = bottle_blocks(llm, model_id, system_prompt, system, canon, sections, agg_chunks)
    spec_time = time.time() - t0
    print(f"    Blocks done ({spec_time:.0f}s, {agg_chunks.input_tokens}→{agg_chunks.output_tokens} tokens, "
          f"{agg_chunks.calls} calls, cache read {agg_chunks.cache_read_tokens})")

    # Call 2: section overviews from the blend's per-section intros (small).
    t1 = time.time()
    ov_in = "\n\n".join(f"=== {s.title} ===\n{s.intro or '(no introduction)'}"
                        for s in sections if not s.internal)
    overviews = {}
    if ov_in:
        r = _llm(llm, model_id, OVERVIEW_PROMPT.format(system=system), ov_in,
                 OVERVIEW_MAX_TOKENS, "overview", agg_ov, "overviews")
        overviews = parse_overviews(r.output_text)

    spec_text = assemble_spec(system, sections, canon, overviews)
    loss = check_no_loss(list(variants.keys()), spec_text)
    if loss["missing"]:
        raise RuntimeError(f"bottling lost requirements: {loss['missing'][:20]}")
    spec_path = output_path / "bottled_spec.md"
    spec_path.write_text(spec_text, encoding="utf-8")

    # Call 3: README narrative from overviews + index + counts (never the whole blend).
    readme_in = build_readme_input(system, sections, canon, overviews, cite_rep)
    r = _llm(llm, model_id, README_PROMPT.format(system=system), readme_in,
             README_MAX_TOKENS, "readme", agg_readme, "readme")
    readme_path = output_path / "README.md"
    readme_path.write_text(assemble_readme(system, r.output_text, canon, cite_rep,
                                           len(spec_text.encode("utf-8")) / 1024), encoding="utf-8")
    readme_time = time.time() - t1

    retention = heading_retention(blend_content, spec_text)
    hits = jargon_hits(spec_text) + jargon_hits(readme_path.read_text(encoding="utf-8"))
    report = {
        "requirements": len(canon),
        "blend_blocks": sum(len(v) for v in variants.values()),
        "no_loss": loss, "citations": cite_rep, "blocks": bstats,
        "jargon_lines_remaining": len(hits), "jargon_samples": hits[:10],
        "omitted_internal_sections": [s.title for s in sections if s.internal],
        "readme_input_chars": len(readme_in), "blend_chars": len(blend_content),
        "calls": {"chunk": agg_chunks.as_dict(), "overview": agg_ov.as_dict(),
                  "readme": agg_readme.as_dict()},
    }
    (output_path / "bottling_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"  Heading retention: {retention}; requirements {loss['spec_ids']}/{loss['blend_ids']}; "
          f"jargon lines left {len(hits)}")
    print(f"  Written: {readme_path.name} ({readme_path.stat().st_size/1024:.1f} KB)")
    print(f"  Written: {spec_path.name} ({spec_path.stat().st_size/1024:.1f} KB)")

    tot = [agg_chunks, agg_ov, agg_readme]
    return {
        "readme_size_kb": round(readme_path.stat().st_size / 1024, 1),
        "spec_size_kb": round(spec_path.stat().st_size / 1024, 1),
        "input_tokens": sum(a.input_tokens for a in tot),
        "output_tokens": sum(a.output_tokens for a in tot),
        "cache_read_tokens": sum(a.cache_read_tokens for a in tot),
        "cache_write_tokens": sum(a.cache_write_tokens for a in tot),
        "readme_time_s": round(readme_time, 1),
        "spec_time_s": round(spec_time, 1),
        "total_time_s": round(readme_time + spec_time, 1),
        "spec_chunks": bstats["batches"],
        "spec_calls": agg_chunks.calls,
        "blend_size_kb": round(len(blend_content.encode("utf-8")) / 1024, 1),
        "heading_retention": retention,
        "requirements": len(canon),
        "citations_kept": cite_rep["citations_kept"],
        "requirements_with_citation": cite_rep["requirements_with_citation"],
    }


# ── Section-bounded splitting (shared helpers) ───────────────────────────────

def _split_at(text: str, pattern: str) -> list[str]:
    """Split text before every line matching pattern (the matching line starts a piece)."""
    idx = [m.start() for m in re.finditer(pattern, text, flags=re.M)]
    if not idx or idx[0] != 0:
        idx = [0] + idx
    return [text[a:b] for a, b in zip(idx, idx[1:] + [len(text)]) if text[a:b]]


def _pack(pieces: list[str], limit: int, finer: list[str]) -> list[str]:
    """Greedily pack consecutive pieces into chunks <= limit; split an oversized piece on the
    next finer boundary (### -> #### -> blank line). Lossless: ''.join(result) == ''.join(pieces)."""
    out, cur = [], ""
    for p in pieces:
        if len(p) > limit and finer:
            sub = _split_at(p, finer[0])
            parts = _pack(sub, limit, finer[1:]) if len(sub) > 1 else _pack([p], limit, finer[1:])
        else:
            parts = [p]
        for q in parts:
            if cur and len(cur) + len(q) > limit:
                out.append(cur)
                cur = ""
            cur += q
    if cur:
        out.append(cur)
    return out


def _split_blend(blend: str, limit: int = CHUNK_CHARS) -> list[tuple[str, str]]:
    """Split the blend into ordered (section_title, text) chunks. Never crosses a ## section
    boundary; a section larger than limit is split at ###, then ####, then paragraph
    boundaries. Lossless: the chunk texts concatenate back to the blend exactly."""
    chunks = []
    for sec in _split_at(blend, r"^## "):
        m = re.match(r"## (.+)", sec)
        title = m.group(1).strip() if m else "(document header)"
        for piece in _pack([sec], limit, [r"^### ", r"^#### ", r"^\s*$"]):
            chunks.append((title, piece))
    return chunks


def _restore_section_heading(chunk_text: str, output: str) -> str:
    """A chunk that opens a ## section must open with that exact heading in the output (the
    model sometimes demotes or rewrites it). Replace a leading heading line naming the section
    at any level, or prepend the original heading. (Section headings are now emitted
    deterministically by assemble_spec; kept for callers that bottle raw excerpts.)"""
    first = chunk_text.lstrip("\n").split("\n", 1)[0]
    if not first.startswith("## "):
        return output
    title = first[3:].strip()
    lines = output.lstrip("\n").split("\n")
    m = re.match(r"#{1,6}\s+(.*)$", lines[0]) if lines else None
    if m and m.group(1).strip().lower() == title.lower():
        lines = lines[1:]
    return first + "\n" + "\n".join(lines)


def heading_retention(blend: str, spec: str) -> dict:
    """### heading counts in vs out (informational; the no-loss contract is check_no_loss)."""
    def count(t):
        return len(re.findall(r"^### ", t, flags=re.M))
    b, s = count(blend), count(spec)
    return {"blend_h3": b, "spec_h3": s, "ratio": round(s / b, 3) if b else None}


# ── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    src_root = ""
    only = None
    if "--source-root" in args:
        i = args.index("--source-root")
        src_root = args[i + 1]
        del args[i:i + 2]
    if "--only-sections" in args:
        i = args.index("--only-sections")
        only = [s for s in args[i + 1].split(",") if s]
        del args[i:i + 2]
    if len(args) < 2:
        print("Usage: python3 bottling.py <master_blend_dir> <output_dir> [target_name] "
              "[--source-root DIR] [--only-sections A,B]")
        sys.exit(1)
    result = run_bottling(args[0], args[1], args[2] if len(args) > 2 else "",
                          source_root=src_root, only_sections=only)
    print(f"\nResult: {json.dumps(result, indent=2)}")
