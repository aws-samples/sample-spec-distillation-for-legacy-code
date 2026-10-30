#!/usr/bin/env python3
"""
migrate_concept_ids.py — recast per-file-colliding concept IDs into globally-unique,
vended IDs. NON-DESTRUCTIVE (bronze snapshot first; reversible remap ledger).

WHY
---
IDs were minted by a per-file counter that reset to 0001, so the same ID (e.g.
RL-0001) names many different concepts across a still's files. There are ZERO
concept-to-concept ID references anywhere (verified), so renumbering headers breaks
nothing — but every DERIVED store that embeds the ID (concept_symbols, symbol_backmap,
viewer) must be REGENERATED afterward (done by separate tools), and the hash-keyed
expensive stores (vectors/graph/code-graph) are ID-free and untouched.

SOURCE OF TRUTH (per still): <run>/stills/<still>/final/formatted/
    X.md              -> numbering authority (superset; ids unique within file)
    signal/X.md       -> disjoint partition of X.md (same ids)
    forensics/X.md    -> disjoint partition of X.md (same ids)
Verified corpus-wide: signal ∪ forensics == formatted, no within-file dup ids.

ALGORITHM
---------
Drive numbering from formatted/X.md. For each header (old_id, in order) compute
  claim_key = still + sha256(header-without-id + body)[:16]     (content-stable)
and vend a unique id per still-prefix from the vending machine. Build
  remap[(still, basename, old_id)] = new_id
then rewrite headers in ALL THREE copies by (basename, old_id). Byte-identical
duplicates (same claim_key) intentionally reuse one id and are FLAGGED as merge
candidates (merge is a separate, gated pass — never done here).

Default = DRY RUN (report only). Pass --commit to snapshot + rewrite + write ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

HDR = re.compile(r"^(### \[)((?:N-)?[A-Z]{2}-\d{4,})(\])(.*)$")  # matches bare or N- tagged
CONCEPT_PFX = "DT|DL|EX|HI|IF|PM|RL|RT|ST|TS|WF"
STILL_PREFIX = {  # still dir name -> id prefix (mirror of format_still.STILL_PREFIXES)
    "domain_language": "DL", "data": "DT", "rule": "RL", "workflow": "WF",
    "state": "ST", "permission": "PM", "exception": "EX", "test": "TS",
    "interface": "IF", "history": "HI", "runtime": "RT",
}


def _split_blocks(text: str):
    """Yield (old_id, header_trailing, body_text, header_line_index, lines)."""
    lines = text.split("\n")
    idxs = [i for i, ln in enumerate(lines) if HDR.match(ln)]
    for k, i in enumerate(idxs):
        m = HDR.match(lines[i])
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        body = "\n".join(lines[i + 1:end])
        yield m.group(2), m.group(4), body, i
    return


def _claim_key(still: str, header_trailing: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(still.encode()); h.update(b"\x00")
    h.update(header_trailing.strip().encode()); h.update(b"\x00")
    h.update(body.encode())
    return still + ":" + h.hexdigest()[:16]


def build_remap(run_root: str, vm, stills_filter=None) -> tuple[dict, dict]:
    """Vend new ids driven by formatted/X.md. Returns (remap, stats).
    remap: {(still, basename, old_id): new_id}."""
    root = Path(run_root)
    remap = {}
    stats = {"stills": {}, "concepts": 0}
    content_ordinal = defaultdict(int)      # content claim_key -> occurrences seen
    dupe_groups = defaultdict(list)         # content claim_key -> [(still,base,old_id,new_id)]
    for fmt in sorted(root.glob("stills/*/final/formatted")):
        still = fmt.parts[-3]
        if still not in STILL_PREFIX:
            continue
        if stills_filter and still not in stills_filter:
            continue
        prefix = STILL_PREFIX[still]
        s_stat = {"prefix": prefix, "files": 0, "concepts": 0, "new_min": None, "new_max": None}
        for src in sorted(fmt.glob("*.md")):  # top-level = numbering authority
            base = src.name
            s_stat["files"] += 1
            txt = src.read_text(encoding="utf-8")
            for old_id, trailing, body, _ in _split_blocks(txt):
                content_ck = _claim_key(still, trailing, body)
                content_ordinal[content_ck] += 1
                ordn = content_ordinal[content_ck]
                # UNIQUE per occurrence: content-stable for the 1st, disambiguated
                # for exact-dup occurrences so every concept gets a distinct id.
                vend_ck = content_ck if ordn == 1 else f"{content_ck}#{ordn}"
                res = vm.claim(prefix, vend_ck, caller="migrate_concept_ids",
                               phase="id_recast",
                               evidence={"still": still, "file": base, "old_id": old_id})
                # Decorate the vended (clean) id with the transient N- namespace so the
                # correct state is POSITIVELY marked; anything left un-N- after regen is
                # provably stale. Stripped in the finalize phase once validation passes.
                new_id = "N-" + res["id"]
                remap[(still, base, old_id)] = new_id
                dupe_groups[content_ck].append((still, base, old_id, new_id))
                num = int(new_id.rsplit("-", 1)[1])
                s_stat["new_min"] = num if s_stat["new_min"] is None else min(s_stat["new_min"], num)
                s_stat["new_max"] = num if s_stat["new_max"] is None else max(s_stat["new_max"], num)
                s_stat["concepts"] += 1
                stats["concepts"] += 1
        stats["stills"][still] = s_stat

    # Exact-duplicate content groups (merge candidates — NOT merged here).
    merge_groups = {k: v for k, v in dupe_groups.items() if len(v) > 1}
    stats["merge_candidate_groups"] = len(merge_groups)
    stats["merge_candidate_concepts"] = sum(len(v) for v in merge_groups.values())
    stats["dupe_examples"] = [g[:3] for g in list(merge_groups.values())[:8]]
    stats["_merge_groups"] = merge_groups  # for ledger (not printed)
    return remap, stats


def _rewrite_file(path: Path, remap: dict, still: str) -> tuple[int, int]:
    """Rewrite header id tokens in one file using remap[(still, basename, old_id)].
    Returns (headers_seen, headers_rewritten). STRICT: only the id token changes;
    non-header lines and header trailing text are byte-preserved."""
    base = path.name
    lines = path.read_text(encoding="utf-8").split("\n")
    seen = rew = 0
    out = []
    for ln in lines:
        m = HDR.match(ln)
        if not m:
            out.append(ln); continue
        seen += 1
        old_id = m.group(2)
        new_id = remap.get((still, base, old_id))
        if new_id is None:
            out.append(ln)  # leave untouched (should not happen; validated later)
        else:
            out.append(f"{m.group(1)}{new_id}{m.group(3)}{m.group(4)}")
            rew += 1
    new_text = "\n".join(out)
    # SAFETY: identical line count, and identical set of non-header lines
    orig = path.read_text(encoding="utf-8").split("\n")
    assert len(orig) == len(out), f"line count changed in {path}"
    for a, b in zip(orig, out):
        if not HDR.match(a):
            assert a == b, f"non-header line mutated in {path}"
    path.write_text(new_text, encoding="utf-8")
    return seen, rew


def bronze(run_root: str, ts: str) -> Path:
    root = Path(run_root)
    dest = root / ".backup" / f"pre-idmigration-{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    for fmt in sorted(root.glob("stills/*/final/formatted")):
        still = fmt.parts[-3]
        # copy top-level + signal + forensics md (the id-bearing source of truth)
        for sub in [".", "signal", "forensics"]:
            d = fmt / sub
            if not d.exists():
                continue
            for md in d.glob("*.md"):
                rel = md.relative_to(root)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(md, target)
    return dest


def apply_remap(run_root: str, remap: dict) -> dict:
    root = Path(run_root)
    applied = defaultdict(lambda: [0, 0])
    for fmt in sorted(root.glob("stills/*/final/formatted")):
        still = fmt.parts[-3]
        if still not in STILL_PREFIX:
            continue
        for sub in [".", "signal", "forensics"]:
            d = fmt / sub
            if not d.exists():
                continue
            for md in sorted(d.glob("*.md")):
                seen, rew = _rewrite_file(md, remap, still)
                applied[still][0] += seen
                applied[still][1] += rew
    return {k: {"headers_seen": v[0], "headers_rewritten": v[1]} for k, v in applied.items()}


def _md_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [m.group(2) for ln in path.read_text(encoding="utf-8").split("\n") if (m := HDR.match(ln))]


def _scan_ids_in_text_files(paths, pattern) -> set:
    out = set()
    for p in paths:
        for m in pattern.finditer(Path(p).read_text(encoding="utf-8", errors="replace")):
            out.add(m.group(0).strip("[]"))
    return out


def _md_titles(path: Path):
    """Return list of full concept titles ('[ID] text') from a markdown file."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").split("\n"):
        m = HDR.match(ln)
        if m:
            # .strip() to match concept_symbols._concept_title (which strips), so the
            # (id+title) binding keys are byte-identical across the two code paths.
            out.append(f"[{m.group(2)}]{m.group(4)}".strip())
    return out


def _idbearing_text_files(root: Path):
    """Operational id-bearing TEXT stores swept for N-/bare polarity: markdown source
    + concept_symbols. (symbol_backmap is scanned via its DB column separately.) The
    viewer is validated separately — it is regenerated only in the final phase."""
    files = []
    for fmt in sorted(root.glob("stills/*/final/formatted")):
        for sub in [".", "signal", "forensics"]:
            files += [str(p) for p in sorted((fmt / sub).glob("*.md"))]
    csp = root / "stills" / "concept_symbols.json"
    if csp.exists():
        files.append(str(csp))
    return files


def validate(run_root: str, expect: str = "final", ledger_path: str = "") -> int:
    """Phase-aware corruption gate. expect='ntag' (mid-migration: everything must be
    N- tagged, zero bare) or 'final' (post-strip: everything clean, zero residual N-).
    Also enforces (id+title) binding, uniqueness, and ID-free stores. Returns 0=PASS."""
    import sqlite3, subprocess
    root = Path(run_root)
    BARE = re.compile(r"\[(?!N-)(?:%s)-\d{4,}\]" % CONCEPT_PFX)  # bracketed id, NOT N- tagged
    NTAG = re.compile(r"N-(?:%s)-\d{4,}" % CONCEPT_PFX)          # any N- tagged id
    ANY = re.compile(r"\[(?:N-)?(?:%s)-\d{4,}\]" % CONCEPT_PFX)  # BRACKETED only (concept ids are always bracketed)
    forbidden = BARE if expect == "ntag" else NTAG
    forbidden_name = "bare (un-N-) concept id" if expect == "ntag" else "residual N- tag"
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    # ── markdown truth: ids, structural invariants, binding keys ──
    all_ids, signal_ids, foren_ids = [], set(), set()
    signal_keys = set()
    struct_ok = True
    for fmt in sorted(root.glob("stills/*/final/formatted")):
        still = fmt.parts[-3]
        for src in sorted(fmt.glob("*.md")):
            base = src.name
            f = _md_ids(src); s = _md_ids(fmt / "signal" / base); fo = _md_ids(fmt / "forensics" / base)
            sf, ss, sfo = set(f), set(s), set(fo)
            if (ss | sfo) != sf or (ss & sfo):
                struct_ok = False
            all_ids += f; signal_ids |= ss; foren_ids |= sfo
            for t in _md_titles(fmt / "signal" / base):
                signal_keys.add(f"{still}::{t}")
    chk("markdown: all concept ids globally UNIQUE (1:1)",
        len(all_ids) == len(set(all_ids)),
        f"{len(all_ids)} concepts, {len(set(all_ids))} distinct")
    chk("markdown: signal ∪ forensics == formatted, disjoint", struct_ok)
    # phase polarity on markdown ids
    if expect == "ntag":
        bad = [i for i in all_ids if not i.startswith("N-")]
        chk("markdown: every id is N- tagged", not bad, f"{len(bad)} untagged")
    else:
        bad = [i for i in all_ids if i.startswith("N-")]
        chk("markdown: no residual N- tags", not bad, f"{len(bad)} still N-")

    # ── universal polarity sweep across ALL id-bearing stores (the N- guard) ──
    offenders = []
    total_forbidden = 0
    for fpath in _idbearing_text_files(root):
        try:
            txt = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        c = len(forbidden.findall(txt))
        if c:
            total_forbidden += c
            offenders.append(f"{Path(fpath).name}:{c}")
    conn = sqlite3.connect(root / "stills" / "shared_vectors.db")
    try:
        for (bk,) in conn.execute("SELECT block_key FROM symbol_backmap"):
            if forbidden.search(bk or ""):
                total_forbidden += 1
    except sqlite3.OperationalError:
        offenders.append("symbol_backmap:MISSING")
    chk(f"ZERO {forbidden_name} across ALL id-bearing stores", total_forbidden == 0,
        f"{total_forbidden} found" + (f" in {offenders[:5]}" if offenders else ""))

    # ── (id+title) binding: derived keys ⊆ renumbered signal keys ──
    csp = root / "stills" / "concept_symbols.json"
    if csp.exists():
        cs = json.load(open(csp))
        keys = set(cs.get("by_concept", {}).keys())
        stale = [k for k in keys if k not in signal_keys]
        chk("concept_symbols keys ⊆ signal (id+title bound)", not stale,
            f"{len(keys)} keys, {len(stale)} stale" + (f" e.g. {stale[0][:50]!r}" if stale else ""))
    else:
        chk("concept_symbols.json present", False, "missing")
    try:
        bkeys = set(bk for (bk,) in conn.execute("SELECT DISTINCT block_key FROM symbol_backmap"))
        stale = [k for k in bkeys if k not in signal_keys]
        chk("symbol_backmap keys ⊆ signal (id+title bound)", not stale,
            f"{len(bkeys)} keys, {len(stale)} stale" + (f" e.g. {stale[0][:50]!r}" if stale else ""))
    except sqlite3.OperationalError as e:
        chk("symbol_backmap table present", False, str(e))

    # ── ID-FREE stores must stay clean (prove untouched) ──
    idfree_ok = True; detail = []
    for tbl, col in [("vectors", "title"), ("unified_concepts", "title")]:
        n = sum(1 for (v,) in conn.execute(f"SELECT {col} FROM {tbl} WHERE {col} IS NOT NULL")
                if ANY.search(v or ""))
        if n:
            idfree_ok = False; detail.append(f"{tbl}.{col}={n}")
    chk("ID-free DB stores still contain 0 concept ids", idfree_ok, ";".join(detail))
    conn.close()
    kg = root / "stills" / "knowledge_graph.json"
    if kg.exists():
        r = subprocess.run(["grep", "-cE", r"\[(N-)?(DT|DL|EX|HI|IF|PM|RL|RT|ST|TS|WF)-[0-9]{4,}\]", str(kg)],
                           capture_output=True, text=True)
        cnt = int((r.stdout or "0").strip() or 0)
        chk("knowledge_graph.json still ID-free", cnt == 0, f"matches={cnt}")

    # viewer: only meaningful in the final phase (regenerated from clean markdown).
    # Zero residual N- across viewer files, AND (id+title) BINDING via each still's
    # concepts.json — catches a stale/un-regenerated viewer even when old/new ids
    # overlap as strings.
    if expect == "final":
        import glob
        vfiles = [f for f in glob.glob(str(root / "viewer" / "**" / "*"), recursive=True)
                  if Path(f).is_file() and Path(f).suffix in (".html", ".json")]
        v_ntag = sum(len(NTAG.findall(Path(f).read_text(encoding="utf-8", errors="replace")))
                     for f in vfiles)
        chk("viewer: zero residual N-", v_ntag == 0, f"{v_ntag} N- tags")
        # binding: {still}::{title} from concepts.json must be ⊆ signal_keys.
        # Normalize by stripping inline <!-- ref:... --> comments (the viewer drops
        # them from displayed titles; the markdown keeps them on some header lines).
        import html
        _refc = re.compile(r"<!--.*?-->")
        _ws = re.compile(r"\s+")

        def _nt(s):  # normalize a title the way the viewer renders it (display cleaning)
            s = _refc.sub(" ", s)        # drop inline ref comments
            prev = None                   # unescape until stable (viewer double-escapes some entities)
            while prev != s:
                prev = s
                s = html.unescape(s)
            s = s.replace("*", "").replace("`", "").replace("_", "")  # md emphasis/code markers
            return _ws.sub(" ", s).strip()

        # only validate real concept-id titles; skip pre-existing malformed non-concept
        # headers (e.g. chunk-local '[C49.100-dup omitted]' dedup artifacts) that the
        # viewer's permissive splitter indexes but which were never concept ids.
        CID_TITLE = re.compile(r"^\[(?:N-)?(?:%s)-\d{4,}\]" % CONCEPT_PFX)
        signal_keys_norm = {_nt(k) for k in signal_keys}
        vkeys, vcount, vskipped = set(), 0, 0
        for cj in glob.glob(str(root / "viewer" / "fragments" / "*" / "concepts.json")):
            still = Path(cj).parent.name
            data = json.load(open(cj))
            entries = data if isinstance(data, list) else data.get("concepts", [])
            for e in entries:
                t = e.get("title") if isinstance(e, dict) else e
                if not t:
                    continue
                if not CID_TITLE.match(t.strip()):
                    vskipped += 1
                    continue
                vcount += 1
                vkeys.add(_nt(f"{still}::{t}"))
        stale = [k for k in vkeys if k not in signal_keys_norm]
        chk("viewer concepts ⊆ signal (id+title bound)", not stale,
            f"{vcount} viewer concepts, {len(stale)} stale, {vskipped} non-concept skipped"
            + (f" e.g. {stale[0][:50]!r}" if stale else ""))

    if ledger_path and Path(ledger_path).exists():
        led = json.load(open(ledger_path))
        plan_ids = set(led.get("remap", {}).values())  # these are N- tagged in the ledger
        cmp_ids = set(all_ids) if expect == "ntag" else {"N-" + i for i in all_ids}
        chk("markdown ids == ledger plan", cmp_ids == plan_ids,
            f"md={len(cmp_ids)} plan={len(plan_ids)} diff={len(cmp_ids ^ plan_ids)}")

    passed = all(ok for _, ok, _ in checks)
    print(f"\nVALIDATION ({expect}) {'PASSED' if passed else 'FAILED'} "
          f"({sum(ok for _,ok,_ in checks)}/{len(checks)} checks)")
    return 0 if passed else 1


def finalize(run_root: str) -> int:
    """Strip the transient N- tag from markdown headers (all copies). Derived stores
    are REGENERATED from the now-clean markdown by the orchestrator afterward."""
    root = Path(run_root)
    STRIP = re.compile(r"N-(?=(?:%s)-\d{4,})" % CONCEPT_PFX)
    files = 0; subs = 0
    for fmt in sorted(root.glob("stills/*/final/formatted")):
        for sub in [".", "signal", "forensics"]:
            for md in sorted((fmt / sub).glob("*.md")):
                t = md.read_text(encoding="utf-8")
                nt, c = STRIP.subn("", t)
                if c:
                    md.write_text(nt, encoding="utf-8")
                    files += 1; subs += c
    print(f"[finalize] stripped N- from {subs} ids across {files} markdown files")
    print("[finalize] NEXT: regenerate derived (concept_symbols, symbol_backmap, viewer) "
          "from clean markdown, then --validate --phase final")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Recast colliding concept IDs (non-destructive).")
    ap.add_argument("run_root")
    ap.add_argument("--commit", action="store_true", help="Snapshot + rewrite (N- tagged) + write ledger")
    ap.add_argument("--validate", action="store_true", help="Run the validation gate")
    ap.add_argument("--phase", default="final", choices=["ntag", "final"], help="validation phase")
    ap.add_argument("--finalize", action="store_true", help="Strip the transient N- tag from markdown")
    ap.add_argument("--ledger", default="", help="ledger path for plan-match check (with --validate)")
    ap.add_argument("--stills", default="", help="comma-separated still filter (default: all)")
    args = ap.parse_args()

    if args.validate:
        return validate(args.run_root, args.phase, args.ledger)
    if args.finalize:
        return finalize(args.run_root)
    stills_filter = set(s.strip() for s in args.stills.split(",") if s.strip()) or None

    ts = time.strftime("%Y%m%d-%H%M%S")
    # Vending: dry-run uses a throwaway ids.db; commit uses the run's real ids.db.
    import id_vending_machine as ivm
    import run_manager as rm
    if args.commit:
        rm.load(args.run_root)  # bind instance so get_default() resolves output:stills/ids.db
        vm = ivm.get_default()
        ids_db = "(run ids.db via run_manager)"
    else:
        import os
        import tempfile
        fd, ids_db = tempfile.mkstemp(suffix=".dryrun.ids.db")
        os.close(fd)
        vm = ivm.get_vending_machine(ids_db)

    remap, stats = build_remap(args.run_root, vm, stills_filter)

    print(f"=== ID RECAST {'COMMIT' if args.commit else 'DRY RUN'} ===  vending: {ids_db}")
    print(f"total concepts numbered: {stats['concepts']}")
    for still, s in sorted(stats["stills"].items()):
        print(f"  {still:16s} [{s['prefix']}] files={s['files']:2d} concepts={s['concepts']:6d} "
              f"new-id range=N-{s['prefix']}-{s['new_min']:04d}..N-{s['prefix']}-{s['new_max']:04d}")
    print(f"exact-duplicate content groups (merge candidates, flagged NOT merged): "
          f"{stats['merge_candidate_groups']} groups / {stats['merge_candidate_concepts']} concepts")
    if stats["dupe_examples"]:
        print("  merge-candidate examples (each line = one dup group's members):")
        for grp in stats["dupe_examples"]:
            members = ", ".join(f"{s}/{b}:{o}->{n}" for s, b, o, n in grp)
            print(f"     {members}")
    # UNIQUENESS GUARANTEE on the plan: distinct new ids must equal concept count.
    total_new = len(set(remap.values()))
    unique = (total_new == stats["concepts"])
    print(f"distinct new ids: {total_new}  concepts: {stats['concepts']}  "
          f"UNIQUE={'YES' if unique else 'NO — ABORT'}")
    if not unique:
        print("ERROR: renumber plan is not 1:1 unique; refusing to proceed.")
        return 2

    if not args.commit:
        print("\nDRY RUN complete. No files written. Re-run with --commit to apply.")
        return 0

    # COMMIT: bronze -> apply -> ledger
    dest = bronze(args.run_root, ts)
    print(f"[bronze] snapshot -> {dest}")
    applied = apply_remap(args.run_root, remap)
    print(f"[silver] rewrote headers: {json.dumps(applied)}")
    ledger = Path(args.run_root) / ".backup" / f"idmigration-ledger-{ts}.json"
    merge_groups = stats.pop("_merge_groups", {})
    ledger.write_text(json.dumps({
        "ts": ts, "run_id": rm.get_config().get("run_id"),
        "remap": {"|".join(k): v for k, v in remap.items()},
        "merge_candidate_groups": {ck: v for ck, v in merge_groups.items()},
        "stats": {k: v for k, v in stats.items() if k not in ("dupe_examples", "_merge_groups")},
    }, indent=2), encoding="utf-8")
    print(f"[ledger] {ledger}")
    print("\nCOMMIT complete. NEXT: regenerate derived (concept_symbols, symbol_backmap, "
          "viewer) then run validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
