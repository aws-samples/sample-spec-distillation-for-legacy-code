#!/usr/bin/env python3
"""run_identity.py — a run's self-verifying IDENTITY, so a recorded strategy is authority ONLY
when the artifact PROVES it's the same run (door C; epoch cut 2026-09-12).

WHY (SYSTEM_SPEC §7 registries; L6 deterministic-stable-identity; L8 observability). A run's
output is a function of THREE inputs, not one:
  1. the pages   (the input corpus)           — run-wide
  2. the preamble (STEERING_PREAMBLE steering) — run-wide, strategy-bound
  3. the still prompt (per still)              — per-still
A recorded `orchestration_strategy` in a run manifest is only trustworthy as a RESUME signal if
those inputs are unchanged. Presence of a recorded value carries NO authority on its own — after
months of runs across changing regimes, a found manifest is more likely vestigial than a genuine
resume. So identity is EARNED by the artifact matching, not asserted by a flag and not assumed
from a coincidental value-match.

IDENTITY (schema 2), two levels + the steering/prompt hashes:
  fingerprint (per page) = sha256(concat bytes) for each page_NNN — recorded, individually
                           referenceable so a diff can name WHICH page drifted.
  palm_print (whole run) = sha256 of the per-page fingerprints joined in EXPLICIT lexicographic
                           page_NNN order. Determinism is load-bearing: page_NNN is zero-padded so
                           lexicographic == numeric, and we SORT explicitly (never trust
                           iterdir/glob order) — otherwise the same pages could hash differently.
  preamble_hash          = sha256(STEERING_PREAMBLE) — the WHOLE composed steering prefix (all
                           blocks), so any steering change invalidates stale extracts AND explains
                           the cache-prefix change in telemetry.
  still_prompt_hashes    = {name: sha256(prompt_for(name))} — per-still, so a single still's prompt
                           change invalidates ONLY that still, not the whole run.
  facts_hash             = sha256 over the Phase 1.2 structural-facts blocks appended to each
                           page in the map prompt (None when facts are off or unavailable), so
                           turning facts on/off or a facts change re-maps every still.
  identifiers_hash       = the same for the Phase 1.3 identifier-evidence blocks.
  + orchestration_strategy, plate_strategy (recorded for provenance).

RESUME GATE (compositional, per-still — NOT one boolean):
  - no run_identity block            -> ORPHAN: never inherit a recorded strategy; fresh run.
  - source_inventory_hash mismatch   -> source tree changed: NOTHING resumes.
  - palm mismatch                    -> corpus changed: NOTHING resumes (report which pages).
  - facts_hash mismatch              -> map prompt facts changed: NOTHING resumes.
  - palm ok, preamble mismatch       -> every still stale (steering changed): all re-map.
  - palm ok, preamble ok             -> still S resumes iff still_prompt_hashes[S] matches;
                                        only mismatched stills re-map.

CLI:
    run_identity.py compute <pages_dir> --stills a,b,c [--orchestration S] [--plate S]
    run_identity.py record  <manifest.json> <pages_dir> --stills a,b,c [--orchestration S] [--plate S]
    run_identity.py verdict <manifest.json> <pages_dir> --stills a,b,c   # prints per-still verdict JSON
    run_identity.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# Self-bootstrap: ensure this module's own dir is importable so the sibling engine modules resolve
# regardless of HOW run_identity is loaded (direct CLI, importlib.spec_from_file_location from an
# orchestrator heredoc, etc.) — the same "engine knows its own location" idiom used elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import orchestration_strategy  # noqa: E402
import plate_strategy  # noqa: E402
import still_registry  # noqa: E402

SCHEMA = 2


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _sha256_text(s: str) -> str:
    return _sha256_bytes(s.encode("utf-8"))


def _page_concat(page_dir: Path) -> Path | None:
    """The page's concat file. Canonical current-epoch name is `concat.txt` (concat-batch.sh
    writes .txt; every live consumer keys on it). `concat.md` exists ONLY in pre-epoch vestigial
    runs, which are quarantined and never identity-checked — so we read .txt only, no fallback."""
    p = page_dir / "concat.txt"
    return p if p.is_file() else None


def page_order(pages_dir: str | Path) -> list[str]:
    """Explicit lexicographic order of page_NNN dirs (determinism is load-bearing — we SORT,
    never rely on filesystem iteration order). Zero-padded names => lexicographic == numeric."""
    root = Path(pages_dir)
    if not root.is_dir():
        return []
    names = [d.name for d in root.iterdir()
             if d.is_dir() and d.name.startswith("page_") and _page_concat(d) is not None]
    return sorted(names)


def page_fingerprints(pages_dir: str | Path) -> dict[str, str]:
    """{page_NNN: sha256(concat bytes)} for every page (in no particular dict order; the palm
    print imposes order). Reading concat bytes once per run at Phase 0 — not per call."""
    root = Path(pages_dir)
    out: dict[str, str] = {}
    for name in page_order(root):
        cf = _page_concat(root / name)
        out[name] = _sha256_bytes(cf.read_bytes())
    return out


def palm_print(fingerprints: dict[str, str], order: list[str]) -> str:
    """Hash-of-hashes over the fingerprints in EXPLICIT page order (the whole-run identity)."""
    joined = "\n".join(f"{p}\t{fingerprints[p]}" for p in order)
    return _sha256_text(joined)


def preamble_hash() -> str:
    """Hash the WHOLE composed steering prefix (all blocks), not any single named block."""
    return _sha256_text(orchestration_strategy.STEERING_PREAMBLE)


def still_prompt_hashes(names: list[str]) -> dict[str, str]:
    """{still: sha256(prompt_for(still))} — per-still so invalidation localizes to the changed still."""
    return {n: _sha256_text(still_registry.prompt_for(n)) for n in names}


def compute_identity(
    pages_dir: str | Path,
    stills: list[str],
    orchestration: str | None = None,
    plate: str | None = None,
    source_inventory_hash: str | None = None,
    facts_hash: str | None = None,
    identifiers_hash: str | None = None,
) -> dict:
    """Compute the full run_identity block for the current inputs.

    source_inventory_hash (from source_inventory.json) binds the identity to the SOURCE tree, not
    just to the pages: pages are a derived artifact and fingerprinting them alone let stale pages
    "match" after the source changed (pipeline-assessment Blocker 2)."""
    order = page_order(pages_dir)
    fps = page_fingerprints(pages_dir)
    names = stills or still_registry.list_extraction_stills()
    # plate_code_hash: behavioral identity of the racking ALGORITHM (recipe), so drift under a
    # stable strategy name is caught even when this corpus's pages coincidentally match (palm =
    # the dish; this = the recipe). Best-effort — if the plate name is unknown/unavailable, record
    # None rather than fail identity computation.
    plate_code = None
    if plate:
        try:
            plate_code = plate_strategy.code_hash(plate)
        except Exception:
            plate_code = None
    return {
        "schema": SCHEMA,
        "page_order": order,
        "page_fingerprints": fps,
        "palm_print": palm_print(fps, order),
        "preamble_hash": preamble_hash(),
        "still_prompt_hashes": still_prompt_hashes(names),
        "orchestration_strategy": orchestration,
        "plate_strategy": plate,
        "plate_code_hash": plate_code,
        "source_inventory_hash": source_inventory_hash,
        "facts_hash": facts_hash,
        "identifiers_hash": identifiers_hash,
    }


def record_identity(manifest_path: str | Path, identity: dict) -> None:
    """Write/merge the run_identity block into the run manifest (create if absent)."""
    path = Path(manifest_path)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
    data["run_identity"] = identity
    # keep the flat strategy keys in sync for humans / existing readers
    if identity.get("orchestration_strategy"):
        data["orchestration_strategy"] = identity["orchestration_strategy"]
    if identity.get("plate_strategy"):
        data["plate_strategy"] = identity["plate_strategy"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def diff(recorded: dict | None, current: dict) -> dict:
    """Compare a recorded run_identity against the current one -> a per-still resume verdict.

    Returns:
      {
        "state": "orphan" | "palm_mismatch" | "preamble_mismatch" | "match",
        "resumable": [still, ...],            # stills whose existing extracts may be reused
        "invalidated": [still, ...],          # stills that MUST re-map
        "changed_pages": [page, ...],         # only on palm_mismatch
        "recorded_strategy": <str|None>,      # always reported (detection != authority)
        "detail": "<human summary>",
      }
    """
    cur_stills = list(current.get("still_prompt_hashes", {}).keys())

    # ORPHAN: no recorded identity block (all pre-epoch runs + sediment). No authority.
    if not recorded or recorded.get("schema") is None:
        return {
            "state": "orphan",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": [],
            "recorded_strategy": (recorded or {}).get("orchestration_strategy"),
            "detail": "no run_identity block — orphan run; recorded strategy (if any) NOT inherited",
        }

    rec_strategy = recorded.get("orchestration_strategy")

    # PLATE CODE: behavioral identity of the racking algorithm (recipe). A mismatch means the
    # strategy CODE changed under a possibly-identical name — a provenance change even if palm
    # coincidentally matches. Hard gate: invalidate all. Only fires when BOTH sides recorded a
    # plate_code_hash (else it's not in play — no false alarm). This is checked BEFORE palm so the
    # surfaced reason is the recipe, not the (possibly-unchanged) dish.
    rec_plate_code = recorded.get("plate_code_hash")
    cur_plate_code = current.get("plate_code_hash")
    if rec_plate_code and cur_plate_code and rec_plate_code != cur_plate_code:
        return {
            "state": "plate_code_mismatch",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": [],
            "recorded_strategy": rec_strategy,
            "detail": (f"plate strategy CODE changed (recorded plate={recorded.get('plate_strategy')!r} "
                       f"code drifted) — racking algorithm differs; nothing resumes"),
        }

    # SOURCE: the source tree itself changed (content/size/add/delete). Pages may coincidentally
    # match (e.g. a referential file's bytes changed but its marker did not) — still a different
    # run. A recorded identity WITHOUT a source hash (pre-fix run) cannot prove the source, so it
    # is treated as a mismatch whenever the current run has one.
    rec_src = recorded.get("source_inventory_hash")
    cur_src = current.get("source_inventory_hash")
    if cur_src and rec_src != cur_src:
        return {
            "state": "source_mismatch",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": [],
            "recorded_strategy": rec_strategy,
            "detail": ("source inventory changed (or was not recorded) since the recorded run — "
                       "nothing resumes"),
        }

    # PALM: whole-corpus identity. Mismatch => nothing resumes; name which pages differ.
    if recorded.get("palm_print") != current.get("palm_print"):
        rec_fps = recorded.get("page_fingerprints", {})
        cur_fps = current.get("page_fingerprints", {})
        changed = sorted(set(rec_fps) | set(cur_fps))
        changed_pages = [p for p in changed if rec_fps.get(p) != cur_fps.get(p)]
        return {
            "state": "palm_mismatch",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": changed_pages,
            "recorded_strategy": rec_strategy,
            "detail": f"page corpus changed ({len(changed_pages)} page(s) differ) — nothing resumes",
        }

    # FACTS: the structural-facts blocks are part of every map prompt. A recorded identity without
    # the key (pre-facts run) equals None, so it matches a run that has facts off.
    if recorded.get("facts_hash") != current.get("facts_hash"):
        return {
            "state": "facts_mismatch",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": [],
            "recorded_strategy": rec_strategy,
            "detail": "structural facts in the map prompt changed (or were turned on/off) — nothing resumes",
        }

    # IDENTIFIER EVIDENCE (Phase 1.3): also part of every map prompt; same rule as facts (absent on
    # a recorded pre-identifiers identity == None == identifiers off).
    if recorded.get("identifiers_hash") != current.get("identifiers_hash"):
        return {
            "state": "identifiers_mismatch",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": [],
            "recorded_strategy": rec_strategy,
            "detail": "identifier evidence in the map prompt changed (or was turned on/off) — nothing resumes",
        }

    # PREAMBLE: run-wide steering. Mismatch => every still stale.
    if recorded.get("preamble_hash") != current.get("preamble_hash"):
        return {
            "state": "preamble_mismatch",
            "resumable": [],
            "invalidated": cur_stills,
            "changed_pages": [],
            "recorded_strategy": rec_strategy,
            "detail": "STEERING_PREAMBLE changed — all stills stale, must re-map",
        }

    # Per-still: pages + preamble match; a still resumes iff its prompt hash matches.
    rec_ph = recorded.get("still_prompt_hashes", {})
    cur_ph = current.get("still_prompt_hashes", {})
    resumable, invalidated = [], []
    for name in cur_stills:
        if name in rec_ph and rec_ph[name] == cur_ph[name]:
            resumable.append(name)
        else:
            invalidated.append(name)
    return {
        "state": "match",
        "resumable": resumable,
        "invalidated": invalidated,
        "changed_pages": [],
        "recorded_strategy": rec_strategy,
        "detail": (f"corpus+preamble match; {len(resumable)} still(s) resumable, "
                   f"{len(invalidated)} invalidated by prompt change"),
    }


def read_recorded(manifest_path: str | Path) -> dict | None:
    path = Path(manifest_path)
    if not path.exists():
        return None  # legitimately no manifest → caller treats as orphan (fresh run)
    # A manifest that EXISTS but can't be parsed is NOT the same as "no manifest" — silently
    # returning None would misclassify a corrupt/unreadable manifest as a clean orphan (exactly
    # the recorded-value-honesty failure this module exists to prevent). Surface it loudly; the
    # caller must decide, not have the decision made by a swallowed parse error.
    try:
        text = path.read_text()
    except OSError as e:
        raise RuntimeError(f"run manifest exists but is unreadable: {path} ({e})") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"run manifest exists but is not valid JSON: {path} ({e}) — "
                           f"refusing to silently treat a corrupt manifest as a fresh orphan") from e
    return data.get("run_identity")  # None here = valid manifest, no run_identity block (real orphan)


# ── selftest ─────────────────────────────────────────────────────────────────
def _selftest() -> int:
    import tempfile
    d = tempfile.mkdtemp(prefix="runid_")
    pages = Path(d) / ".pages"
    for i in range(3):
        pd = pages / f"page_{i:03d}"
        pd.mkdir(parents=True)
        (pd / "concat.txt").write_text(f"PAGE {i} content\n", encoding="utf-8")

    # determinism: order is explicit-sorted, palm stable across recompute
    o1 = page_order(pages); o2 = page_order(pages)
    assert o1 == o2 == ["page_000", "page_001", "page_002"], f"order not deterministic: {o1}"
    fps = page_fingerprints(pages)
    assert palm_print(fps, o1) == palm_print(page_fingerprints(pages), o2), "palm not stable"

    # use real registry stills so prompt hashes resolve
    stills = still_registry.list_extraction_stills()[:2]
    ident = compute_identity(pages, stills, orchestration="page-centric", plate="single-plate")
    assert ident["schema"] == SCHEMA and ident["palm_print"].startswith("sha256:")

    # orphan: no recorded -> orphan, nothing resumable
    v = diff(None, ident)
    assert v["state"] == "orphan" and v["resumable"] == [] and v["invalidated"] == stills

    # match: identical -> all resumable
    v = diff(ident, ident)
    assert v["state"] == "match" and sorted(v["resumable"]) == sorted(stills), v

    # palm mismatch: change a page -> nothing resumes, page named
    (pages / "page_001" / "concat.txt").write_text("PAGE 1 CHANGED\n", encoding="utf-8")
    ident2 = compute_identity(pages, stills, orchestration="page-centric", plate="single-plate")
    v = diff(ident, ident2)
    assert v["state"] == "palm_mismatch" and "page_001" in v["changed_pages"], v

    # preamble mismatch: same pages, different preamble hash -> all stale
    ident3 = dict(ident2)
    rec = dict(ident2); rec["preamble_hash"] = "sha256:deadbeef"
    v = diff(rec, ident2)
    assert v["state"] == "preamble_mismatch" and v["invalidated"] == stills, v

    # per-still: flip ONE still's prompt hash -> only that still invalidated
    if len(stills) >= 2:
        rec = json.loads(json.dumps(ident2))
        rec["still_prompt_hashes"][stills[0]] = "sha256:changed"
        v = diff(rec, ident2)
        assert v["state"] == "match", v
        assert stills[0] in v["invalidated"] and stills[1] in v["resumable"], v

    # plate code mismatch: same pages+preamble+prompts, but recorded plate_code_hash differs
    # (algorithm drift under a stable name) -> hard gate, nothing resumes. Checked before palm.
    identp = compute_identity(pages, stills, orchestration="page-centric", plate="single-plate")
    assert identp.get("plate_code_hash"), "plate_code_hash must be recorded when plate given"
    recp = json.loads(json.dumps(identp))
    recp["plate_code_hash"] = "sha256:oldcode"
    v = diff(recp, identp)
    assert v["state"] == "plate_code_mismatch" and v["resumable"] == [], v
    # ...but if either side lacks a plate_code_hash, the gate does NOT false-alarm
    recp2 = json.loads(json.dumps(identp)); recp2["plate_code_hash"] = None
    v = diff(recp2, identp)
    assert v["state"] == "match", f"missing plate_code_hash must not gate: {v}"

    # facts mismatch: facts turned on (or changed) -> nothing resumes; absent on both sides matches
    identf = compute_identity(pages, stills, orchestration="page-centric", plate="single-plate",
                              facts_hash="sha256:facts1")
    v = diff(identp, identf)
    assert v["state"] == "facts_mismatch" and v["resumable"] == [], v
    legacy = json.loads(json.dumps(identp)); legacy.pop("facts_hash", None)
    assert diff(legacy, identp)["state"] == "match", "pre-facts identity must match facts-off run"

    print("run_identity selftest: OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run identity: fingerprint + palm + steering/prompt hashes")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compute", help="print the computed run_identity for a .pages dir")
    c.add_argument("pages_dir")
    c.add_argument("--stills", default="")
    c.add_argument("--orchestration", default="")
    c.add_argument("--plate", default="")

    r = sub.add_parser("record", help="compute + write run_identity into a run manifest")
    r.add_argument("manifest")
    r.add_argument("pages_dir")
    r.add_argument("--stills", default="")
    r.add_argument("--orchestration", default="")
    r.add_argument("--plate", default="")

    v = sub.add_parser("verdict", help="diff recorded identity vs current -> per-still verdict")
    v.add_argument("manifest")
    v.add_argument("pages_dir")
    v.add_argument("--stills", default="")

    sub.add_parser("selftest", help="run built-in self-checks")
    args = ap.parse_args()

    def _stills(s: str) -> list[str]:
        return [x.strip() for x in s.split(",") if x.strip()]

    if args.cmd == "compute":
        ident = compute_identity(args.pages_dir, _stills(args.stills),
                                 args.orchestration or None, args.plate or None)
        print(json.dumps(ident, indent=2))
        return 0
    if args.cmd == "record":
        ident = compute_identity(args.pages_dir, _stills(args.stills),
                                 args.orchestration or None, args.plate or None)
        record_identity(args.manifest, ident)
        print(json.dumps({"recorded": args.manifest, "palm_print": ident["palm_print"],
                          "pages": len(ident["page_order"]),
                          "stills": list(ident["still_prompt_hashes"].keys())}, indent=2))
        return 0
    if args.cmd == "verdict":
        recorded = read_recorded(args.manifest)
        current = compute_identity(args.pages_dir, _stills(args.stills))
        print(json.dumps(diff(recorded, current), indent=2))
        return 0
    if args.cmd == "selftest":
        return _selftest()
    return 1


if __name__ == "__main__":
    sys.exit(main())
