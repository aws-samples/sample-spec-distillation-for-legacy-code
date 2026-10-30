#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Inject known defects into a copy of a corpus and write the ground truth.

    python3 eval/mutate/mutate.py --corpus synthetic-corpus/ap-invoices \
        --out /tmp/ap-mut --mutations 5 --seed 42

Produces::

    <out>/corpus/            the mutated source tree (answer key and README excluded)
    <out>/mutations.yaml     ground truth, in the same schema as the answer keys
    <out>/mutate_manifest.json   run provenance (timestamp, git commit, rejections)

Why this exists
---------------
Recall and precision say how much of a *known* spec a pipeline reproduces. They
say nothing about whether it would *notice a defect* that nobody told it about.
Mutation testing measures exactly that: inject a defect whose category is known,
then ask whether the pipeline's output flags it. ``score_mutations.py`` turns the
answer into a detection rate per category.

Guarantees
----------
* **Reproducible.** The same ``--corpus``, ``--mutations``, ``--seed`` and
  ``--categories`` produce byte-identical ``corpus/`` and ``mutations.yaml``.
  Non-deterministic provenance lives only in ``mutate_manifest.json``.
* **Syntax-preserving.** Every mutated file must pass ``sanity.check`` and its
  structural profile delta must equal the delta the mutation declared. A
  mutation that fails either test is rejected and recorded, never written.
* **Non-overlapping.** Two mutations may not touch the same line of the same
  file. The second one is rejected with ``reason: overlaps``.
* **Clear of known traps (``--avoid-key``).** With one or more answer keys, a
  mutation whose edits or ground-truth locations overlap a location of a key item
  carrying a trap flag (``--avoid-key-scope all``: any key item; optionally widened
  by ``--avoid-key-margin`` lines) is rejected with ``reason: overlaps_answer_key``. Otherwise a mutation can land on a site the
  corpus already seeded as a trap, and the control run "detects" it before any
  defect was injected (JC seed 7: MUT-002 sat on the seeded markup-cap
  inconsistency, a 16.7% control floor).
* **No LLM.** Mutation is pure text manipulation.
* **Languages (``--lang``).** ``vb`` = the VB.NET + T-SQL operators in ``mutations.py``;
  ``cs`` = the C# operators in ``mutations_cs.py`` (same eight category names, C#-aware site
  discovery; test projects, migrations and generated files are never mutated). ``auto``
  (default) picks ``cs`` when the tree has ``.cs`` files and no ``.vb`` files.
* **Roslyn parse check (C#, optional).** When the facts structure tool can run (``dotnet`` on
  PATH, or ``DISTILL_FACTS_CMD`` / ``--roslyn-cmd``), every touched file is parsed before and
  after mutation; a file whose Roslyn syntax-error count went up is rolled back and its
  mutations rejected as ``not_syntax_preserving``. Without a tool the check is recorded as
  ``skipped`` in the manifest; the tokeniser checks in ``sanity.py`` still apply.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutations as M                                     # noqa: E402
import mutations_cs as MC                                 # noqa: E402
import sanity                                             # noqa: E402

try:
    import yaml
except Exception:                                         # pragma: no cover
    yaml = None

SCHEMA_VERSION = 1
TOOL_VERSION = "1.0.0"
DEFAULT_EXCLUDE = ("ANSWER_KEY.yaml", "README.md")


# --------------------------------------------------------------------------- helpers
def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def corpus_hash(root: Path) -> str:
    """Content hash of a tree: sha256 over "relpath\\0filehash\\n" for every file, sorted."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(sha256_file(p).encode())
            h.update(b"\n")
    return h.hexdigest()


def git_commit(repo: Path) -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  text=True, timeout=20).stdout.strip()
        except Exception:
            return ""
    return {"commit": run("rev-parse", "HEAD"),
            "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain"))}


def copy_corpus(src: Path, dst: Path, exclude: tuple[str, ...]) -> list[str]:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    copied = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if any(part == e or rel == e for e in exclude for part in (p.name, rel)):
            continue
        if ".git" in p.relative_to(src).parts:              # a cloned repo's history is not corpus
            continue
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst / rel)
        copied.append(rel)
    return copied


# --------------------------------------------------------------------------- answer keys
def load_key_locations(paths: list[Path], scope: str = "flagged") -> dict[str, list[tuple[int, int, str]]]:
    """Locations of the answer-key items in ``scope``, as ``{normalised path: [(start, end,
    item id)]}``. ``flagged`` (default): items carrying a trap flag (``flags: [...]``), i.e.
    the problems the corpus seeded on purpose, which are what a control run already flags.
    ``all``: every item. Accepts ``locations: [{file, start_line, end_line}]`` and
    ``citations: ["file:a-b"]`` items."""
    import re
    out: dict[str, list[tuple[int, int, str]]] = {}
    for path in paths:
        if yaml is None:                                      # pragma: no cover
            raise SystemExit("--avoid-key needs PyYAML")
        if not Path(path).is_file():
            raise SystemExit(f"--avoid-key: no such file {path}")
        d = yaml.safe_load(Path(path).read_text()) or {}
        items = d.get("items") or [] if isinstance(d, dict) else []
        n = 0
        for it in items:
            if scope == "flagged" and not it.get("flags"):
                continue
            kid = str(it.get("id", "?"))
            for loc in it.get("locations") or []:
                a = int(loc["start_line"])
                out.setdefault(_norm(loc["file"]), []).append((a, int(loc.get("end_line") or a), kid))
                n += 1
            if not it.get("locations"):
                for c in it.get("citations") or []:
                    m = re.match(r"(.+?):(\d+)(?:-(\d+))?$", str(c))
                    if m:
                        a = int(m.group(2))
                        out.setdefault(_norm(m.group(1)), []).append((a, int(m.group(3) or a), kid))
                        n += 1
        if not n:
            raise SystemExit(f"--avoid-key {path}: no {scope} item locations found; refusing to treat "
                             f"an unreadable key as 'no traps'")
    return out


def _norm(f: str) -> str:
    return str(f).replace("\\", "/").lstrip("./").lower()


def key_clash(mut: M.Mutation, key_locs: dict, margin: int = 0) -> list[str]:
    """Key item ids whose locations overlap this mutation's edited lines or its
    ground-truth locations (each widened by ``margin``)."""
    spans: list[tuple[str, int, int]] = [(e.file, e.start, e.end) for e in mut.edits]
    spans += [(x["file"], int(x["start_line"]), int(x.get("end_line") or x["start_line"]))
              for x in mut.extra_locations]
    hits: set[str] = set()
    for f, a, b in spans:
        nf = _norm(f)
        cands = key_locs.get(nf)
        if cands is None:  # keys may cite by basename only
            base = nf.split("/")[-1]
            cands = [x for k, v in key_locs.items() if k.split("/")[-1] == base for x in v]
        for ka, kb, kid in cands:
            if a - margin <= kb and ka <= b + margin:
                hits.add(kid)
    return sorted(hits)


# --------------------------------------------------------------------------- selection
def select_mutations(corpus: M.Corpus, n: int, seed: int,
                     categories: list[str] | None = None,
                     key_locs: dict | None = None,
                     key_margin: int = 0, registry=M) -> tuple[list[M.Mutation], list[dict]]:
    """Pick up to ``n`` mutations, spread across categories, deterministically from ``seed``.

    Selection order: shuffle the category list with the seeded RNG, then take one
    mutation per category round-robin, each time taking the next site from that
    category's seeded-shuffled site list. Overlapping or structurally unsafe
    mutations are rejected and the next candidate is tried.
    """
    cats = list(categories or sorted(registry.OPERATORS))
    sites = registry.all_sites(corpus, cats)
    by_cat: dict[str, list[M.Site]] = {c: [] for c in cats}
    for s in sites:
        by_cat[s.category].append(s)

    rng = random.Random(seed)
    order = sorted(cats)
    rng.shuffle(order)
    for c in order:
        rng.shuffle(by_cat[c])

    rejected: list[dict] = []
    for c in order:
        if not by_cat[c]:
            rejected.append({"category": c, "reason": "no_applicable_sites",
                             "detail": "the corpus contains no site this operator can use"})

    chosen: list[M.Mutation] = []
    touched: dict[str, set[int]] = {}
    cursor = {c: 0 for c in cats}
    while len(chosen) < n:
        progressed = False
        for c in order:
            if len(chosen) >= n:
                break
            while cursor[c] < len(by_cat[c]):
                site = by_cat[c][cursor[c]]
                cursor[c] += 1
                progressed = True
                try:
                    mut = registry.OPERATORS[c][1](site, corpus)
                except Exception as exc:                      # operator could not build here
                    rejected.append({"category": c, "site": site.key,
                                     "reason": "build_failed", "detail": str(exc)})
                    continue
                if key_locs:
                    ids = key_clash(mut, key_locs, key_margin)
                    if ids:
                        rejected.append({"category": c, "site": site.key,
                                         "reason": "overlaps_answer_key",
                                         "detail": f"touches the location(s) of key item(s) "
                                                   f"{', '.join(ids[:8])}"})
                        continue
                clash = False
                for e in mut.edits:
                    if touched.get(e.file, set()) & e.touched:
                        rejected.append({"category": c, "site": site.key, "reason": "overlaps",
                                         "detail": f"{e.file} lines {e.start}-{e.end} already mutated"})
                        clash = True
                        break
                if clash:
                    continue
                for e in mut.edits:
                    touched.setdefault(e.file, set()).update(e.touched)
                chosen.append(mut)
                break
        if not progressed:
            break
    return chosen, rejected


# --------------------------------------------------------------------------- application
def apply_mutations(tree: Path, muts: list[M.Mutation]) -> tuple[list[M.Mutation], list[dict]]:
    """Apply edits file by file, recording post-mutation line ranges.

    Edits are applied in ascending line order with a running offset, so the
    ``locations`` recorded in the ground truth are coordinates in the *mutated*
    tree. A file whose structural profile moves further than its mutations
    declared, or which fails ``sanity.check``, is rolled back and its mutations
    are rejected.
    """
    by_file: dict[str, list[tuple[M.Mutation, M.Edit]]] = {}
    for m in muts:
        for e in m.edits:
            by_file.setdefault(e.file, []).append((m, e))

    rejected: list[dict] = []
    dropped: set[int] = set()
    for rel, pairs in sorted(by_file.items()):
        path = tree / rel
        original = path.read_text(errors="replace")
        before = sanity.profile(path)
        lines = original.split("\n")
        offset = 0
        for mut, e in sorted(pairs, key=lambda p: p[1].start):
            s, en = e.start + offset, e.end + offset
            lines[s - 1:en] = e.new_lines
            final_start = s
            final_end = s + len(e.new_lines) - 1 if e.new_lines else s - 1
            if e.loc_slice and e.new_lines:
                final_start = s + e.loc_slice[0]
                final_end = s + e.loc_slice[1]
            mut_locs = getattr(mut, "_locations", None)
            if mut_locs is None:
                mut_locs = []
                mut._locations = mut_locs                      # type: ignore[attr-defined]
            mut_locs.append({"file": rel,
                             "start_line": max(1, final_start),
                             "end_line": max(1, final_end if final_end >= final_start else final_start),
                             "original_start_line": e.start, "original_end_line": e.end,
                             "deleted": not e.new_lines})
            offset += len(e.new_lines) - (e.end - e.start + 1)
        path.write_text("\n".join(lines))

        declared: dict[str, int] = {}
        for mut, _ in pairs:
            for k, v in mut.expected_delta.get(rel, {}).items():
                declared[k] = declared.get(k, 0) + v
        after = sanity.profile(path)
        delta = {k: v for k, v in sanity.profile_delta(before, after).items()
                 if k.startswith(("vb_open_", "vb_close_", "sql_begin", "sql_end",
                                  "cs_method_decl", "cs_type_decl"))}
        ok, problems = sanity.check(path)
        if delta != {k: v for k, v in declared.items() if v} or not ok:
            path.write_text(original)
            for mut, _ in pairs:
                dropped.add(id(mut))
                rejected.append({"category": mut.category, "site": mut.site.key,
                                 "reason": "not_syntax_preserving",
                                 "detail": f"profile delta {delta} != declared {declared}; "
                                           f"problems: {problems[:3]}"})
    kept = [m for m in muts if id(m) not in dropped]
    return kept, rejected


# --------------------------------------------------------------------------- Roslyn parse check
def detect_lang(root: Path) -> str:
    """``cs`` when the tree has C# and no VB.NET, else ``vb`` (the original operators)."""
    has_cs = has_vb = False
    for p in root.rglob("*"):
        if ".git" in p.parts:
            continue
        s = p.suffix.lower()
        has_cs = has_cs or s == ".cs"
        has_vb = has_vb or s in sanity.VB_EXT
        if has_vb:
            return "vb"
    return "cs" if has_cs else "vb"


def resolve_roslyn(cmd: str | None) -> tuple[list[str] | None, str]:
    """Command prefix that takes a source root as its last argument and prints facts JSON
    (the facts structure tool, which reports ``parse_diagnostics``), plus how it was found."""
    import shlex
    if cmd:
        return shlex.split(cmd), "--roslyn-cmd"
    facts_dir = Path(__file__).resolve().parents[2] / "runtimes" / "facts"
    sys.path.insert(0, str(facts_dir))
    try:
        import build_facts                                  # noqa: E402
        return build_facts.resolve_tool(log=lambda m: None)
    except Exception as exc:                                 # pragma: no cover
        return None, f"facts tool unavailable: {exc}"


def _parse_errors(prefix: list[str], root: Path) -> dict[str, int] | None:
    try:
        r = subprocess.run(prefix + [str(root)], capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return None
        doc = json.loads(r.stdout)
    except Exception:
        return None
    if "parse_diagnostics" not in doc:
        return None                                          # an older tool build
    out: dict[str, int] = {}
    for d in doc["parse_diagnostics"]:
        out[d["file"]] = out.get(d["file"], 0) + 1
    return out


def roslyn_check(base: Path, tree: Path, files: list[str], prefix: list[str] | None,
                 how: str) -> dict:
    """Parse each touched C# file before and after mutation; report files whose Roslyn
    syntax-error count went up. Only the touched files are staged, so this is fast."""
    import tempfile
    cs_files = sorted(f for f in files if f.lower().endswith(".cs"))
    if not cs_files:
        return {"status": "not_applicable", "files_checked": 0}
    if prefix is None:
        return {"status": "skipped", "reason": how, "files_checked": 0}
    with tempfile.TemporaryDirectory() as td:
        res = {}
        for side, root in (("before", base), ("after", tree)):
            d = Path(td) / side
            for rel in cs_files:
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, d / rel)
            res[side] = _parse_errors(prefix, d)
    if res["before"] is None or res["after"] is None:
        return {"status": "skipped", "reason": f"tool did not return parse_diagnostics ({how})",
                "files_checked": 0}
    worse = {f: {"before": res["before"].get(f, 0), "after": n} for f, n in res["after"].items()
             if n > res["before"].get(f, 0)}
    return {"status": "ok" if not worse else "failed", "tool": how,
            "files_checked": len(cs_files), "new_errors": worse}


# --------------------------------------------------------------------------- ground truth
def ground_truth(corpus_name: str, muts: list[M.Mutation], seed: int,
                 base_root: Path, base_hash: str, mutated_hash: str) -> dict:
    items = []
    for i, m in enumerate(muts, 1):
        locs = [{"file": l["file"], "start_line": l["start_line"], "end_line": l["end_line"]}
                for l in getattr(m, "_locations", []) if not l["deleted"]]
        locs += [dict(x) for x in m.extra_locations]
        if not locs:  # deletion-only mutation: anchor on the line that follows the hole
            locs = [{"file": l["file"], "start_line": l["start_line"],
                     "end_line": l["start_line"]} for l in getattr(m, "_locations", [])]
        items.append({
            "id": f"MUT-{i:03d}",
            "type": m.item_type,
            "statement": m.statement,
            "locations": locs,
            "evidence_class": m.evidence_class,
            "flags": m.flags,
            "mutation": {
                "category": m.category,
                "detection_flag": M.CATEGORIES[m.category],
                "site": m.site.key,
                "edits": [{"file": e.file, "original_start_line": e.start,
                           "original_end_line": e.end, "new_line_count": len(e.new_lines)}
                          for e in m.edits],
                "applied_locations": getattr(m, "_locations", []),
                "before": m.before,
                "after": m.after,
                "detect_terms": m.detect_terms,
            },
        })
    return {
        "corpus": f"{corpus_name}-mutated",
        "base_corpus": base_root.as_posix(),
        "mutation_schema_version": SCHEMA_VERSION,
        "mutate_tool_version": TOOL_VERSION,
        "version": 1,
        "seed": seed,
        "base_corpus_sha256": base_hash,
        "mutated_corpus_sha256": mutated_hash,
        "categories": sorted({m.category for m in muts}),
        "items": items,
    }


def dump_yaml(obj) -> str:
    if yaml is None:                                          # pragma: no cover
        return json.dumps(obj, indent=2, sort_keys=True)
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, width=100,
                          allow_unicode=True)


# --------------------------------------------------------------------------- main
def run(corpus_root: Path, out: Path, n: int, seed: int, categories: list[str] | None,
        exclude: tuple[str, ...] = DEFAULT_EXCLUDE, strict: bool = False,
        avoid_keys: list[Path] | None = None, avoid_margin: int = 0,
        avoid_scope: str = "flagged", lang: str = "auto",
        roslyn_cmd: str | None = None, roslyn: bool = True) -> dict:
    if not corpus_root.is_dir():
        raise SystemExit(f"corpus not found: {corpus_root}")
    if lang == "auto":
        lang = detect_lang(corpus_root)
    registry = MC if lang == "cs" else M
    base_hash = corpus_hash(corpus_root)
    tree = out / "corpus"
    out.mkdir(parents=True, exist_ok=True)
    copied = copy_corpus(corpus_root, tree, exclude)
    if not copied:
        raise SystemExit(f"corpus is empty after --exclude: {corpus_root}")

    key_locs = load_key_locations(list(avoid_keys), avoid_scope) if avoid_keys else None
    c = MC.CsCorpus(tree) if lang == "cs" else M.Corpus(tree)
    muts, rejected = select_mutations(c, n, seed, categories, key_locs, avoid_margin, registry)
    muts, rej2 = apply_mutations(tree, muts)
    rejected += rej2

    touched = sorted({e.file for m in muts for e in m.edits})
    if roslyn and lang == "cs":
        prefix, how = resolve_roslyn(roslyn_cmd)
        rcheck = roslyn_check(corpus_root, tree, touched, prefix, how)
    else:
        rcheck = {"status": "not_applicable" if lang != "cs" else "disabled", "files_checked": 0}
    if rcheck.get("status") == "failed":
        bad = set(rcheck["new_errors"])
        # a mutation with an edit in a broken file is dropped, and so is every other edit it
        # made (possibly in other files): grow the set of files to restore to a fixed point
        while True:
            grown = bad | {e.file for m in muts if any(x.file in bad for x in m.edits)
                           for e in m.edits}
            if grown == bad:
                break
            bad = grown
        for rel in sorted(bad):
            shutil.copyfile(corpus_root / rel, tree / rel)
        for m in muts:
            if any(e.file in bad for e in m.edits):
                rejected.append({"category": m.category, "site": m.site.key,
                                 "reason": "not_syntax_preserving",
                                 "detail": "Roslyn reports new syntax errors in "
                                           f"{sorted(set(rcheck['new_errors']) & bad)}"})
        muts = [m for m in muts if not any(e.file in bad for e in m.edits)]

    ok, problems = sanity.check_tree(tree)
    mutated_hash = corpus_hash(tree)
    gt = ground_truth(corpus_root.name, muts, seed, corpus_root, base_hash, mutated_hash)
    (out / "mutations.yaml").write_text(
        "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
        "# SPDX-License-Identifier: MIT-0\n"
        "#\n"
        "# GROUND TRUTH for a mutated corpus. Generated by eval/mutate/mutate.py.\n"
        "# Schema matches the answer keys (items[{id,type,statement,locations,evidence_class,flags}])\n"
        "# so eval/score.py can consume this file directly as --key. The extra `mutation:` block\n"
        "# is ignored by score.py and used by eval/mutate/score_mutations.py.\n"
        "# Keep this file OUT of the corpus directory.\n"
        + dump_yaml(gt))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": git_commit(Path(__file__).resolve().parents[2]),
        "tool_version": TOOL_VERSION,
        "mutation_schema_version": SCHEMA_VERSION,
        "command": {"corpus": corpus_root.as_posix(), "out": out.as_posix(),
                    "mutations": n, "seed": seed,
                    "categories": categories or sorted(registry.OPERATORS),
                    "lang": lang,
                    "exclude": list(exclude),
                    "avoid_key": [str(k) for k in avoid_keys or []],
                    "avoid_key_margin": avoid_margin,
                    "avoid_key_scope": avoid_scope if avoid_keys else None},
        "avoid_key_sha256": {str(k): sha256_file(Path(k)) for k in avoid_keys or []},
        "requested": n,
        "applied": len(muts),
        "files_copied": len(copied),
        "base_corpus_sha256": base_hash,
        "mutated_corpus_sha256": mutated_hash,
        "mutations_yaml_sha256": sha256_file(out / "mutations.yaml"),
        "lang": lang,
        "roslyn_check": rcheck,
        "sanity_ok": ok,
        "sanity_problem_count": len(problems),
        "rejected_count": len(rejected),
        "sanity_problems": problems,
        "rejected": rejected,
        "python": sys.version.split()[0],
    }
    (out / "mutate_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if strict and (len(muts) < n or not ok):
        raise SystemExit(f"--strict: applied {len(muts)}/{n}, sanity_ok={ok}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True, type=Path, help="source corpus root (read-only)")
    ap.add_argument("--out", type=Path, help="output dir: corpus/ + mutations.yaml "
                                             "(not needed with --list-sites)")
    ap.add_argument("--mutations", type=int, default=5, help="how many defects to inject")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed; same seed = identical output")
    ap.add_argument("--categories", help="comma-separated subset of categories (default: all)")
    ap.add_argument("--exclude", action="append", default=[],
                    help=f"file/basename to keep out of the copied corpus "
                         f"(default: {', '.join(DEFAULT_EXCLUDE)}); repeatable")
    ap.add_argument("--avoid-key", action="append", default=[], type=Path, metavar="KEY.yaml",
                    help="answer key whose item locations are off limits: a mutation that overlaps "
                         "one is rejected (reason overlaps_answer_key), so it cannot collide with a "
                         "trap the corpus already seeded; repeatable")
    ap.add_argument("--avoid-key-scope", choices=["flagged", "all"], default="flagged",
                    help="which key items are off limits: flagged (default) = items with a trap flag, "
                         "the corpus's seeded problems; all = every key item (on a densely keyed "
                         "corpus such as JC this leaves too few sites)")
    ap.add_argument("--avoid-key-margin", type=int, default=0, metavar="N",
                    help="widen every key location by N lines on each side (default 0)")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if fewer than --mutations applied or sanity failed")
    ap.add_argument("--list-sites", action="store_true", help="print applicable sites and exit")
    ap.add_argument("--lang", choices=["auto", "vb", "cs"], default="auto",
                    help="operator set: vb = VB.NET + T-SQL, cs = C#; auto (default) = cs when the "
                         "tree has .cs and no .vb files")
    ap.add_argument("--roslyn-cmd", metavar="CMD",
                    help="C#: command that takes a source root as its last argument and prints "
                         "facts JSON with parse_diagnostics (default: the facts structure tool via "
                         "dotnet or DISTILL_FACTS_CMD; skipped when neither is available)")
    ap.add_argument("--no-roslyn", action="store_true", help="C#: skip the Roslyn parse check")
    a = ap.parse_args()

    cats = [x.strip() for x in a.categories.split(",")] if a.categories else None
    if a.list_sites:
        lang = detect_lang(a.corpus) if a.lang == "auto" else a.lang
        reg = MC if lang == "cs" else M
        c = MC.CsCorpus(a.corpus) if lang == "cs" else M.Corpus(a.corpus)
        counts: dict[str, int] = {}
        for s in reg.all_sites(c, cats):
            counts[s.category] = counts.get(s.category, 0) + 1
            print(f"{s.category:24s} {s.file}:{s.line}  {s.key}")
        print("\n" + json.dumps(counts, indent=2))
        return 0

    if a.out is None:
        ap.error("--out is required")
    man = run(a.corpus, a.out, a.mutations, a.seed, cats,
              tuple(a.exclude) if a.exclude else DEFAULT_EXCLUDE, a.strict,
              a.avoid_key, a.avoid_key_margin, a.avoid_key_scope, a.lang, a.roslyn_cmd,
              not a.no_roslyn)
    print(json.dumps({k: man[k] for k in ("requested", "applied", "sanity_ok",
                                          "base_corpus_sha256", "mutated_corpus_sha256",
                                          "mutations_yaml_sha256")}, indent=2))
    for r in man["rejected"]:
        print(f"rejected: {r['category']} {r.get('site', '')} -> {r['reason']}")
    print(f"wrote {a.out}/corpus, {a.out}/mutations.yaml, {a.out}/mutate_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
