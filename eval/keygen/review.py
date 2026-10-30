#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Human review of a draft answer key: HTML sheet, CSV, decisions, filtered key for scoring.

    # (a) a self-contained HTML review sheet (+ CSV for spreadsheet review)
    python3 eval/keygen/review.py --key KEY.yaml --source /tmp/eshop \
        --html KEY.review.html --csv KEY.review.csv

    # (b) apply the decisions exported from the sheet (or a filled-in CSV) -> reviewed key
    python3 eval/keygen/review.py --key KEY.yaml --apply decisions.json --out KEY.reviewed.yaml

    # (c) keep only reviewed items, so eval/score.py scores against what a human accepted
    python3 eval/keygen/review.py --key KEY.reviewed.yaml --filter-out KEY.scored.yaml
    python3 eval/score.py --spec SPEC.md --key KEY.scored.yaml --source-root /tmp/eshop

The HTML file has no external dependencies: each item shows its statement, sources, flags and a
line-numbered code excerpt, with accept / edit / reject controls. "Export decisions" downloads a
``decisions.json``; progress is also kept in the browser's localStorage. Decisions are keyed by
item id, and ids are stable hashes, so decisions survive a rebuild of the key.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yaml                                                  # noqa: E402

from build_key import counts, header                         # noqa: E402

DEFAULT_SCORED = ("accepted", "edited", "planted")
CSV_COLS = ["id", "type", "status", "source", "evidence_class", "flags", "statement", "locations",
            "snippet", "decision", "edited_statement", "edited_type", "note"]


def load(path: Path) -> dict:
    d = yaml.safe_load(Path(path).read_text())
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        raise SystemExit(f"{path}: not an answer key (needs an 'items' list)")
    return d


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path: Path, d: dict) -> None:
    meta = dict(d.get("metadata") or {})
    meta["counts"] = counts(d["items"])
    d = {**d, "metadata": meta}
    hdr = header(meta)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(hdr + yaml.safe_dump(d, sort_keys=False, default_flow_style=False,
                                               width=100, allow_unicode=True))


# --------------------------------------------------------------------------- excerpts
#: excerpts quote only the cited lines (no context), at most 8 lines from at most 2 locations
#: per item, so a committed sheet quotes short snippets of third-party code, not whole files
MAX_EXCERPT_LOCS = 2


def excerpt(source: Path | None, loc: dict, context: int = 0, cap: int = 8) -> list[tuple[int, str, bool]]:
    if source is None:
        return []
    p = Path(source) / loc["file"]
    if not p.is_file():
        return []
    lines = p.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").split("\n")
    a, b = int(loc["start_line"]), int(loc["end_line"])
    lo, hi = max(1, a - context), min(len(lines), b + context)
    if hi - lo + 1 > cap:
        hi = lo + cap - 1
    return [(n, lines[n - 1], a <= n <= b) for n in range(lo, hi + 1)]


# --------------------------------------------------------------------------- HTML
CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;background:#f6f7f9;color:#1d232b}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #d9dde3;padding:10px 18px;z-index:2}
header h1{font-size:16px;margin:0 0 4px}
header .meta{color:#5b6573;font-size:12px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:6px}
.bar input,.bar select{font:inherit;padding:3px 6px}
button{font:inherit;padding:4px 10px;border:1px solid #9aa4b1;border-radius:4px;background:#fff;cursor:pointer}
button.primary{background:#1f5fbf;color:#fff;border-color:#1f5fbf}
main{padding:12px 18px}
.item{background:#fff;border:1px solid #d9dde3;border-radius:6px;padding:10px 12px;margin:0 0 10px}
.item.accept{border-left:5px solid #2e7d32}.item.edit{border-left:5px solid #b26a00}.item.reject{border-left:5px solid #b3261e;opacity:.75}
.head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.id{font-weight:600;font-family:ui-monospace,Menlo,monospace}
.tag{font-size:11px;padding:1px 6px;border-radius:9px;background:#eef1f5;color:#3c4652}
.tag.corroborated{background:#e3f1e4;color:#1e5b22}.tag.flag{background:#fdeee0;color:#7a4300}
.stmt{margin:6px 0}
textarea{width:100%;min-height:52px;font:inherit;box-sizing:border-box}
pre{background:#f3f5f8;border:1px solid #e3e7ec;border-radius:4px;padding:6px 8px;overflow:auto;font:12px/1.4 ui-monospace,Menlo,monospace;margin:6px 0}
pre .hl{background:#fff4c2;display:block}
pre .ln{color:#8a94a3;display:inline-block;width:4.5em;user-select:none}
.loc{font:12px ui-monospace,Menlo,monospace;color:#3c4652}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:6px}
.prov{font-size:12px;color:#5b6573;margin:4px 0 0 0;padding-left:18px}
.hidden{display:none}
"""

JS = r"""
const KEY = JSON.parse(document.getElementById('keydata').textContent);
const store = 'kgreview:' + KEY.key_sha256;
let D = JSON.parse(localStorage.getItem(store) || '{}');
function save(){ localStorage.setItem(store, JSON.stringify(D)); tally(); }
function set(id, field, val){ D[id] = D[id] || {}; D[id][field] = val; if(field==='decision'){ paint(id);} save(); }
function paint(id){ const el=document.getElementById('it-'+id); const d=(D[id]||{}).decision||'';
  el.classList.remove('accept','edit','reject'); if(d) el.classList.add(d);
  el.querySelector('.editbox').classList.toggle('hidden', d!=='edit'); }
function tally(){ const n=Object.values(D).filter(x=>x.decision).length;
  document.getElementById('tally').textContent = n + ' / ' + KEY.n + ' decided'; }
function exportDecisions(){
  const reviewer=document.getElementById('reviewer').value.trim();
  if(!reviewer){ alert('Enter a reviewer name first'); return; }
  const out={key: KEY.key, key_sha256: KEY.key_sha256, reviewer: reviewer,
             date: new Date().toISOString().slice(0,10), decisions: {}};
  for(const [id,d] of Object.entries(D)){ if(d.decision) out.decisions[id]=d; }
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{type:'application/json'}));
  a.download='decisions.json'; a.click(); }
function filt(){ const q=document.getElementById('q').value.toLowerCase();
  const src=document.getElementById('fsrc').value, ty=document.getElementById('ftype').value,
        st=document.getElementById('fstate').value;
  for(const el of document.querySelectorAll('.item')){
    const d=(D[el.dataset.id]||{}).decision||'';
    const ok=(!q||el.textContent.toLowerCase().includes(q))&&(!src||el.dataset.src.includes(src))&&
             (!ty||el.dataset.type===ty)&&(!st||(st==='undecided'?!d:d===st));
    el.classList.toggle('hidden',!ok); } }
window.addEventListener('DOMContentLoaded',()=>{
  for(const el of document.querySelectorAll('.item')){ const id=el.dataset.id; const d=D[id]||{};
    if(d.decision){ const r=el.querySelector('input[value="'+d.decision+'"]'); if(r) r.checked=true; }
    if(d.statement) el.querySelector('textarea.st').value=d.statement;
    if(d.note) el.querySelector('input.note').value=d.note;
    paint(id); }
  document.getElementById('reviewer').value=localStorage.getItem('kgreview:reviewer')||'';
  tally(); });
"""


def render_html(d: dict, key_path: Path, source: Path | None) -> str:
    meta = d.get("metadata") or {}
    items = d["items"]
    srcs = sorted({s.split(":")[0] for it in items for s in it.get("source", [])})
    types = sorted({it["type"] for it in items})
    keydata = {"key": str(key_path), "key_sha256": sha(key_path), "n": len(items)}
    e = html.escape
    out = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
           f"<title>Review: {e(str(d.get('corpus', '')))} answer key</title><style>{CSS}</style></head><body>",
           "<header>",
           f"<h1>Answer-key review: {e(str(d.get('corpus', '')))} ({len(items)} items)</h1>",
           f"<div class='meta'>{e(str(meta.get('repo_url') or ''))} @ {e(str(meta.get('commit') or ''))} · "
           f"built by {e(str((meta.get('builder') or {}).get('name', '')))} "
           f"{e(str((meta.get('builder') or {}).get('version', '')))} on {e(str(meta.get('generated') or ''))} · "
           f"key sha256 {keydata['key_sha256'][:12]}"]
    if meta.get("source_license"):
        out.append(f" · code excerpts © their authors, {e(str(meta['source_license']))} license</div>")
    else:
        out.append("</div>")
    out.append("<div class='bar'>Reviewer <input id='reviewer' "
               "onchange=\"localStorage.setItem('kgreview:reviewer',this.value)\" placeholder='your alias'>"
               " <input id='q' placeholder='search' oninput='filt()'>"
               " <select id='fsrc' onchange='filt()'><option value=''>all sources</option>"
               + "".join(f"<option>{e(s)}</option>" for s in srcs) + "</select>"
               " <select id='ftype' onchange='filt()'><option value=''>all types</option>"
               + "".join(f"<option>{e(t)}</option>" for t in types) + "</select>"
               " <select id='fstate' onchange='filt()'><option value=''>all</option><option>undecided</option>"
               "<option value='accept'>accepted</option><option value='edit'>edited</option>"
               "<option value='reject'>rejected</option></select>"
               " <span id='tally'></span>"
               " <button class='primary' onclick='exportDecisions()'>Export decisions</button></div>")
    out.append("</header><main>")
    for it in items:
        iid = str(it["id"])
        flags = it.get("flags") or []
        src = " ".join(it.get("source") or [])
        out.append(f"<div class='item' id='it-{e(iid)}' data-id='{e(iid)}' data-type='{e(it['type'])}' "
                   f"data-src='{e(src)}'>")
        out.append(f"<div class='head'><span class='id'>{e(iid)}</span>"
                   f"<span class='tag'>{e(it['type'])}</span>"
                   f"<span class='tag'>{e(str(it.get('evidence_class', '')))}</span>"
                   f"<span class='tag'>status: {e(str(it.get('status', 'unreviewed')))}</span>"
                   + "".join(f"<span class='tag'>{e(s)}</span>" for s in it.get("source") or [])
                   + "".join(f"<span class='tag {'corroborated' if f == 'corroborated' else 'flag'}'>{e(f)}</span>"
                             for f in flags) + "</div>")
        out.append(f"<div class='stmt'>{e(str(it['statement']))}</div>")
        for loc in (it.get("locations") or [])[:MAX_EXCERPT_LOCS]:
            out.append(f"<div class='loc'>{e(loc['file'])}:{loc['start_line']}-{loc['end_line']}</div>")
            ex = excerpt(source, loc)
            if ex:
                out.append("<pre>" + "".join(
                    f"<span class='{'hl' if hl else ''}'><span class='ln'>{n}</span>{e(t)}</span>"
                    + ("" if hl else "\n") for n, t, hl in ex) + "</pre>")
            elif it.get("snippet"):
                out.append(f"<pre>{e(str(it['snippet']))}</pre>")
        if len(it.get("locations") or []) > MAX_EXCERPT_LOCS:
            out.append(f"<div class='loc'>… and {len(it['locations']) - MAX_EXCERPT_LOCS} more location(s)</div>")
        prov = it.get("provenance") or []
        if len(prov) > 1:
            out.append("<ul class='prov'>" + "".join(
                f"<li>{e(p['source'])}: {e(p['statement'])}</li>" for p in prov) + "</ul>")
        out.append(
            "<div class='controls'>"
            + "".join(f"<label><input type='radio' name='d-{e(iid)}' value='{v}' "
                      f"onchange=\"set('{e(iid)}','decision','{v}')\"> {lbl}</label>"
                      for v, lbl in (("accept", "accept"), ("edit", "edit"), ("reject", "reject")))
            + f" <input class='note' placeholder='note' size='40' onchange=\"set('{e(iid)}','note',this.value)\">"
            "</div>"
            f"<div class='editbox hidden'><textarea class='st' onchange=\"set('{e(iid)}','statement',this.value)\">"
            f"{e(str(it['statement']))}</textarea></div>")
        out.append("</div>")
    out.append("</main>")
    out.append(f"<script type='application/json' id='keydata'>{json.dumps(keydata)}</script>")
    out.append(f"<script>{JS}</script></body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------- CSV
def render_csv(d: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, lineterminator="\n")
    w.writeheader()
    for it in d["items"]:
        w.writerow({
            "id": it["id"], "type": it["type"], "status": it.get("status", "unreviewed"),
            "source": ";".join(it.get("source") or []), "evidence_class": it.get("evidence_class", ""),
            "flags": ";".join(it.get("flags") or []), "statement": it["statement"],
            "locations": ";".join(f"{l['file']}:{l['start_line']}-{l['end_line']}" for l in it.get("locations") or []),
            "snippet": it.get("snippet", ""), "decision": "", "edited_statement": "", "edited_type": "",
            "note": ""})
    return buf.getvalue()


def decisions_from_csv(path: Path) -> dict:
    out = {"reviewer": None, "decisions": {}}
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            dec = (row.get("decision") or "").strip().lower()
            dec = {"accepted": "accept", "a": "accept", "edited": "edit", "e": "edit",
                   "rejected": "reject", "r": "reject"}.get(dec, dec)
            if dec not in ("accept", "edit", "reject"):
                continue
            d = {"decision": dec}
            if row.get("edited_statement", "").strip():
                d["statement"] = row["edited_statement"].strip()
            if row.get("edited_type", "").strip():
                d["type"] = row["edited_type"].strip()
            if row.get("note", "").strip():
                d["note"] = row["note"].strip()
            out["decisions"][row["id"]] = d
    return out


# --------------------------------------------------------------------------- apply / filter
TYPES = {"rule", "data", "state", "workflow", "exception", "interface", "permission"}


def apply_decisions(d: dict, dec: dict, reviewer: str | None = None, when: str | None = None) -> tuple[dict, dict]:
    by_id = {str(it["id"]): it for it in d["items"]}
    rep = {"accepted": 0, "edited": 0, "rejected": 0, "unknown_ids": []}
    for iid, x in sorted((dec.get("decisions") or {}).items()):
        it = by_id.get(str(iid))
        if it is None:
            rep["unknown_ids"].append(iid)
            continue
        kind = x.get("decision")
        if kind == "accept":
            it["status"] = "accepted"
            rep["accepted"] += 1
        elif kind == "edit":
            new = (x.get("statement") or "").strip()
            if new and new != it["statement"]:
                it.setdefault("original_statement", it["statement"])
                it["statement"] = new
            if x.get("type") in TYPES:
                it["type"] = x["type"]
            it["status"] = "edited"
            rep["edited"] += 1
        elif kind == "reject":
            it["status"] = "rejected"
            rep["rejected"] += 1
        else:
            continue
        if x.get("note"):
            it["review_note"] = x["note"]
    meta = d.setdefault("metadata", {})
    meta["reviewer"] = reviewer or dec.get("reviewer") or meta.get("reviewer")
    meta["reviewed"] = when or dec.get("date") or date.today().isoformat()
    meta["decisions_applied"] = {k: v for k, v in rep.items() if k != "unknown_ids"}
    return d, rep


def filter_key(d: dict, statuses: tuple[str, ...] = DEFAULT_SCORED) -> dict:
    items = [it for it in d["items"] if it.get("status", "unreviewed") in statuses]
    meta = dict(d.get("metadata") or {})
    meta["filtered"] = {"statuses": list(statuses), "kept": len(items), "of": len(d["items"])}
    return {**d, "metadata": meta, "items": items}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, type=Path)
    ap.add_argument("--source", type=Path, help="source tree for code excerpts in the HTML sheet")
    ap.add_argument("--html", type=Path, help="write the HTML review sheet here")
    ap.add_argument("--csv", type=Path, help="write a CSV for spreadsheet review here")
    ap.add_argument("--apply", type=Path, help="decisions.json (from the sheet) or a filled-in CSV")
    ap.add_argument("--reviewer", help="override the reviewer recorded in the key")
    ap.add_argument("--date", help="override the review date recorded in the key")
    ap.add_argument("--out", type=Path, help="reviewed key to write (with --apply)")
    ap.add_argument("--filter-out", type=Path, help="write a key with only reviewed items (for score.py)")
    ap.add_argument("--statuses", default=",".join(DEFAULT_SCORED),
                    help=f"statuses kept by --filter-out (default {','.join(DEFAULT_SCORED)})")
    a = ap.parse_args(argv)
    d = load(a.key)
    did = False
    if a.html:
        a.html.parent.mkdir(parents=True, exist_ok=True)
        a.html.write_text(render_html(d, a.key, a.source))
        print(f"wrote {a.html}")
        did = True
    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        a.csv.write_text(render_csv(d))
        print(f"wrote {a.csv}")
        did = True
    if a.apply:
        dec = decisions_from_csv(a.apply) if a.apply.suffix.lower() == ".csv" else json.loads(a.apply.read_text())
        if dec.get("key_sha256") and dec["key_sha256"] != sha(a.key):
            print("note: decisions were exported from a different build of this key; applying by id",
                  file=sys.stderr)
        d, rep = apply_decisions(d, dec, a.reviewer, a.date)
        out = a.out or a.key.with_name(a.key.name.replace(".draft", "").replace(".yaml", ".reviewed.yaml"))
        write(out, d)
        print(json.dumps({"wrote": str(out), **rep}, indent=2))
        did = True
    if a.filter_out:
        fd = filter_key(d, tuple(s.strip() for s in a.statuses.split(",") if s.strip()))
        if not fd["items"]:
            print("warning: no item has a kept status; score.py would score against an empty key",
                  file=sys.stderr)
        write(a.filter_out, fd)
        print(json.dumps({"wrote": str(a.filter_out), **fd["metadata"]["filtered"]}))
        did = True
    if not did:
        ap.error("nothing to do: give --html, --csv, --apply or --filter-out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
