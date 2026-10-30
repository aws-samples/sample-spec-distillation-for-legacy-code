#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Measure detection power: did a pipeline output notice the injected defects?

    python3 eval/mutate/score_mutations.py \
        --mutations /tmp/ap-mut/mutations.yaml \
        --output  output/ap-mut/bottled_spec/bottled_spec.md \
        --source-root /tmp/ap-mut/corpus \
        --control output/ap-invoices-demo/bottled_spec/bottled_spec.md \
        --out /tmp/ap-mut/detect

What is measured
----------------
For each injected mutation the detector asks two separate questions:

``located``
    Does any finding point at the mutated lines? Evidence is a citation whose
    file and line range overlap a mutation location (within ``--line-slack``).

``flagged``
    Does that finding *say something is wrong*? Evidence is a match against the
    mutation's ``detect_terms`` (written by the injector, so the vocabulary is
    fixed before the output is seen) or the shared defect vocabulary.

``detected = flagged`` on a located finding, **or** flagged on a finding that
names the mutated symbol / file / literal. The second route matters because some
artifacts carry no citations at all (the bottled spec strips them), and scoring
those at zero would measure citation style rather than detection power.

False positives
---------------
Detection rate alone is gameable by a tool that calls everything suspicious, so
the report also carries:

* ``control``: the same detector run over the output for the **unmutated**
  corpus. A mutation "detected" there was never detectable - the tool flags that
  spot anyway. ``net`` detection subtracts it.
* ``defect_claims_off_site``: findings that flag a defect somewhere no mutation
  was injected. The base corpus has its own seeded traps, so pass ``--base-key``
  to subtract claims that land on a trap-flagged item of the base answer key;
  what is left is ``unexplained``.

This module is self-contained on purpose: it does not import ``eval/score.py``,
so a rewrite of the scorer cannot change these numbers. Pass ``--score-cmd`` to
additionally run the real scorer as a black-box CLI over ``mutations.yaml``
(which is deliberately in answer-key schema) and fold its ``key_coverage`` in.
No Bedrock calls are made by this module itself.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except Exception:                                             # pragma: no cover
    yaml = None

# Words that mean "this is a problem", independent of mutation category. Used for the
# off-site false-positive count and as a fallback signal.
DEFECT_VOCAB = [
    "inconsisten", "conflict", "contradict", "mismatch", "diverg", "disagree",
    "does not match", "out of sync", "no longer match",
    "dead code", "unused", "no caller", "never called", "unreachable", "orphan",
    "duplicate of", "duplicated logic", "redundant copy", "not referenced",
    "off-by-one", "off by one", "boundary mismatch", "excludes the boundary",
    "undocumented", "not documented", "silently", "silent cap",
    "not enforced", "unenforced", "missing validation",
    "only in the database", "database only", "db-only", "db only",
    "comment only", "comment-only", "only in a comment", "stale comment",
    "defect", "discrepanc", "drift", "obsolete", "bypass",
]

ARTIFACT_CANDIDATES = [
    "issues/issues.json", "issues/issues.md",
    "bottled_spec/bottled_spec.md", "master_blend/master_blend.md", "spec.md",
]

EXT = r"(?:vb|sql|cs|cob|cbl|cpy|txt|rpt|md|py|rs|java)"
_RANGE = (r"`?\s*(?:[:#,]|\s)\s*\(?\s*(?:lines?\s+|L)?(?P<a>\d+)"
          r"(?:\s*(?:-|–|—|to|\.\.)\s*L?(?P<b>\d+))?")
# A file name with spaces or parentheses (e.g. "AP Open Payables by Job (412).txt") is only
# recognised when it starts right after a path separator or a backtick - otherwise the
# preceding prose would be swallowed into the file name.
CIT_PATTERNS = [
    re.compile(r"(?:(?<=/)|(?<=`))(?P<file>[A-Za-z0-9_][A-Za-z0-9_.()\- ]*\." + EXT + r")"
               + _RANGE, re.I),
    re.compile(r"(?<![\w/.\-])(?P<file>[A-Za-z0-9_][A-Za-z0-9_.\-]*\." + EXT + r")"
               + _RANGE, re.I),
]
BARE_LINE_RE = re.compile(r"(?<![\w.:/#-])(?:lines?\s+|L)(?P<a>\d+)"
                          r"(?:\s*(?:-|–|—|to)\s*L?(?P<b>\d+))?\b", re.I)
IDENT_RE = re.compile(r"\b(?:[A-Za-z]+_[A-Z0-9_]+|[a-z]+[A-Z]\w+|[A-Z][a-z]+[A-Z]\w+|usp_\w+|trg_\w+)\b")
NUM_RE = re.compile(r"\b\d{3,}(?:\.\d+)?\b")


# --------------------------------------------------------------------------- loading
def load_yaml(p: Path):
    if yaml is None:                                          # pragma: no cover
        raise SystemExit("PyYAML is required to read mutations.yaml")
    return yaml.safe_load(p.read_text())


def resolve_artifact(path: Path, artifact: str | None) -> Path:
    if path.is_file():
        return path
    if not path.is_dir():
        raise SystemExit(f"output not found: {path}")
    if artifact:
        p = path / artifact
        if not p.exists():
            raise SystemExit(f"artifact not found: {p}")
        return p
    for cand in ARTIFACT_CANDIDATES:
        if (path / cand).exists():
            return path / cand
    raise SystemExit(f"no known artifact under {path}; pass --artifact")


def segment(path: Path) -> list[dict]:
    """Split an output artifact into findings: markdown blocks, or JSON statement objects."""
    text = path.read_text(errors="replace")
    if path.suffix.lower() == ".json":
        return _segment_json(json.loads(text))
    out, cur, heading, in_code = [], [], "", False

    def flush():
        nonlocal cur
        body = "\n".join(cur).strip()
        if body and len(re.findall(r"[A-Za-z]{3,}", body)) >= 3:
            out.append({"id": f"F{len(out) + 1}", "heading": heading, "text": body})
        cur = []

    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            cur.append(line)
            continue
        if in_code:
            cur.append(line)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush()
            heading = m.group(2).strip()
            continue
        if not line.strip():
            flush()
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", line):
            continue
        if line.lstrip().startswith(("|", "-", "*", "+")) or re.match(r"^\s*\d+[.)]\s", line):
            if cur and not line.startswith((" ", "\t")):
                flush()
        cur.append(line)
    flush()
    for f in out:                                 # the heading is part of the finding's text
        f["text"] = (f["heading"] + "\n" + f["text"]) if f["heading"] else f["text"]
    return out


def _segment_json(obj) -> list[dict]:
    out: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            stmt = next((o[k] for k in ("statement", "description", "finding", "rule", "text",
                                        "summary", "title") if isinstance(o.get(k), str)
                         and len(o[k]) > 15), None)
            if stmt:
                out.append({"id": f"F{len(out) + 1}", "heading": str(o.get("title", "")),
                            "text": json.dumps(o, default=str)})
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def citations(text: str) -> list[dict]:
    cits, spans = [], []
    for pat in CIT_PATTERNS:
        for m in pat.finditer(text):
            if any(s < m.end() and m.start() < e for s, e in spans):
                continue
            a = int(m.group("a"))
            b = int(m.group("b")) if m.group("b") else a
            cits.append({"file": m.group("file").strip(), "start": a, "end": b})
            spans.append(m.span())
    if cits:                                      # keep text order (spaced pattern ran first)
        order = sorted(range(len(cits)), key=lambda i: spans[i][0])
        cits, spans = [cits[i] for i in order], [spans[i] for i in order]
    last = cits[-1]["file"] if cits else None
    for m in BARE_LINE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans) or last is None:
            continue
        prev = [c for c, s in zip(cits, spans) if s[1] <= m.start()]
        a = int(m.group("a"))
        b = int(m.group("b")) if m.group("b") else a
        cits.append({"file": (prev[-1]["file"] if prev else last), "start": a, "end": b})
    return cits


# --------------------------------------------------------------------------- detection
def base(name: str) -> str:
    return name.replace("\\", "/").split("/")[-1].lower()


def anchors_for(item: dict) -> dict:
    """Identifiers, literals and file names that pin a finding to this mutation's site."""
    mut = item.get("mutation", {})
    # the site id is "<kind>:<symbol>@<path>:<line>"; only the part before "@" names code.
    # Path segments ("ClientSide", "modJCGlobals") are NOT identifiers: counting them let any
    # flagged finding that merely names the file "detect" the mutation (JC seed 7 control floor).
    site = str(mut.get("site", "")).split("@", 1)[0]
    blob = " ".join([str(mut.get("before", "")), str(mut.get("after", "")), site])
    files = {base(l["file"]) for l in item.get("locations", [])}
    stems = {f.rsplit(".", 1)[0] for f in files}
    path_parts = {seg.lower() for l in item.get("locations", [])
                  for seg in str(l["file"]).replace("\\", "/").replace(".", "/").split("/")}
    idents = {w for w in IDENT_RE.findall(blob) if len(w) >= 5 and w.lower() not in path_parts}
    nums = set(NUM_RE.findall(blob))
    return {"idents": sorted(idents), "nums": sorted(nums), "files": sorted(files | stems)}


def loc_range(l: dict) -> tuple[int, int]:
    """Answer-key locations use start_line/end_line; citations use start/end."""
    a = int(l.get("start_line", l.get("start", 1)))
    b = int(l.get("end_line", l.get("end", a)) or a)
    return (a, b) if a <= b else (b, a)


def overlaps(cit: dict, locs: list[dict], slack: int) -> bool:
    cs, ce = loc_range(cit)
    for l in locs:
        if base(cit["file"]) != base(l["file"]):
            continue
        ls, le = loc_range(l)
        if cs - slack <= le and ls - slack <= ce:
            return True
    return False


def term_hits(text_lc: str, terms: list[str]) -> list[str]:
    return [t for t in terms if t in text_lc]


def detect(items: list[dict], findings: list[dict], slack: int) -> dict:
    """Per-mutation detection verdicts for one artifact."""
    for f in findings:
        f.setdefault("cits", citations(f["text"]))
        f.setdefault("lc", f["text"].lower())
    results = []
    for item in items:
        mut = item.get("mutation", {})
        terms = [t.lower() for t in mut.get("detect_terms", [])] or DEFECT_VOCAB
        locs = item.get("locations", [])
        anc = anchors_for(item)
        cited, flagged_cited, flagged_anchor = [], [], []
        for f in findings:
            on_site = any(overlaps(c, locs, slack) for c in f["cits"])
            hits = term_hits(f["lc"], terms)
            if on_site:
                cited.append(f["id"])
                if hits:
                    flagged_cited.append({"id": f["id"], "terms": hits,
                                          "snippet": f["text"][:240].replace("\n", " ")})
                continue
            if not hits:
                continue
            named = (any(i.lower() in f["lc"] for i in anc["idents"])
                     or any(n in f["lc"] for n in anc["nums"] if len(n) >= 4))
            in_file_scope = any(fn in f["lc"] for fn in anc["files"])
            if named and in_file_scope:
                flagged_anchor.append({"id": f["id"], "terms": hits,
                                       "snippet": f["text"][:240].replace("\n", " ")})
        detected_cited = bool(flagged_cited)
        detected_any = detected_cited or bool(flagged_anchor)
        results.append({
            "id": item["id"], "category": mut.get("category", "?"),
            "detection_flag": mut.get("detection_flag", "?"),
            "located": bool(cited), "detected_cited": detected_cited,
            "detected_any": detected_any,
            "verdict": ("detected_cited" if detected_cited else
                        "detected_unlocated" if detected_any else
                        "located_not_flagged" if cited else "missed"),
            "evidence": (flagged_cited or flagged_anchor)[:3],
            "n_findings_on_site": len(cited),
        })
    return {"mutations": results}


def defect_claims(findings: list[dict], items: list[dict], slack: int,
                  base_trap_locs: list[dict]) -> dict:
    """Findings that flag a defect: how many land on an injected site, how many do not."""
    all_locs = [l for it in items for l in it.get("locations", [])]
    on_site = off_site = explained = 0
    off_examples = []
    for f in findings:
        lc = f.get("lc") or f["text"].lower()
        if not term_hits(lc, DEFECT_VOCAB):
            continue
        cits = f.get("cits") or citations(f["text"])
        if any(overlaps(c, all_locs, slack) for c in cits):
            on_site += 1
            continue
        off_site += 1
        if base_trap_locs and any(overlaps(c, base_trap_locs, slack) for c in cits):
            explained += 1
        elif len(off_examples) < 10:
            off_examples.append({"id": f["id"], "snippet": f["text"][:200].replace("\n", " ")})
    return {"defect_claims_on_site": on_site, "defect_claims_off_site": off_site,
            "defect_claims_off_site_explained_by_base_key": explained,
            "defect_claims_off_site_unexplained": off_site - explained,
            "off_site_examples": off_examples}


def by_category(results: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in results:
        g = out.setdefault(r["category"], {"n": 0, "detected_cited": 0, "detected_any": 0,
                                           "located": 0, "missed": 0, "ids": []})
        g["n"] += 1
        g["ids"].append(r["id"])
        g["detected_cited"] += int(r["detected_cited"])
        g["detected_any"] += int(r["detected_any"])
        g["located"] += int(r["located"])
        g["missed"] += int(r["verdict"] == "missed")
    for g in out.values():
        g["detection_rate_pct"] = round(100.0 * g["detected_any"] / g["n"], 1)
        g["detection_rate_cited_pct"] = round(100.0 * g["detected_cited"] / g["n"], 1)
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- optional scorer
def run_score_cmd(cmd: str, mutations: Path, source_root: Path, spec: Path, out: Path) -> dict:
    """Run an external scorer as a black box over mutations.yaml used as an answer key.

    Only flags the scorer actually advertises in ``--help`` are passed, so this keeps
    working when the scorer's interface changes. Returns the parsed score.json (or an
    error record) - never raises.
    """
    argv = shlex.split(cmd)
    try:
        helptext = subprocess.run(argv + ["--help"], capture_output=True, text=True,
                                  timeout=120).stdout
    except Exception as exc:
        return {"error": f"scorer --help failed: {exc}"}
    have = set(re.findall(r"--[a-z0-9-]+", helptext))
    need = {"--spec", "--key", "--source-root"}
    if not need <= have:
        return {"error": f"scorer lacks required flags {sorted(need - have)}"}
    out.mkdir(parents=True, exist_ok=True)
    argv += ["--spec", str(spec), "--key", str(mutations), "--source-root", str(source_root)]
    for flag, val in (("--out-dir", str(out)), ("--no-llm", None)):
        if flag in have:
            argv.append(flag)
            if val is not None:
                argv.append(val)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    except Exception as exc:
        return {"error": f"scorer failed: {exc}"}
    sj = out / "score.json"
    if not sj.exists():
        return {"error": f"scorer produced no score.json (rc={p.returncode})",
                "stderr": p.stderr[-800:]}
    data = json.loads(sj.read_text())
    return {"key_coverage": data.get("key_coverage"), "recall": data.get("recall"),
            "method": data.get("method"), "score_json": str(sj)}


# --------------------------------------------------------------------------- report
def to_markdown(r: dict) -> str:
    L = [f"# Mutation detection: `{r['output']}`", "",
         f"- Ground truth: `{r['mutations_file']}` ({r['counts']['mutations']} injected defects, "
         f"seed {r['seed']})",
         f"- Source root: `{r['source_root']}`",
         f"- Findings parsed: {r['counts']['findings']}; line slack {r['line_slack']}",
         f"- Control output: `{r.get('control') or 'none'}`", "",
         "## Detection rate", "",
         "| Category | Injected | Detected (any evidence) | Detected with a citation | "
         "Located but not flagged | Missed | Detection % | Net of control % |",
         "|---|---|---|---|---|---|---|---|"]
    ctrl = r.get("control_by_category") or {}
    for cat, g in r["by_category"].items():
        c = ctrl.get(cat, {}).get("detected_any", 0)
        net = round(100.0 * max(0, g["detected_any"] - c) / g["n"], 1)
        L.append(f"| {cat} | {g['n']} | {g['detected_any']} | {g['detected_cited']} | "
                 f"{g['located'] - g['detected_cited']} | {g['missed']} | "
                 f"{g['detection_rate_pct']}% | {net}% |")
    o = r["overall"]
    L += ["", f"**Overall: {o['detected_any']}/{o['n']} detected "
              f"({o['detection_rate_pct']}%), {o['detected_cited']} with a citation on the "
              f"mutated lines, {o['missed']} not mentioned at all.**", ""]
    if r.get("control_overall"):
        co = r["control_overall"]
        L += [f"Control (unmutated corpus, same detector): {co['detected_any']}/{co['n']} "
              f"of the same sites already flagged. Net detection "
              f"{o['detected_any'] - co['detected_any']}/{o['n']}.", ""]
    fp = r["false_positives"]
    L += ["## False-positive behaviour", "", "| Measure | Count |", "|---|---|",
          f"| Findings flagging a defect on an injected site | {fp['defect_claims_on_site']} |",
          f"| Findings flagging a defect elsewhere | {fp['defect_claims_off_site']} |",
          f"| ... explained by a trap in the base answer key | "
          f"{fp['defect_claims_off_site_explained_by_base_key']} |",
          f"| ... unexplained | {fp['defect_claims_off_site_unexplained']} |", "",
          "Off-site claims are **not** errors by themselves: the base corpus carries its own "
          "seeded traps and real smells. Pass `--base-key` to subtract the known ones. Read this "
          "block as the cost side of the detection rate above.", "",
          "## Per mutation", "", "| id | Category | Verdict | Findings on site | Evidence |",
          "|---|---|---|---|---|"]
    for m in r["mutations"]:
        ev = m["evidence"][0]["snippet"][:120].replace("|", "/") if m["evidence"] else ""
        L.append(f"| {m['id']} | {m['category']} | {m['verdict']} | {m['n_findings_on_site']} | {ev} |")
    if r.get("scorer"):
        L += ["", "## External scorer (black-box run over mutations.yaml)", "",
              "```json", json.dumps(r["scorer"], indent=2)[:2000], "```"]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mutations", required=True, type=Path, help="mutations.yaml ground truth")
    ap.add_argument("--output", required=True, type=Path,
                    help="pipeline output for the MUTATED corpus: a spec file or a run dir")
    ap.add_argument("--artifact", help="artifact path inside --output when it is a dir")
    ap.add_argument("--source-root", type=Path, help="the mutated corpus (for the report only)")
    ap.add_argument("--control", type=Path,
                    help="same artifact produced from the UNMUTATED corpus (false-positive baseline)")
    ap.add_argument("--base-key", type=Path,
                    help="base corpus answer key; its flagged items explain off-site defect claims")
    ap.add_argument("--line-slack", type=int, default=3,
                    help="lines of tolerance when matching a citation to a mutated line (default 3)")
    ap.add_argument("--min-detection-pct", type=float,
                    help="exit non-zero if overall detection %% is below this")
    ap.add_argument("--score-cmd",
                    help="optional external scorer to run as a black box, "
                         "e.g. \"python3 eval/score.py\"")
    ap.add_argument("--out", type=Path, help="output dir (default: alongside --mutations)")
    a = ap.parse_args()

    gt = load_yaml(a.mutations)
    items = gt.get("items", [])
    if not items:
        raise SystemExit(f"{a.mutations} has no items")
    spec = resolve_artifact(a.output, a.artifact)
    findings = segment(spec)

    det = detect(items, findings, a.line_slack)
    cats = by_category(det["mutations"])
    n = len(det["mutations"])
    overall = {
        "n": n,
        "detected_any": sum(1 for m in det["mutations"] if m["detected_any"]),
        "detected_cited": sum(1 for m in det["mutations"] if m["detected_cited"]),
        "located": sum(1 for m in det["mutations"] if m["located"]),
        "missed": sum(1 for m in det["mutations"] if m["verdict"] == "missed"),
    }
    overall["detection_rate_pct"] = round(100.0 * overall["detected_any"] / n, 1)
    overall["detection_rate_cited_pct"] = round(100.0 * overall["detected_cited"] / n, 1)

    base_locs: list[dict] = []
    if a.base_key and a.base_key.exists():
        bk = load_yaml(a.base_key)
        for it in bk.get("items", []):
            if it.get("flags"):
                base_locs += it.get("locations", [])
    fp = defect_claims(findings, items, a.line_slack, base_locs)

    control_by_cat = control_overall = None
    if a.control:
        cspec = resolve_artifact(a.control, a.artifact)
        cf = segment(cspec)
        cdet = detect(items, cf, a.line_slack)
        control_by_cat = by_category(cdet["mutations"])
        control_overall = {
            "n": n,
            "detected_any": sum(1 for m in cdet["mutations"] if m["detected_any"]),
            "detected_cited": sum(1 for m in cdet["mutations"] if m["detected_cited"]),
        }

    report = {
        "mutations_file": str(a.mutations), "output": str(spec),
        "source_root": str(a.source_root or ""), "control": str(a.control) if a.control else None,
        "seed": gt.get("seed"), "mutation_schema_version": gt.get("mutation_schema_version"),
        "line_slack": a.line_slack,
        "counts": {"mutations": n, "findings": len(findings)},
        "overall": overall, "by_category": cats,
        "control_overall": control_overall, "control_by_category": control_by_cat,
        "false_positives": fp,
        "mutations": det["mutations"],
    }
    if a.score_cmd:
        report["scorer"] = run_score_cmd(a.score_cmd, a.mutations,
                                         a.source_root or Path("."), spec,
                                         (a.out or a.mutations.parent) / "scorer")

    out = a.out or a.mutations.parent
    out.mkdir(parents=True, exist_ok=True)
    (out / "mutation_score.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "mutation_score.md").write_text(to_markdown(report))
    print(json.dumps({"overall": overall, "by_category": cats,
                      "false_positives": {k: v for k, v in fp.items() if k != "off_site_examples"}},
                     indent=2))
    print(f"wrote {out}/mutation_score.{{json,md}}")
    if a.min_detection_pct is not None and overall["detection_rate_pct"] < a.min_detection_pct:
        print(f"FAIL: detection {overall['detection_rate_pct']}% < "
              f"--min-detection-pct {a.min_detection_pct}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
