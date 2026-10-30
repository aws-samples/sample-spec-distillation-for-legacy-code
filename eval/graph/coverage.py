# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Section 3 — concept coverage against an answer key, by location overlap only.

This is NOT a semantic recall number (score.py already measures that, with a judge). The question
here is narrower and entirely deterministic: for each key item, does the knowledge graph contain a
concept that *cites the lines the item lives on*? A key item with no concept pointing anywhere near
it is a hole in the graph independent of any wording judgement.

Per key item:
  full     one concept's citations overlap EVERY location of the item
  partial  some concept overlaps at least one location, but none covers all of them
  missing  no concept cites any of the item's locations   <- the flagged failure

Citation parsing and key loading are imported from eval/score.py so the two scorers agree on what
counts as a citation ("file.vb#L10-L20", "file.sql:10-20", "file (lines 10-20)", bare "L10" under a
file heading, basename fallback). score.py is NOT modified; a compact fallback parser is used if it
cannot be imported, and the report says which was used.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from .load import GraphBundle

MAX_EXAMPLES = 25
EVAL_DIR = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------- score.py bridge
def _score_module():
    if str(EVAL_DIR) not in sys.path:
        sys.path.insert(0, str(EVAL_DIR))
    import score  # noqa: F401  (eval/score.py — read-only use)
    return score


_FALLBACK_EXT = r"(?:vb|cs|sql|cob|cbl|cpy|txt|rpt|py|js|ts|java|rs|c|h|jcl)"
_FALLBACK_CIT = re.compile(
    r"(?P<file>[A-Za-z0-9_./\\-]+\." + _FALLBACK_EXT + r")`?\s*(?::|#|,)?\s*\(?\s*"
    r"(?:lines?\s+|ll?\.\s*)?L?(?P<a>\d+)(?:\s*(?:-|–|—|to|\.\.)\s*L?(?P<b>\d+))?\b", re.I)


def _fallback_citations(text: str, src) -> list[dict]:
    out = []
    for m in _FALLBACK_CIT.finditer(text or ""):
        a = int(m.group("a"))
        b = int(m.group("b") or a)
        rel, _why = src.resolve(m.group("file"))
        out.append({"raw": m.group(0).strip(), "file": m.group("file"), "resolved": rel,
                    "start": a, "end": b, "valid": rel is not None and 1 <= a <= b,
                    "reason": "ok" if rel else "file not found"})
    return out


# ----------------------------------------------------------------------------- key
def load_key(path: Path) -> dict:
    try:
        return _score_module().load_key(Path(path))
    except Exception:  # noqa: BLE001 — fall back to a local loader
        text = Path(path).read_text()
        if Path(path).suffix in (".yaml", ".yml"):
            import yaml  # type: ignore
            key = yaml.safe_load(text)
        else:
            key = json.loads(text)
        for it in key.get("items", []):
            if "citations" not in it and it.get("locations"):
                it["citations"] = [f"{loc['file']}:{loc['start_line']}-{loc.get('end_line', loc['start_line'])}"
                                   for loc in it["locations"]]
            it.setdefault("flags", [])
            it.setdefault("evidence", it.get("evidence_class"))
        return key


def _key_locations(item: dict) -> list[tuple[str, int, int]]:
    locs = []
    for loc in item.get("locations") or []:
        f = str(loc.get("file", ""))
        a = int(loc.get("start_line", 0) or 0)
        bb = int(loc.get("end_line", a) or a)
        if f and a:
            locs.append((f.split("/")[-1].casefold(), a, bb))
    if locs:
        return locs
    for c in item.get("citations") or []:
        m = re.match(r"(.+?):(\d+)(?:-(\d+))?$", str(c))
        if m:
            locs.append((m.group(1).split("/")[-1].casefold(), int(m.group(2)),
                         int(m.group(3) or m.group(2))))
    return locs


# ----------------------------------------------------------------------------- main
def concept_coverage(b: GraphBundle, key_path: str | Path) -> dict:
    key = load_key(Path(key_path))
    items = key.get("items") or []
    src = b.src

    parser = "score.py"
    try:
        score = _score_module()
        files = {rel: p for rel, p in src.files.items()}
        counts = {rel: src.line_count(rel) for rel in files}
        extra = score.literal_file_patterns(files)
        default_file = next(iter(files)) if len(files) == 1 else None

        def cits(text: str) -> list[dict]:
            return score.extract_citations({"text": text, "context_file": None},
                                           files, counts, default_file, extra)
    except Exception as exc:  # noqa: BLE001
        parser = f"built-in fallback ({type(exc).__name__}: {exc})"

        def cits(text: str) -> list[dict]:
            return _fallback_citations(text, src)

    # concept id -> set of (basename, start, end) it cites
    by_id = {n.get("id"): n for n in b.kg_nodes}
    concept_cits: dict[str, list[tuple[str, int, int]]] = {}
    total_cits = valid_cits = 0
    for cid, body in b.concept_bodies.items():
        if cid not in by_id:
            continue
        spans = []
        for c in cits(body):
            total_cits += 1
            if not c.get("valid") or not c.get("resolved"):
                continue
            valid_cits += 1
            spans.append((str(c["resolved"]).split("/")[-1].casefold(), int(c["start"]), int(c["end"])))
        concept_cits[cid] = spans

    # bucket citations by file for a cheap per-item scan
    by_file: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for cid, spans in concept_cits.items():
        for base, a, bb in spans:
            by_file[base].append((cid, a, bb))

    unified = {i for i, n in by_id.items() if n.get("node_type") == "unified_concept"}
    results = []
    for it in items:
        locs = _key_locations(it)
        per_loc = []
        cover: dict[str, set] = defaultdict(set)   # concept id -> indices of locations covered
        for li, (base, a, bb) in enumerate(locs):
            hits = [cid for cid, ca, cb in by_file.get(base, ()) if ca <= bb and a <= cb]
            per_loc.append({"file": base, "start": a, "end": bb, "concepts": len(hits)})
            for cid in hits:
                cover[cid].add(li)
        best = max(cover.items(), key=lambda kv: len(kv[1]), default=(None, set()))
        n_loc = len(locs)
        if not locs:
            degree = "no_locations"
        elif best[0] is not None and len(best[1]) == n_loc:
            degree = "full"
        elif cover:
            degree = "partial"
        else:
            degree = "missing"
        any_cov = len({li for s in cover.values() for li in s})
        results.append({
            "id": it.get("id"), "type": it.get("type"),
            "evidence": it.get("evidence") or it.get("evidence_class"),
            "flags": it.get("flags") or [],
            "locations": n_loc, "locations_covered": any_cov,
            "degree": degree,
            "concepts_touching": len(cover),
            "unified_concepts_touching": len([c for c in cover if c in unified]),
            "best_concept": best[0],
            "best_concept_title": (by_id.get(best[0]) or {}).get("title") if best[0] else None,
            "best_concept_locations": len(best[1]),
            "per_location": per_loc,
            "statement": " ".join(str(it.get("statement", "")).split())[:240],
        })

    def tally(rows) -> dict:
        n = len(rows)
        f = sum(1 for r in rows if r["degree"] == "full")
        p = sum(1 for r in rows if r["degree"] == "partial")
        m = sum(1 for r in rows if r["degree"] == "missing")
        pct = lambda a: round(100.0 * a / n, 2) if n else None  # noqa: E731
        return {"items": n, "full": f, "partial": p, "missing": m,
                "no_locations": n - f - p - m,
                "full_pct": pct(f), "any_pct": pct(f + p),
                "partial_credit_pct": round(100.0 * (f + 0.5 * p) / n, 2) if n else None}

    def breakdown(keyfn) -> dict:
        groups: dict[str, list] = defaultdict(list)
        for r in results:
            for g in keyfn(r):
                groups[str(g)].append(r)
        return {g: tally(rs) for g, rs in sorted(groups.items())}

    # location-level recall: share of all key locations that some concept cites
    all_loc = sum(r["locations"] for r in results)
    cov_loc = sum(r["locations_covered"] for r in results)

    return {
        "status": "ok",
        "key": str(key_path),
        "corpus": key.get("corpus"),
        "citation_parser": parser,
        "concept_bodies_source": b.concept_bodies_source,
        "concepts_with_bodies": len(concept_cits),
        "concepts_with_any_citation": sum(1 for v in concept_cits.values() if v),
        "concept_citations": {"total": total_cits, "valid": valid_cits,
                              "validity_pct": round(100.0 * valid_cits / total_cits, 2)
                              if total_cits else None},
        "overall": tally(results),
        "location_recall": {"key_locations": all_loc, "covered": cov_loc,
                            "pct": round(100.0 * cov_loc / all_loc, 2) if all_loc else None},
        "by_type": breakdown(lambda r: [r["type"] or "?"]),
        "by_evidence_class": breakdown(lambda r: [r["evidence"] or "?"]),
        "by_flag": breakdown(lambda r: r["flags"] or ["(none)"]),
        "missing_items": [{k: r[k] for k in ("id", "type", "evidence", "flags", "locations",
                                             "statement")}
                          for r in results if r["degree"] == "missing"],
        "partial_items": [{k: r[k] for k in ("id", "type", "locations", "locations_covered",
                                             "best_concept_title")}
                          for r in results if r["degree"] == "partial"][:MAX_EXAMPLES],
        "items": results,
    }
