#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validate a distillation run's knowledge graph. Writes graph_eval.json + graph_eval.md.

    python3 eval/graph_eval.py --run output/ap-invoices-demo \
        --source-root synthetic-corpus/ap-invoices \
        --key synthetic-corpus/ap-invoices/ANSWER_KEY.yaml

Four sections, three of them with no LLM and no network:

  1. STRUCTURAL   exact anchor + shape checks: every code-graph object/edge/entity resolves to a
                  real file and a real symbol occurrence; every bridge link's symbol occurs in the
                  cited file; no dangling/duplicate/self-loop edges; same_as merge classes are
                  consistent; CONTRIBUTES_TO is a DAG; unified concept ids are reproducible from
                  their contributors; orphans are counted.
  2. PARITY       the headline metric: precision and recall of the LLM-built code graph against
                  <output>/facts/graph.json, the deterministic Roslyn/ScriptDom parser output.
                  Skipped with a clear note when the parser did not run.
  3. COVERAGE     for each answer-key item, is there a concept citing the lines it lives on?
  4. BRIDGE LLM   optional (--llm): a bounded, cached, schema-validated sample asking whether a
                  concept<->code link is semantically right, with a bootstrap 95% CI.

Exit codes
  0  checks ran, no gate violated
  1  at least one --fail-under / --fail-over gate violated
  2  a required artifact is missing (no <output>/knowledge_graph/)
  3  REFUSED: the run was built from a different source revision than --source-root
     (override with --allow-revision-mismatch if you know why)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph import coverage, judge, load, parity, report, structural  # noqa: E402

VERSION = "1.0.0"


def _kv(pairs: list[str], flag: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"{flag} expects NAME=VALUE, got {p!r}")
        name, _, val = p.partition("=")
        try:
            out[name.strip()] = float(val)
        except ValueError:
            raise SystemExit(f"{flag} {name!r}: {val!r} is not a number") from None
    return out


def run(args) -> tuple[dict, int]:
    try:
        b = load.load_bundle(args.run, args.source_root, exclude=args.exclude,
                             concept_bodies=args.concept_bodies)
    except load.ArtifactError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return {"error": str(exc)}, 2

    prov = b.provenance
    result: dict = {
        "meta": {"version": VERSION, "output_dir": str(b.output_dir),
                 "source_root": str(b.source_root), "key": str(args.key) if args.key else None},
        "provenance": {"status": prov.status, "detail": prov.detail,
                       "inventory_hash": prov.inventory_hash,
                       "manifest_inventory_hash": prov.manifest_inventory_hash,
                       "source_identity": prov.source_identity,
                       "inventory_entries": prov.file_count, "verified": prov.verified,
                       "missing_from_source_root": prov.missing[:25],
                       "digest_mismatch": prov.digest_mismatch[:25],
                       "untracked_in_source_root": prov.untracked_in_source_root[:25]},
        "counts": b.counts(),
        "notes": list(b.notes),
    }

    if prov.refuse and not args.allow_revision_mismatch:
        result["refused"] = True
        result["notes"].append(
            "REFUSED to score: " + prov.detail +
            " — scoring a graph against a source tree it was not built from produces anchor and "
            "citation failures that say nothing about the graph. Point --source-root at the "
            "revision the run used, or pass --allow-revision-mismatch to score anyway.")
        print("REFUSED: " + prov.detail, file=sys.stderr)
        return result, 3
    if prov.refuse:
        result["notes"].append("source revision MISMATCH, scored anyway on "
                               "--allow-revision-mismatch: " + prov.detail)
    elif prov.status == "unknown":
        result["notes"].append("source revision unverified: " + prov.detail)

    result["structural"] = structural.check(b)
    result["structural"]["bridge"].pop("_per_link", None)
    result["parity"] = parity.compare(b)

    if args.key:
        try:
            result["concept_coverage"] = coverage.concept_coverage(b, args.key)
        except Exception as exc:  # noqa: BLE001 — a bad key must not lose sections 1/2
            result["concept_coverage"] = {"status": "skipped",
                                          "reason": f"{type(exc).__name__}: {exc}"}
    else:
        result["concept_coverage"] = {"status": "skipped", "reason": "no --key given"}

    if args.llm:
        try:
            result["bridge_llm"] = judge.sample_and_judge(
                b, sample=args.sample, seed=args.seed, cache_dir=args.cache_dir,
                max_cost=args.max_cost, model_id=args.model_id, region=args.region)
        except Exception as exc:  # noqa: BLE001 — LLM failure must not fail the deterministic run
            result["bridge_llm"] = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
            result["notes"].append(f"--llm sampling failed: {type(exc).__name__}: {exc}")
    else:
        result["bridge_llm"] = {"status": "skipped",
                                "reason": "--llm not given (deterministic run)"}

    result["thresholds"] = report.thresholds(result)
    result["threshold_doc"] = {k: {"better": d[0], "meaning": d[1]}
                               for k, d in report.THRESHOLD_DOC.items()}
    result["gate_violations"] = report.evaluate_gates(
        result["thresholds"], _kv(args.fail_under, "--fail-under"),
        _kv(args.fail_over, "--fail-over"))
    return result, (1 if result["gate_violations"] else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path,
                    help="distillation output dir containing knowledge_graph/")
    ap.add_argument("--source-root", type=Path,
                    help="the source tree the run was built from")
    ap.add_argument("--key", type=Path, help="answer key (YAML/JSON) for section 3")
    ap.add_argument("--out-dir", type=Path, help="default: <run>/validation/graph")
    ap.add_argument("--suffix", default="", help="output filename suffix, e.g. '.nollm'")
    ap.add_argument("--exclude", action="append", default=[],
                    help="glob of files under --source-root to ignore (repeatable), "
                         "e.g. --exclude ANSWER_KEY.yaml")
    ap.add_argument("--concept-bodies", choices=["auto", "db", "preview"], default="auto",
                    help="auto (default): full bodies from stills/shared_vectors.db when present, "
                         "else the 300-char body_preview in knowledge_graph.json")
    ap.add_argument("--allow-revision-mismatch", action="store_true",
                    help="score even when the run's source_inventory does not match --source-root")
    ap.add_argument("--llm", action="store_true", help="also run the bounded bridge-quality sample")
    ap.add_argument("--sample", type=int, default=30, help="bridge links to judge (default 30)")
    ap.add_argument("--seed", type=int, default=0, help="sampling seed (default 0)")
    ap.add_argument("--max-cost", type=float, default=2.0,
                    help="hard USD budget for --llm (default 2.0)")
    ap.add_argument("--model-id", default=judge.MODEL_ID)
    ap.add_argument("--region", default=judge.REGION)
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="content-addressed judge cache (default eval/graph/.cache/bridge)")
    ap.add_argument("--fail-under", action="append", default=[], metavar="NAME=VALUE",
                    help="fail (exit 1) if threshold NAME is below VALUE; repeatable")
    ap.add_argument("--fail-over", action="append", default=[], metavar="NAME=VALUE",
                    help="fail (exit 1) if threshold NAME is above VALUE; repeatable")
    ap.add_argument("--list-thresholds", action="store_true",
                    help="print the gateable metric names and exit")
    ap.add_argument("--quiet", action="store_true", help="do not print the summary to stdout")
    a = ap.parse_args()

    if a.list_thresholds:
        for name, (better, meaning) in report.THRESHOLD_DOC.items():
            print(f"{name:42s} better={better:3s}  {meaning}")
        return 0
    missing = [f for f in ("run", "source_root") if getattr(a, f) is None]
    if missing:
        ap.error("the following arguments are required: "
                 + ", ".join("--" + m.replace("_", "-") for m in missing))

    result, code = run(a)
    out = a.out_dir or (Path(a.run) / "validation" / "graph")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"graph_eval{a.suffix}.json").write_text(json.dumps(result, indent=2, default=str))
    (out / f"graph_eval{a.suffix}.md").write_text(report.to_markdown(result))

    if not a.quiet:
        summary = {
            "provenance": result.get("provenance", {}).get("status"),
            "counts": result.get("counts"),
            "structural_verdict": (result.get("structural") or {}).get("verdict"),
            "hard_failures": (result.get("structural") or {}).get("hard_failures"),
            "parity": (result.get("parity") or {}).get("headline")
                      or (result.get("parity") or {}).get("status"),
            "concept_coverage": (result.get("concept_coverage") or {}).get("overall")
                                or (result.get("concept_coverage") or {}).get("status"),
            "bridge_symbol_occurrence_pct":
                (result.get("thresholds") or {}).get("bridge_symbol_occurrence_pct"),
            "bridge_llm": (result.get("bridge_llm") or {}).get("agreement")
                          or (result.get("bridge_llm") or {}).get("status"),
            "gate_violations": result.get("gate_violations"),
        }
        print(json.dumps(summary, indent=2, default=str))
        print(f"wrote {out}/graph_eval{a.suffix}.{{json,md}}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
