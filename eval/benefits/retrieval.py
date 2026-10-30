# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure-Python BM25 retrieval over a spec, for the ``--adapter spec`` answerers.

Why this exists: the benefit evaluators used to send the first ``--max-spec-chars``
characters of the spec and drop the rest. On the JC business spec that was 150,000 of
556,000 characters, so every Q&A, impact and field-app answer was written from the
first quarter of the document, whatever the question was about.

Instead the spec is split into chunks along its own structure (one chunk per markdown
heading, i.e. per requirement in a bottled spec; long sections such as the requirements
index are split further at line boundaries), every chunk is scored against the question
with Okapi BM25, and the best chunks are packed into the character budget. Chunks are
then shown in document order, each under a header naming its heading path and spec
lines, and the caller records exactly which chunks were used.

No third-party dependency, deterministic (ties broken by document order), and cheap:
the JC business spec (556 KB) chunks and indexes in well under a second.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?")
CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

STOP = set("""a an the of to in on for and or is are be by with as at from that this it its into
than then when if else any all each per via which who whom whose was were has have had not no
only also can may will shall should must does do done what where how why there their they them
""".split())

DEFAULT_CHUNK_CHARS = 4000


def stem(w: str) -> str:
    """Tiny plural folding ("invoices" -> "invoice", "entries" -> "entry"); no dependency."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokenize(text: str) -> list[str]:
    """Lower-cased, plural-folded words, plus the parts of camelCase / PascalCase /
    snake_case identifiers, so ``ExpFlag`` matches a question about the "exp flag"."""
    out: list[str] = []
    for w in WORD_RE.findall(text):
        lw = w.lower()
        if lw not in STOP and len(lw) > 1:
            out.append(stem(lw))
        if not w[0].isdigit():
            parts = [p for seg in w.split("_") for p in CAMEL_RE.findall(seg)]
            if len(parts) > 1:
                out.extend(stem(p.lower()) for p in parts if len(p) > 1 and p.lower() not in STOP)
    return out


@dataclass
class Chunk:
    id: str
    heading: str            # heading path, "A > B > C"
    start_line: int         # 1-based, inclusive, in the spec file
    end_line: int
    text: str
    tokens: list[str] = field(default_factory=list, repr=False)

    @property
    def chars(self) -> int:
        return len(self.text)

    def info(self, score: float | None = None) -> dict:
        d = {"id": self.id, "heading": self.heading, "spec_lines": [self.start_line, self.end_line],
             "chars": self.chars}
        if score is not None:
            d["score"] = round(score, 4)
        return d


def chunk_spec(text: str, max_chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[Chunk]:
    """Split at every markdown heading; split any section longer than ``max_chunk_chars``
    at line boundaries (a single over-long line stays whole)."""
    lines = text.split("\n")
    sections: list[tuple[str, int, list[str]]] = []
    path: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_start = 1
    cur_heading = ""
    for i, line in enumerate(lines, 1):
        m = HEADING_RE.match(line)
        if m:
            if any(x.strip() for x in cur):
                sections.append((cur_heading, cur_start, cur))
            level = len(m.group(1))
            path = [(lv, t) for lv, t in path if lv < level] + [(level, m.group(2))]
            cur_heading = " > ".join(t for _, t in path)
            cur, cur_start = [line], i
        else:
            cur.append(line)
    if any(x.strip() for x in cur):
        sections.append((cur_heading, cur_start, cur))

    chunks: list[Chunk] = []
    for heading, start, sec in sections:
        piece: list[str] = []
        piece_start = start
        size = 0
        for j, line in enumerate(sec):
            if piece and size + len(line) + 1 > max_chunk_chars:
                chunks.append(Chunk(f"C{len(chunks) + 1}", heading, piece_start,
                                    piece_start + len(piece) - 1, "\n".join(piece)))
                piece, piece_start, size = [], start + j, 0
            piece.append(line)
            size += len(line) + 1
        if piece and any(x.strip() for x in piece):
            chunks.append(Chunk(f"C{len(chunks) + 1}", heading, piece_start,
                                piece_start + len(piece) - 1, "\n".join(piece)))
    for c in chunks:
        # the heading path is indexed too: a requirement's section name is part of its topic
        c.tokens = tokenize(c.heading + "\n" + c.text)
    return chunks


class BM25:
    """Okapi BM25 over pre-tokenised chunks (k1=1.5, b=0.75)."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.tf = [Counter(c.tokens) for c in chunks]
        self.len = [len(c.tokens) for c in chunks]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(chunks)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def scores(self, query: str) -> list[float]:
        q = Counter(tokenize(query))
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            norm = self.k1 * (1 - self.b + self.b * (ln / self.avg if self.avg else 0.0))
            for w, qn in q.items():
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + norm) * (1 + math.log(qn))
            out.append(s)
        return out


class SpecRetriever:
    """Chunk + index a spec once; select the most relevant chunks per query."""

    def __init__(self, text: str, max_chunk_chars: int = DEFAULT_CHUNK_CHARS):
        self.text = text
        self.chunks = chunk_spec(text, max_chunk_chars)
        self.index = BM25(self.chunks)

    def select(self, query: str, budget_chars: int) -> tuple[str, dict]:
        """Context text for ``query`` within ``budget_chars``, plus a record of what was used.

        If the whole spec fits, it is returned unchanged (``mode: full``). Otherwise chunks
        are taken best-first (ties: document order) while they fit; a chunk that does not
        fit is skipped and smaller ones are still tried. Zero-score chunks are never used,
        except when NO chunk matches the query at all: then the leading chunks are sent in
        document order and ``fallback`` says so.
        """
        if len(self.text) <= budget_chars:
            return self.text, {"mode": "full", "spec_chars": len(self.text),
                               "budget_chars": budget_chars, "chars": len(self.text),
                               "chunks_total": len(self.chunks),
                               "chunks_used": len(self.chunks), "chunks": [],
                               "spec_chars_shown_pct": 100.0}
        sc = self.index.scores(query)
        order = sorted(range(len(self.chunks)), key=lambda i: (-sc[i], i))
        picked: list[int] = []
        used = 0
        fallback = not any(s > 0 for s in sc)
        for i in (range(len(self.chunks)) if fallback else order):
            if not fallback and sc[i] <= 0:
                break
            cost = self.chunks[i].chars + len(self._header(self.chunks[i])) + 2
            if used + cost > budget_chars:
                if fallback:
                    break
                continue
            picked.append(i)
            used += cost
        picked.sort()
        parts = [self._header(self.chunks[i]) + "\n" + self.chunks[i].text for i in picked]
        body = "\n\n".join(parts)
        shown = sum(self.chunks[i].chars for i in picked)
        return body, {
            "mode": "retrieval", "method": "bm25", "spec_chars": len(self.text),
            # no chunk shares a term with the query: the document's opening chunks are sent
            "fallback": "no_term_match_leading_chunks" if fallback else None,
            "budget_chars": budget_chars, "chars": len(body),
            "chunks_total": len(self.chunks), "chunks_used": len(picked),
            "spec_chars_shown_pct": round(100.0 * shown / len(self.text), 1) if self.text else None,
            "chunks": [self.chunks[i].info(sc[i]) for i in picked],
        }

    @staticmethod
    def _header(c: Chunk) -> str:
        return f"[spec chunk {c.id} | {c.heading or '(preamble)'} | spec lines {c.start_line}-{c.end_line}]"
