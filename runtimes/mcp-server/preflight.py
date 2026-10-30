#!/usr/bin/env python3
"""preflight.py — the GO/NO-GO gate that runs everything up to JUST BEFORE Map-Reduce and reports
coverage, escalations, and critical-signal-at-risk so an expensive M/R pass is never spent while
blind to a large chunk of signal.

WHY (SYSTEM_SPEC L4 ground-truth-carries-its-error; the observability law). The pipeline's most
expensive step is Map-Reduce (per-page LLM over the whole corpus). Before paying for it, we can
CHEAPLY and DETERMINISTICALLY answer: did we survey every file? which reservoirs have no rig
(booked-untapped)? which file types are unknown to the ontology (escalations)? This gate assembles
those signals into a verdict a human reads BEFORE go/no-go. (Note: a structural-role/topology
no-rig gap is graded MEDIUM, not CRITICAL — plating infers grouping from file paths and does not
depend on the unbuilt rich-topology rig; treating it as CRITICAL was a verified false alarm.)

It measures, it does not fix (L4: reducing the error is the SYSTEM's job over time via new rigs;
the gate's job is faithful measurement + surfacing). It NEVER blocks automatically — it emits a
verdict + severity so a human (or an orchestrator policy) decides. "It would suck to do an
expensive run of M/R only to find out you were missing a huge chunk of signal."

INPUTS (all cheap / deterministic / no M/R):
  - the paged substrate (.pages) → census + reserves ledger (code_graph.compute_source_coverage)
  - the materialized manifest (ingest_helpers) → raw/inferred/referential accounting, if present
  - the signal-class ontology (signal_classes) → role of each census ext (material/signal/structural)
  - optionally the rectifier survey (rectify --no-llm) → unknown-type escalations

SEVERITY (worst wins the verdict):
  CRITICAL  reserved for a gap that genuinely endangers the run. (A structural-role/topology no-rig
            gap is NOT this — plating uses path-based inference, rig-independent; see the graded
            alarms below. The prior "structural gap = CRITICAL" was a verified false alarm.)
  HIGH      a large material/signal reservoir is untapped (a rig gap on a big deposit).
  MEDIUM    unknown file types surfaced (ontology-expansion escalations), OR a structural-role
            (dependency-topology) extension lacks the UNBUILT optional rich-topology rig — booked;
            plating unaffected.
  LOW       small booked-untapped reserves; expected frontier.
  OK        everything surveyed + tapped (or the untapped is small + non-structural).

CLI:
  python preflight.py --pages <dir> [--materialized <manifest.tsv>] [--still-specs DIR]
      [--rectify] [--out report.json] [--fail-on CRITICAL|HIGH|MEDIUM]
  --fail-on sets the process exit code (2) if the verdict is at/above that severity — lets an
  orchestrator gate a run in CI/automation while a human reads the same report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import code_graph as CG
import signal_classes as SC

_SEVERITY_ORDER = ["OK", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def _worst(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER.index(a) >= _SEVERITY_ORDER.index(b) else b


def _ext_roles(ext: str) -> set[str]:
    """Signal-class roles this extension carries (material/signal/structural), via the ontology."""
    classes, known, _ = SC.classify("x." + ext if ext else "x")
    roles = set()
    for c in classes:
        role = SC.PRIME_SIGNAL_CLASSES.get(c, (None,))[0]
        if role:
            roles.add(role)
    return roles


def _example_paths(pages_dir: Path, exts: list[str], limit: int = 5) -> list[str]:
    """The WHERE: up to `limit` real source paths whose extension is in `exts`, from the census.
    Turns "a reservoir somewhere" into "these files". Deterministic (census order)."""
    want = {e.lower() for e in exts}
    seen: list[str] = []
    for path, ext, _ in CG.census_source_files(pages_dir):
        if (ext or "").lower() in want:
            seen.append(path)
            if len(seen) >= limit:
                break
    return seen


def _action_for_coverage_gap(roles: set[str], ext_counts: dict, attenuated: bool,
                             lifted: int, booked: int) -> tuple[str, str]:
    """The WHAT/HOW + WHY-IT-MATTERS for a coverage_gap, grounded in the signal-class ontology.
    Returns (action, why_it_matters). No fabricated alarm, no fabricated all-clear (L4)."""
    ext_list = CG.format_ext_counts(ext_counts) if ext_counts else "(no extension)"
    if attenuated:
        how = ("no action — the ingest stage lifted this reservoir to text "
               f"({lifted} lifted-to-text / {booked} booked-referential).")
        why = ("recovered by an ingest helper (inferred text) — listed for provenance. Files booked "
               "referential are marker-only: the model does NOT see their content.")
        return how, why
    if "structural" in roles:
        how = ("no action required — the only rig this would feed is the UNBUILT optional "
               "rich-topology extraction (code-graph 'path a'), which nothing we run today "
               "consumes. Build it ONLY if you later want a rich dependency graph; plating "
               "does not need it (dependency-cohesive groups by file path).")
        why = ("code-graph would gain a build/project dependency graph; NOTHING is lost today "
               "without it — plating and distillation are unaffected. Not a plate hazard.")
        return how, why
    # material/signal reservoir with no code-graph structural rig
    how = (f"OPTIONAL — to add code-graph STRUCTURAL recovery for these files, build a "
           f"code-graph extraction rig for [{ext_list}] (a per-language template in "
           f"signal_classes.py + code_graph parser). NOT required for distillation: the stills "
           f"already extracted these files via the page map this run.")
    why = ("what is unrecovered is the CODE-GRAPH structural layer (symbols/edges) for these "
           "files — NOT their distilled business signal, which the page-centric map already "
           "captured. Skipping the rig loses structural cross-referencing, not content.")
    return how, why


def _read_materialized(path: str | None) -> dict:
    """Parse the materialized manifest (if given) into kind counts + referential/inferred exts."""
    out = {"present": False, "raw": 0, "inferred": 0, "referential": 0,
           "referential_exts": Counter(), "inferred_exts": Counter()}
    if not path or not os.path.exists(path):
        return out
    out["present"] = True
    import csv
    with open(path, newline="") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < 2:
                continue
            rel, kind = row[0], row[1]
            ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
            out[kind] = out.get(kind, 0) + 1
            if kind == "referential":
                out["referential_exts"][ext] += 1
            elif kind == "inferred":
                out["inferred_exts"][ext] += 1
    return out


def preflight(pages_dir: Path, materialized: str | None, still_specs: str | None,
              run_rectify: bool, profile: str, region: str, fail_on: str = "") -> dict:
    findings = []                       # each: {severity, kind, detail, ...}
    verdict = "OK"

    # ── 1. coverage ledger (census vs. tapped, per modality + alarms) ──
    cov = CG.compute_source_coverage(pages_dir)
    mat = _read_materialized(materialized)

    # ── 2. coverage alarms, graded by the SIGNAL-CLASS ROLE of the untapped reservoir ──
    # Graded by the signal-class ROLE of the untapped reservoir (structural gap != CRITICAL — see below).
    total = cov["total_source_files"] or 1
    for alarm in cov.get("coverage_alarms", []):
        exts = alarm.get("extensions", [])
        reserves = alarm.get("reserves", 0)
        ext_counts = cov["per_modality"].get(alarm["modality"], {}).get("extension_counts", {})
        roles = set()
        for e in exts:
            roles |= _ext_roles(e)
        # If the materialized manifest shows these exts are now lifted (inferred) or booked
        # (referential), the alarm is ATTENUATED — the ingest-helper stage addressed it.
        lifted = sum(mat["inferred_exts"].get(e, 0) for e in exts) if mat["present"] else 0
        booked = sum(mat["referential_exts"].get(e, 0) for e in exts) if mat["present"] else 0
        share = reserves / total
        # A `structural`-role gap (dependency-topology: .csproj/.sln/CMakeLists/… ) does NOT
        # threaten plating and is NOT CRITICAL. VERIFIED (2026-09-14): the plate strategy we run
        # (plate_strategy.dependency-cohesive) infers module grouping from FILE PATHS — it "needs
        # no topology graph" (plate_strategy.py) and is "independent of" the rich rig
        # (signal_classes.py dependency-topology rig note). The missing code-graph rig here is the
        # UNBUILT, exploration-tier path (a) (LLM-render dep-graph), which feeds NOTHING we run
        # today. So a structural-role no-rig gap is a MEDIUM "optional rich-topology extraction is
        # unbuilt; plating unaffected (uses path-based inference)" — never a go/no-go CRITICAL.
        # (Prior code fired CRITICAL here — a false alarm that cried wolf at the go/no-go gate.)
        if "structural" in roles:
            sev = "MEDIUM"
            why = ("structural-role (dependency-topology) extension has no code-graph structural "
                   "rig — this is the UNBUILT optional rich-topology extraction (path a), which "
                   "feeds nothing we run today. PLATING IS UNAFFECTED: dependency-cohesive infers "
                   "grouping from file paths, needs no topology graph. Not a plate hazard.")
        elif share >= 0.05:
            sev = "HIGH"
            why = (f"large reservoir ({share:.0%} of the survey) with no code-graph structural rig "
                   f"— significant signal unrecovered by code-graph (not a distillation-coverage claim)")
        else:
            sev = "LOW"
            why = "small booked-untapped reserve — expected frontier, book it"
        # attenuate if the ingest stage lifted/booked most of it
        # Only a LIFT (inferred text) attenuates. A referential booking is marker-only — the model
        # sees no content — so it is NOT "addressed"; the content_not_paged finding below grades it.
        if lifted > 0 and lifted >= reserves * 0.8:
            sev = "LOW"
            why = f"attenuated: {lifted}/{reserves} lifted to text by an ingest helper (inferred)"
        elif booked > 0:
            why += (f" {booked}/{reserves} of these files are booked referential (marker only, "
                    f"content NOT paged).")
        attenuated = lifted > 0 and lifted >= reserves * 0.8
        action, why_it_matters = _action_for_coverage_gap(roles, ext_counts, attenuated, lifted, booked)
        where = _example_paths(pages_dir, exts)
        findings.append({"severity": sev, "kind": "coverage_gap", "modality": alarm["modality"],
                         "extensions": exts, "extension_counts": ext_counts,
                         "extensions_label": CG.format_ext_counts(ext_counts),
                         "reserves": reserves, "roles": sorted(roles),
                         "lifted_inferred": lifted, "booked_referential": booked, "why": why,
                         # R-7 actionability: where / what-how / why-it-matters
                         "where": {"example_paths": where, "total_files": reserves},
                         "action": action, "why_it_matters": why_it_matters})
        verdict = _worst(verdict, sev)

    # ── 2b. content actually paged: referential (marker-only) share of the materialized files ──
    # A referential file reaches the model as `[FILE: x KIND: referential SIZE: n]` with no body.
    # If a large share of the corpus is booked that way the map phase runs on nothing (the COBOL
    # negative control: the model refused and the run still "succeeded"), so this must NOT read
    # as "safe to run".
    if mat["present"]:
        n_mat = mat.get("raw", 0) + mat.get("inferred", 0) + mat.get("referential", 0)
        n_ref = mat.get("referential", 0)
        share = (n_ref / n_mat) if n_mat else 0.0
        if n_ref:
            if share >= 0.5:
                sev = "CRITICAL"
            elif share >= 0.2:
                sev = "HIGH"
            else:
                sev = "LOW"
            ref_exts = dict(mat["referential_exts"].most_common(15))
            findings.append({
                "severity": sev, "kind": "content_not_paged",
                "referential_files": n_ref, "materialized_files": n_mat,
                "referential_share": round(share, 3),
                "extensions_label": CG.format_ext_counts(ref_exts),
                "why": (f"{n_ref}/{n_mat} files ({share:.0%}) are booked referential — marker only, "
                        f"their content is NOT paged and the model cannot extract from them"),
                "where": {"example_paths": _example_paths(pages_dir, list(ref_exts)),
                          "total_files": n_ref},
                "action": ("add the extension to ingest_helpers._TEXT_NATIVE if it is text, or "
                           "provision an ingest helper that lifts it; otherwise expect no findings "
                           "from these files"),
                "why_it_matters": ("map/reduce over marker-only pages produces refusals or "
                                   "invented content, not a specification"),
            })
            verdict = _worst(verdict, sev)

    # ── 3. unknown file types (ontology-expansion escalations) — from census exts classify() misses ──
    census_exts = Counter()
    for _, ext, _ in CG.census_source_files(pages_dir):
        census_exts[ext] += 1
    unknown = [(e, n) for e, n in census_exts.most_common()
               if not SC.classify("x." + e if e else "x")[1]]
    # subtract those the materialized manifest already resolved (inferred or referential)
    if mat["present"]:
        resolved = set(mat["inferred_exts"]) | set(mat["referential_exts"])
        unknown = [(e, n) for e, n in unknown if e not in resolved]
    if unknown:
        sev = "MEDIUM"
        unknown_exts = [e for e, _ in unknown]
        findings.append({"severity": sev, "kind": "unknown_types",
                         "count_exts": len(unknown), "count_files": sum(n for _, n in unknown),
                         "top": dict(unknown[:15]),
                         "extensions_label": CG.format_ext_counts(dict(unknown[:15])),
                         "why": "file types the ontology has never seen — ontology-expansion "
                                "candidates; run rectify to interrogate/route, or ratify",
                         # R-7 actionability
                         "where": {"example_paths": _example_paths(pages_dir, unknown_exts),
                                   "total_files": sum(n for _, n in unknown)},
                         "action": ("run `rectify` (deterministic sniff, no LLM/spend) to "
                                    "interrogate + route these types into the ontology, or ratify "
                                    "to accept them as-is. Until then they are booked-untapped."),
                         "why_it_matters": ("these files were paged (so distillation saw their "
                                            "text) but the ontology has no class for them — they "
                                            "carry no typed role, so no rig targets them. Booked, "
                                            "not silently dropped (L2/L4).")})
        verdict = _worst(verdict, sev)

    # ── 4. optional: run the rectifier survey (deterministic, no LLM) for escalation detail ──
    rectify_summary = None
    if run_rectify:
        try:
            import rectify as R
            rep = R.rectify_census(pages_dir, commit=False, limit=None,
                                   profile=profile, region=region, model=R.DEFAULT_MODEL,
                                   use_llm=False)  # deterministic sniff only — cheap, no spend
            rectify_summary = {k: rep[k] for k in
                               ("unknown_ext_count", "cohorts_escalated", "no_instance_count")
                               if k in rep}
        except Exception as e:
            rectify_summary = {"error": f"{type(e).__name__}: {e}"}

    # ── R-7: blocking status (derived from severity + the gate threshold) ──
    # A finding/verdict BLOCKS only if the gate is set to fail at/above its severity. With no
    # --fail-on (report mode), nothing blocks — HIGH is advisory, not a halt. This is the honest
    # "whether to act now" the operator needs; HIGH != blocking by default.
    def _blocks(sev: str) -> bool:
        if not fail_on:
            return False
        return _SEVERITY_ORDER.index(sev) >= _SEVERITY_ORDER.index(fail_on)
    for f in findings:
        f["blocking"] = _blocks(f["severity"])
    verdict_blocks = _blocks(verdict)

    # ── R-7: verdict-level recommended action (the top-line "what do I do") ──
    actionable = [f for f in findings if f["severity"] in ("HIGH", "CRITICAL")
                  and "no action" not in f.get("action", "").lower()]
    if verdict == "OK":
        recommended_action = "Proceed — no untapped signal reservoir of concern."
    elif not actionable:
        recommended_action = ("Proceed — findings are advisory (attenuated / optional / booked); "
                              "no rig or action is required for this run. See per-finding action.")
    else:
        top = actionable[0]
        exts = ", ".join(top.get("extensions", [])) or top.get("kind", "")
        recommended_action = (
            f"{'HALT — resolve before spending' if verdict_blocks else 'Advisory (run continues)'}: "
            f"the largest actionable gap is the [{exts}] reservoir "
            f"({top.get('reserves', top.get('count_files', '?'))} files). {top.get('action','')}")

    return {
        "pages_dir": str(pages_dir),
        "verdict": verdict,
        "blocking": verdict_blocks,
        "recommended_action": recommended_action,
        "verdict_meaning": {
            "OK": "surveyed + tapped (or untapped is small + non-structural) — safe to run M/R",
            "LOW": "small booked frontier reserves — safe to run; note the gaps",
            "MEDIUM": "unknown file types surfaced — booked, consider ratify/rectify before a full run",
            "HIGH": "a large reservoir is untapped — a rig gap on real signal; decide before spending",
            "CRITICAL": "a gap that genuinely endangers the run — resolve before M/R",
        }[verdict],
        "coverage": {
            "total_source_files": cov["total_source_files"],
            "structural_parse_pct": cov["structural_parse_pct"],
            "booked_untapped": cov["booked_untapped"],
            "conserved": cov["conserved"],
            "per_modality": cov["per_modality"],
        },
        "materialized": {"present": mat["present"], "raw": mat.get("raw", 0),
                         "inferred": mat.get("inferred", 0), "referential": mat.get("referential", 0)},
        "rectify_survey": rectify_summary,
        "findings": sorted(findings, key=lambda f: -_SEVERITY_ORDER.index(f["severity"])),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight go/no-go gate before Map-Reduce")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--materialized", default="", help="materialized_manifest.tsv (optional)")
    ap.add_argument("--still-specs", default="")
    ap.add_argument("--rectify", action="store_true", help="also run the deterministic rectifier survey")
    ap.add_argument("--out", default="")
    ap.add_argument("--fail-on", default="", choices=["", "CRITICAL", "HIGH", "MEDIUM", "LOW"],
                    help="exit 2 if the verdict is at/above this severity (for orchestrator gating)")
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    args = ap.parse_args()

    rep = preflight(Path(args.pages), args.materialized or None, args.still_specs or None,
                    args.rectify, args.profile, args.region, fail_on=args.fail_on)
    out = json.dumps(rep, indent=2)
    if args.out:
        Path(args.out).write_text(out)
    # human-readable banner to stderr always (the gate is meant to be SEEN)
    v = rep["verdict"]
    print(f"\n{'='*66}\n  PREFLIGHT VERDICT: {v}"
          f"{'  [BLOCKING]' if rep.get('blocking') else '  [advisory]'}\n  {rep['verdict_meaning']}\n{'='*66}",
          file=sys.stderr)
    print(f"  → RECOMMENDED: {rep.get('recommended_action','')}", file=sys.stderr)
    for f in rep["findings"]:
        blk = "BLOCKING" if f.get("blocking") else "advisory"
        print(f"\n  [{f['severity']}/{blk}] {f['kind']}: {f.get('why','')}", file=sys.stderr)
        w = f.get("where", {})
        if w.get("example_paths"):
            ex = ", ".join(w["example_paths"][:5])
            more = w.get("total_files", 0)
            print(f"      WHERE  ({more} files) e.g. {ex}", file=sys.stderr)
        if f.get("action"):
            print(f"      ACTION {f['action']}", file=sys.stderr)
        if f.get("why_it_matters"):
            print(f"      IMPACT {f['why_it_matters']}", file=sys.stderr)
    cov = rep["coverage"]
    print(f"\n  survey: {cov['total_source_files']} files, {cov['structural_parse_pct']}% structurally "
          f"parsed by code-graph (NOT distillation coverage), {cov['booked_untapped']} booked-untapped, "
          f"conserved={cov['conserved']}", file=sys.stderr)
    if not args.out:
        print(out)

    if args.fail_on:
        if _SEVERITY_ORDER.index(v) >= _SEVERITY_ORDER.index(args.fail_on):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
