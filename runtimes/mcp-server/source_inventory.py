#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""source_inventory.py — Phase 0 enumeration + SOURCE IDENTITY + stage-identity stamps.

WHY: reuse used to be decided by file EXISTENCE (candidates.txt, page_*/concat.txt, .complete,
master_blend.md, ...). A changed, deleted or added source file therefore silently reused stale
pages and every downstream artifact up to bottled_spec.md. This module binds reuse to content:

  enumerate   walk the source tree, apply the (hashed) filter rules, and write
                candidates.txt          sorted relative paths that will be paged
                excluded.tsv            every filtered path + an explicit reason (never silent)
                source_inventory.json   [{path,size,sha256}], inventory_hash, rules_hash,
                                        code/config versions, and source_identity
  diff        compare two inventories -> added / changed / deleted (for the log)
  stage-id    sha256(upstream id + code file hashes + config k=v) — chained per stage, so a
              change anywhere upstream changes every downstream id (mismatch => recompute).

source_identity = sha256(inventory_hash, rules_hash, versions of the scripts that turn source into
pages (enumeration, ingest helpers, concat-batch, plate strategy), ingest helper availability,
and caller config such as token budget + plate strategy). Prompt versions enter the chain later via
run_identity (preamble + per-still prompt hashes), which keys the map/reduce stage.

Git input (DISTILL_GIT_URL, source_fetch.py): the resolved commit SHA and subfolder enter the
identity as config (git_commit=, git_subdir=), so the same SHA reuses every stage and a new commit
invalidates; the provenance (URL without credentials, ref, SHA, subdir) is recorded as
source_provenance. .git/ directories are pruned from the walk and listed once in excluded.tsv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA = 1
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# Filter rules (ordered; first match wins). Applied to the path RELATIVE to the source root, so a
# source root that itself lives under e.g. /build/ is not wholesale excluded. .rdl/.rdlc/.dtsx are
# XML text and are NOT filtered (they page as raw). .rpt is filtered unless the JRC helper is
# available (see enumerate_source).
EXCLUDE_RULES: list[tuple[str, str]] = [
    (r"\.(png|jpg|jpeg|gif|svg|ico|webp|bmp|woff2?|ttf|eot|otf)$", "binary image/font asset"),
    (r"\.(zip|tar|gz|bz2|7z|rar|pyc|pyo|class|so|dll|exe)$", "archive or compiled binary"),
    (r"\.(pdf|doc|docx|xls|xlsx|lock|map|min\.js|min\.css)$", "binary document, lockfile or minified bundle"),
    (r"\.(pbix|twbx|qvw|dtsConfig|ispac)$", "packaged BI/ETL binary (no ingest helper)"),
    (r"\.(db|sqlite|mdb|accdb|ldf|mdf|bak|dat|bin|obj|pdb)$", "database or opaque binary"),
    (r"(^|/)node_modules/", "vendored dependency dir (node_modules)"),
    (r"(^|/)\.git(/|$)", "VCS metadata (.git)"),
    (r"(^|/)__pycache__/", "python bytecode cache"),
    (r"(^|/)(\.venv|venv|dist|build|coverage|\.next|vendor)/", "build/vendor/venv output dir"),
    (r"(\.DS_Store|Thumbs\.db)$", "OS metadata file"),
]
# Directories pruned from the walk (not descended into); each is listed once in excluded.tsv.
PRUNE_DIRS = {".git": "VCS metadata (.git)"}
RPT_RULE = r"\.rpt$"
RPT_REASON_NO_JRC = ("Crystal Reports binary; RptJrcHelper unavailable ({why}) — set RPT_JRC_JAVA + "
                     "RPT_JRC_CLASSPATH to lift report definitions")

# Scripts whose code shapes the pages; their content hashes are part of source_identity.
PAGE_CODE_FILES = [
    HERE / "source_inventory.py",
    HERE / "ingest_helpers.py",
    HERE / "plate_strategy.py",
    REPO / "runtimes" / "pipeline" / "concat-batch.sh",
]


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def code_version(path: str | Path) -> str:
    p = Path(path)
    return sha256_file(p) if p.is_file() else "absent"


def rules_hash(include_rpt: bool) -> str:
    return sha256_text(json.dumps({"rules": EXCLUDE_RULES, "rpt_rule": RPT_RULE,
                                   "prune_dirs": PRUNE_DIRS, "include_rpt": include_rpt},
                                  sort_keys=True))


def _rpt_helper_status() -> tuple[bool, str]:
    try:
        import ingest_helpers as IH
        h = next((x for x in IH.registry() if isinstance(x, IH.RptJrcHelper)), None)
        if h is None:
            return False, "helper not registered"
        return h.available()
    except Exception as e:  # pragma: no cover - defensive
        return False, f"ingest_helpers import failed: {e}"


def enumerate_source(source_root: str) -> tuple[list[str], list[tuple[str, str]], dict]:
    """Walk source_root -> (candidates, excluded[(path, reason)], meta). Deterministic order."""
    root = Path(source_root)
    rpt_ok, rpt_why = _rpt_helper_status()
    compiled = [(re.compile(rx), reason) for rx, reason in EXCLUDE_RULES]
    rpt_rx = re.compile(RPT_RULE)
    candidates: list[str] = []
    excluded: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        # VCS metadata is never source: prune .git/ (a git input clones into <output>/.source/,
        # which holds one) and list it once in excluded.tsv instead of walking every object file.
        for d in [d for d in dirnames if d in PRUNE_DIRS]:
            dirnames.remove(d)
            rel_d = os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/")
            excluded.append((rel_d + "/", PRUNE_DIRS[d]))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue  # matches `find -type f` (symlinks are not regular files)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            reason = None
            for rx, why in compiled:
                if rx.search(rel):
                    reason = why
                    break
            if reason is None and rpt_rx.search(rel) and not rpt_ok:
                reason = RPT_REASON_NO_JRC.format(why=rpt_why)
            if reason is None:
                candidates.append(rel)
            else:
                excluded.append((rel, reason))
    candidates.sort()
    excluded.sort()
    return candidates, excluded, {"include_rpt": rpt_ok, "rpt_helper": rpt_why or "available"}


def build_inventory(source_root: str, candidates: list[str], meta: dict,
                    config: dict | None = None, provenance: dict | None = None) -> dict:
    """provenance (git input: url without credentials, ref, resolved commit, subdir) is recorded
    as-is; the parts that select the content (commit, subdir) reach the identity through config
    (run_distillation.sh passes git_commit= / git_subdir=)."""
    entries = []
    for rel in candidates:
        full = os.path.join(source_root, rel)
        try:
            entries.append({"path": rel, "size": os.path.getsize(full), "sha256": sha256_file(full)})
        except OSError as e:
            entries.append({"path": rel, "size": -1, "sha256": f"unreadable:{type(e).__name__}"})
    inv_hash = sha256_text("".join(f"{e['path']}\t{e['size']}\t{e['sha256']}\n" for e in entries))
    versions = {str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p): code_version(p)
                for p in PAGE_CODE_FILES}
    try:
        import ingest_helpers as IH
        versions["ingest_helpers.HELPER_VERSIONS"] = IH.helper_versions()
    except Exception:
        pass
    cfg = dict(sorted((config or {}).items()))
    rh = rules_hash(meta.get("include_rpt", False))
    identity = sha256_text(json.dumps({"schema": SCHEMA, "inventory_hash": inv_hash,
                                       "rules_hash": rh, "versions": versions,
                                       "include_rpt": meta.get("include_rpt", False),
                                       "config": cfg}, sort_keys=True))
    return {"schema": SCHEMA, "file_count": len(entries), "inventory_hash": inv_hash,
            "rules_hash": rh, "include_rpt": meta.get("include_rpt", False),
            "rpt_helper": meta.get("rpt_helper"), "versions": versions, "config": cfg,
            "source_identity": identity, "source_provenance": provenance or None,
            "entries": entries}


def diff_inventories(old: dict | None, new: dict) -> dict:
    o = {e["path"]: e["sha256"] for e in (old or {}).get("entries", [])}
    n = {e["path"]: e["sha256"] for e in new.get("entries", [])}
    return {"added": sorted(set(n) - set(o)), "deleted": sorted(set(o) - set(n)),
            "changed": sorted(p for p in set(o) & set(n) if o[p] != n[p])}


def stage_id(upstream: str, code: list[str], config: list[str]) -> str:
    parts = {"upstream": upstream,
             "code": {Path(c).name + ":" + str(i): code_version(c) for i, c in enumerate(code)},
             "config": sorted(config)}
    return sha256_text(json.dumps(parts, sort_keys=True))


def _parse_kv(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Source inventory + stage identity")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enumerate", help="enumerate source -> candidates/excluded/inventory")
    e.add_argument("source_root")
    e.add_argument("--out-dir", required=True)
    e.add_argument("--suffix", default="", help="filename suffix (e.g. .next) for staged writes")
    e.add_argument("--config", nargs="*", default=[], help="k=v config entries in the identity")
    e.add_argument("--provenance", default="", help="JSON file with source provenance (git input) to record")

    d = sub.add_parser("diff", help="diff two inventories (old may be missing)")
    d.add_argument("old")
    d.add_argument("new")

    s = sub.add_parser("stage-id", help="print a chained stage identity")
    s.add_argument("--upstream", default="")
    s.add_argument("--code", nargs="*", default=[])
    s.add_argument("--config", nargs="*", default=[])

    g = sub.add_parser("get", help="print one top-level field of an inventory JSON")
    g.add_argument("inventory")
    g.add_argument("field")

    args = ap.parse_args()
    if args.cmd == "enumerate":
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        cands, excl, meta = enumerate_source(args.source_root)
        prov = json.loads(Path(args.provenance).read_text()) if args.provenance else None
        inv = build_inventory(args.source_root, cands, meta, _parse_kv(args.config), prov)
        sfx = args.suffix
        (out / f"candidates.txt{sfx}").write_text("".join(c + "\n" for c in cands), encoding="utf-8")
        (out / f"excluded.tsv{sfx}").write_text("".join(f"{p}\t{r}\n" for p, r in excl),
                                                 encoding="utf-8")
        (out / f"source_inventory.json{sfx}").write_text(json.dumps(inv, indent=1), encoding="utf-8")
        by_reason: dict[str, int] = {}
        for _, r in excl:
            by_reason[r] = by_reason.get(r, 0) + 1
        print(json.dumps({"candidates": len(cands), "excluded": len(excl),
                          "excluded_by_reason": by_reason,
                          "source_identity": inv["source_identity"]}))
        return 0
    if args.cmd == "diff":
        old = None
        if os.path.exists(args.old):
            try:
                old = json.loads(Path(args.old).read_text())
            except Exception:
                old = None
        new = json.loads(Path(args.new).read_text())
        print(json.dumps(diff_inventories(old, new)))
        return 0
    if args.cmd == "stage-id":
        print(stage_id(args.upstream, args.code, args.config))
        return 0
    if args.cmd == "get":
        try:
            print(json.loads(Path(args.inventory).read_text()).get(args.field, ""))
        except Exception:
            print("")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
