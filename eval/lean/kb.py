# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. SPDX-License-Identifier: MIT-0
"""Lean pipeline v1: read-only knowledge base over a run directory, shared by ask.py and impact.py.

Loads findings.json (spec findings with citations), graph.json (symbol/table dependency graph),
facts.json and glossary.md, and offers:
  - search(text): BM25 over finding statements (+ glossary expansion of business terms)
  - match_nodes(text): graph nodes whose identifiers match the words of the text
  - units_at(file, line) / findings_for_span(file, a, b): map between citations and graph units
  - node_summary(id): one-line description of a node and its direct edges
  - snippet(file, a, b): line-numbered source excerpt (only on demand; never the whole corpus)
No LLM calls here.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from specfmt import CitationIndex

STOP = set("""a an the of to in on for and or is are be by with as at from that this it its into than then when if
else any all each per via which who what whom whose was were has have had not no can may will shall should must
does do done how why where there their they them we you i me my our your so such also only just about which
happen happens happened system code""".split())


def words(s: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "")
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)          # 30,000 -> 30000
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z]+|\d+(?:\.\d+)?", s.replace("_", " ")):
        w = w.lower()
        if w in STOP or len(w) < 2:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(w) > 4 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.append(w[:6] if not w[0].isdigit() else w)   # crude prefix stem: approve/approval -> approv
    return out


class KB:
    def __init__(self, run: Path):
        self.run = run
        self.src = run / "source"
        self.facts = json.loads((run / "facts.json").read_text())
        self.graph = json.loads((run / "graph.json").read_text())
        self.findings = json.loads((run / "findings.json").read_text())["findings"]
        for f in self.findings:
            f["citations"] = [tuple(c) for c in f["citations"]]
        self.files = [x["file"] for x in self.facts["files"]]
        self.ci = CitationIndex(self.src, self.files)
        self.nodes = {n["id"]: n for n in self.graph["nodes"]}
        self.out_e, self.in_e = defaultdict(list), defaultdict(list)
        for e in self.graph["edges"]:
            self.out_e[e["src"]].append(e)
            self.in_e[e["dst"]].append(e)
        self.units = [n for n in self.graph["nodes"] if n.get("file") and n.get("start") and
                      n["kind"] not in ("file", "column", "vb-type", "table", "ui-event", "constant")]
        self.glossary = self._glossary()
        self._index()

    # ------------------------------------------------------------------ glossary
    def _glossary(self) -> list[dict]:
        p = self.run / "glossary.md"
        rows = []
        if not p.exists():
            return rows
        for line in p.read_text().splitlines():
            if not line.strip().startswith("|") or re.match(r"^\s*\|\s*:?-", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3 or cells[0].lower() in ("term", "#"):
                continue
            rows.append({"term": cells[0].strip("*` "), "meaning": cells[1], "identifiers": cells[2],
                         "citations": cells[3] if len(cells) > 3 else ""})
        return rows

    def glossary_hits(self, text: str) -> list[dict]:
        q = set(words(text))
        hits = []
        for g in self.glossary:
            tw = set(words(g["term"]))
            # also the meaning's lead phrase ("Project Manager; approval limit ..." -> project manager)
            lead = set(words(re.split(r"[;.(,:]", g["meaning"])[0])) if len(g["meaning"]) else set()
            if tw and (tw <= q or len(tw & q) >= max(1, len(tw) - 1) and len(tw) > 1) or \
                    lead and len(lead) <= 3 and lead <= q:
                hits.append(g)
        return hits

    # ------------------------------------------------------------------ BM25 over findings
    def _doc(self, f) -> list[str]:
        files = " ".join(c[0].split("/")[-1] for c in f["citations"])
        return words(" ".join([f.get("statement", ""), f.get("section", ""), f.get("flag_note", ""),
                               " ".join(f.get("flags", [])), files]))

    def _index(self):
        self.docs = [self._doc(f) for f in self.findings]
        self.df = Counter(w for d in self.docs for w in set(d))
        self.avg = sum(map(len, self.docs)) / max(1, len(self.docs))

    def search(self, text: str, k: int = 20, expand: bool = True) -> list[tuple[float, dict]]:
        q = Counter(words(text))
        if expand:
            for g in self.glossary_hits(text):
                for w in words(g["identifiers"] + " " + g["meaning"]):
                    q[w] += 0.5
        N = len(self.docs)
        scored = []
        for f, d in zip(self.findings, self.docs):
            tf = Counter(d)
            s = 0.0
            for w, qw in q.items():
                if w not in tf:
                    continue
                idf = math.log(1 + (N - self.df[w] + 0.5) / (self.df[w] + 0.5))
                s += qw * idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(d) / self.avg))
            if s > 0:
                if f.get("origin") in ("layer3", "layer5"):
                    s *= 1.1
                scored.append((s, f))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]

    # ------------------------------------------------------------------ graph helpers
    def match_nodes(self, text: str, kinds: set[str] | None = None, limit: int = 40) -> list[dict]:
        q = set(words(text))
        for g in self.glossary_hits(text):
            q |= {w for w in words(g["identifiers"])}
        out = []
        for n in self.graph["nodes"]:
            if kinds and n["kind"] not in kinds:
                continue
            nw = set(words(n["name"].split(":")[-1]))
            if not nw:
                continue
            ov = len(nw & q)
            if ov:
                out.append((ov / len(nw) + 0.1 * ov, n))
        out.sort(key=lambda x: -x[0])
        return [n for _, n in out[:limit]]

    def units_at(self, file: str, a: int, b: int | None = None) -> list[dict]:
        b = b or a
        hits = [n for n in self.units if n["file"] == file and n["start"] <= b and n["end"] >= a]
        hits.sort(key=lambda n: n["end"] - n["start"])
        return hits

    def findings_for_span(self, file: str, a: int, b: int) -> list[dict]:
        return [f for f in self.findings if any(c[0] == file and c[1] <= b and c[2] >= a for c in f["citations"])]

    def node_summary(self, nid: str) -> str:
        n = self.nodes.get(nid)
        if not n:
            return nid
        loc = f" [{self.ci.fmt(n['file'], n['start'], n['end'])}]" if n.get("file") and n.get("start") else ""
        parts = [f"{n['kind']} {n['name']}{loc}"]
        outs = defaultdict(set)
        for e in self.out_e.get(nid, []):
            if e["kind"] not in ("contains", "has_column", "member"):
                outs[e["kind"]].add(e["dst"].split(":", 1)[1])
        ins = defaultdict(set)
        for e in self.in_e.get(nid, []):
            if e["kind"] not in ("contains", "has_column", "member"):
                ins[e["kind"]].add(e["src"].split(":", 1)[1])
        for k, v in outs.items():
            parts.append(f"{k} -> {', '.join(sorted(v)[:8])}")
        for k, v in ins.items():
            parts.append(f"<- {k} by {', '.join(sorted(v)[:8])}")
        return "; ".join(parts)

    def snippet(self, file: str, a: int, b: int, pad: int = 2, cap: int = 40) -> str:
        ls = (self.src / file).read_text(errors="replace").splitlines()
        a, b = max(1, a - pad), min(len(ls), b + pad)
        b = min(b, a + cap - 1)
        return "\n".join(f"{i:4d} | {ls[i - 1]}" for i in range(a, b + 1))

    def finding_line(self, f: dict, n: int = 400) -> str:
        cits = "; ".join(self.ci.fmt(*c) or "" for c in f["citations"][:5])
        return (f"{f['id']} [{f.get('section', '')}{' / ' + f['file'].split('/')[-1] if f.get('file') else ''}] "
                f"{re.sub(r'\s+', ' ', f['statement'])[:n]} {{{cits}}} ({f['evidence']}"
                f"{'; ' + ', '.join(f['flags']) if f.get('flags') else ''})")
