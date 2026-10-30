#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""eval_view.py: read-only aggregation of a run's evaluation artifacts, for the launcher's
``GET /api/runs/<id>/eval`` and ``GET /api/eval/compare`` (docs/LAUNCHER_API.md).

Nothing here calls a model, writes a file or imports an evaluator. It reads what
``eval/score.py`` (score.json), ``eval/graph_eval.py`` (graph_eval.json), ``eval/ci/run_suite.py``
(suite.json) and ``eval/benefits/*`` (qa/impact/fieldapp/insights.json) already wrote, plus the
run's own ``conservation_report.json`` and telemetry, and returns one normalized document with two
sections kept strictly apart (see "What these numbers mean" in eval/README.md):

* ``exact``     deterministic: same inputs, same answer, no model. Lines conserved, citation
                validity, structural checks, parser-confirmed graph precision/recall, bridge
                link assertions, cost of calls made.
* ``estimated`` LLM-as-a-judge (Claude on Bedrock): recall, precision, incorrect/uncertain counts,
                judge agreement, golden agreement, per-trap outcomes, benefit evaluations. Every
                value carries its 95% CI, or (when the scorer recorded none) the measured
                judge-noise band from eval/ci/suite_lib.py DEFAULT_TOLERANCES.

A metric that is missing or null is never turned into 0: it is reported as
``{"value": null, "status": "not_measured", "reason": ...}``.

Where artifacts are looked for (all read-only):
  1. ``<run>/validation/**``                 the run's own evaluation outputs
  2. ``extra_dirs`` (``<runs-dir>/_evals/<run_id>/``)   evaluations the launcher started
  3. ``<repo>/eval/runs/**``, ``<repo>/eval/ci/baselines/``   committed results, used only when
     their recorded provenance (``spec``, ``meta.output_dir``, ``provenance.vars.run``) points
     at this run directory
An artifact whose provenance points at a *different* run is skipped even inside the run's own
``validation/`` folder (the JC v2 folder holds a graph_eval.json of the first JC run).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

try:                                    # the tolerance table is the suite's own, not a copy
    sys.path.insert(0, str(REPO / "eval" / "ci"))
    from suite_lib import tolerance_for as _suite_tolerance_for  # noqa: E402
except Exception:                       # pragma: no cover - only when eval/ci is absent
    _suite_tolerance_for = None

try:
    import yaml  # noqa: E402
except Exception:                       # pragma: no cover
    yaml = None

EVAL_SCHEMA_VERSION = 1

#: spec artifacts a score.json can score, keyed by their path inside a run directory
ARTIFACTS = {
    "bottled_spec": "bottled_spec/bottled_spec.md",
    "master_blend": "master_blend/master_blend.md",
    "issues": "issues/issues.md",
}
ARTIFACT_LABELS = {"bottled_spec": "Business spec (bottled)", "master_blend": "Full spec (master blend)",
                   "issues": "Issues & Insights report"}
BENEFITS = {
    "qa": ("qa.json", "Q&A (B2/B5)", "benefits-qa"),
    "impact": ("impact.json", "Change impact (B3)", "benefits-impact"),
    "fieldapp": ("fieldapp.json", "Field-app compatibility", "benefits-fieldapp"),
    "insights": ("insights.json", "Issues insights (B1)", "benefits-insights"),
}
BENEFIT_HEADLINES = {
    "qa": ["correct_pct", "score_pct", "wrong", "trick_abstain_pct", "answers_with_citations_pct",
           "citation_validity_pct"],
    "impact": ["location_recall_pct", "location_precision_pct", "file_recall_pct", "rule_recall_pct"],
    "fieldapp": ["full_pct", "partial_pct", "missed_pct", "score_pct", "correct_pct"],
    "insights": ["issue_described_any_pct", "issue_flagged_same_pct", "flag_precision_true_pct",
                 "incorrect_findings", "overall_recall_any_pct"],
}
SKIP_DIRS = {"_plan", "corpus", "__pycache__", ".git"}

LEGEND = {
    "exact": "Exact: same answer every time, no AI.",
    "estimated": "Estimated: LLM-as-a-judge (Claude on Bedrock), shown with 95% CI.",
}
BANNER = ("Results are on synthetic samples with known answers (answer keys written with the "
          "corpus) unless a key is provided. For code without a key only the Exact checks apply.")


# ── small helpers ───────────────────────────────────────────────────────────────────────────
_CACHE: dict[str, tuple[tuple, object]] = {}
_CACHE_LOCK = threading.Lock()


def read_json(p: Path):
    """Parsed JSON, cached by (mtime, size); None when absent or invalid."""
    try:
        st = p.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    k = str(p)
    with _CACHE_LOCK:
        hit = _CACHE.get(k)
        if hit and hit[0] == sig:
            return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    with _CACHE_LOCK:
        if len(_CACHE) > 256:
            _CACHE.clear()
        _CACHE[k] = (sig, data)
    return data


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _get(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def not_measured(reason: str, **extra) -> dict:
    return {"value": None, "status": "not_measured", "reason": reason, **extra}


def exact_value(v, *, reason: str = "not reported by the evaluator", **extra) -> dict:
    """A deterministic value. Missing/null stays not_measured, never 0."""
    if _num(v) is None:
        return not_measured(reason, **extra)
    return {"value": v, "status": "measured", **extra}


def tolerance(metric: str) -> tuple[float | None, str, str]:
    if _suite_tolerance_for is None:    # pragma: no cover
        return (None, "informational", "eval/ci/suite_lib.py not importable")
    return _suite_tolerance_for(metric)


def _mtime_iso(p: Path) -> str | None:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(p.stat().st_mtime, datetime.timezone.utc).isoformat()
    except OSError:
        return None


def _rglob(root: Path, names: set[str]):
    """Files named in ``names`` under root, skipping plan/corpus copies. Deterministic order."""
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f in names or ("suite.json" in names and f.endswith(".suite.json")):   # ci/baselines
                out.append(Path(dirpath) / f)
    return out


# ── provenance matching: does an artifact describe THIS run directory? ─────────────────────
def _run_identity(d: Path) -> tuple | None:
    """(run_id, created_at, palm_print) of a run directory, or None when it records none."""
    rj = read_json(d / "run.json") or {}
    man = read_json(d / "run_manifest.json") or {}
    ident = (rj.get("run_id"), rj.get("created_at"), _get(man, "run_identity", "palm_print"))
    return ident if ident[0] and ident[1] else None


def same_run(a: Path, b: Path) -> bool:
    """True when two directories are copies of one run (same run.json id and creation time, and
    the same palm print when both record one): a run that was copied or moved keeps its scores."""
    ia, ib = _run_identity(a), _run_identity(b)
    return bool(ia and ib and ia[:2] == ib[:2] and (not ia[2] or not ib[2] or ia[2] == ib[2]))


def ref_matches(ref, run_dir: Path, repo: Path, allow_copy: bool = False) -> bool | None:
    """True: ``ref`` names run_dir. False: it names another directory. None: cannot tell
    (for example a container path such as /work/demo/out that no longer exists)."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    run_dir = run_dir.resolve()
    p = Path(ref)
    cands = [p] if p.is_absolute() else [repo / p, repo / "eval" / p, run_dir.parent / p.name]
    for c in cands:
        try:
            if c.exists():
                # allow_copy (the run's own folders only): an artifact recorded at the original
                # location of a copied/moved run is still this run's own result
                if c.resolve() == run_dir or (allow_copy and c.is_dir() and same_run(c.resolve(), run_dir)):
                    return True
        except OSError:
            continue
    parts = [x for x in p.parts if x not in ("/", "..", ".")]
    if len(parts) >= 2 and tuple(run_dir.parts[-len(parts):]) == tuple(parts):
        return True
    if any(c.exists() for c in cands if not p.is_absolute() or c == p):
        return False                    # an existing directory that is not this run
    if parts and parts[-1] != run_dir.name:
        return False
    return None


def spec_run_ref(spec) -> tuple[str | None, str | None]:
    """(run directory part of a spec path, artifact name)."""
    if not isinstance(spec, str):
        return None, None
    norm = spec.replace("\\", "/")
    for art, rel in ARTIFACTS.items():
        if norm.endswith("/" + rel) or norm == rel:
            return norm[: -len(rel)].rstrip("/") or ".", art
    return None, None


# ── answer keys ────────────────────────────────────────────────────────────────────────────
def resolve_key(ref, repo: Path, known_keys: list[dict]) -> tuple[Path | None, str]:
    """Find the answer key a score.json names. The key is server-side only; the client never
    supplies this path. Returns (path or None, how it was resolved)."""
    if not isinstance(ref, str) or not ref:
        return None, "no key recorded"
    p = Path(ref)
    tail = tuple(x for x in p.parts if x not in ("/", "..", "."))[-2:]
    for k in known_keys:                # the server's own key list wins
        kp = Path(k["abs_path"])
        if tuple(kp.parts[-2:]) == tail and kp.is_file():
            return kp, f"server key {k.get('id')!r}, matched by path suffix {'/'.join(tail)}"
    for c in ([p] if p.is_absolute() else [repo / p, repo / "eval" / p]):
        if c.is_file():
            return c.resolve(), "recorded path"
    return None, f"recorded key {ref!r} not found on this server"


def load_key_items(path: Path | None) -> tuple[list[dict] | None, str | None]:
    if path is None:
        return None, "answer key not found"
    if yaml is None:
        return None, "PyYAML is not installed in the launcher's Python; trap categories unavailable"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:              # pragma: no cover
        return None, f"answer key unreadable: {type(e).__name__}"
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, "answer key has no items list"
    return items, None


# ── per-artifact readers ───────────────────────────────────────────────────────────────────
def _score_rank(d: dict, p: Path) -> tuple:
    j = d.get("judge") or {}
    has_ci = bool((j.get("bootstrap_ci95") or {}).get("recall_any_match_pct"))
    return (d.get("method") == "llm", has_ci, _num(j.get("runs")) or 0, p.stat().st_mtime)


def _ci(j: dict, name: str):
    b = (j.get("bootstrap_ci95") or {}).get(name)
    if isinstance(b, dict) and _num(b.get("lo")) is not None and _num(b.get("hi")) is not None:
        return [b["lo"], b["hi"]]
    return None


def _across(j: dict, name: str):
    a = (j.get("across_runs") or {}).get(name)
    if isinstance(a, dict) and isinstance(a.get("runs"), list):
        return {"min": a.get("min"), "median": a.get("median"), "max": a.get("max"),
                "runs": a.get("runs")}
    return None


def judged_metric(value, metric: str, *, ci=None, across=None, judged: bool, reason_if_none: str) -> dict:
    """An LLM-judged value with its uncertainty. Never a bare number: a CI when the scorer
    recorded one, otherwise the measured judge-noise band for that metric family."""
    if not judged:
        return not_measured(reason_if_none, metric=metric)
    if _num(value) is None:
        return not_measured("the scorer reported null for this metric", metric=metric)
    tol, _direction, basis = tolerance(metric)
    out = {"value": value, "status": "measured", "metric": metric, "ci95": ci,
           "across_runs": across, "noise_band": tol, "noise_basis": basis}
    if ci is None:
        out["uncertainty"] = (f"no CI recorded by this scorer version; measured judge noise for "
                              f"{metric} is ±{tol:g}" if tol is not None else
                              "no CI recorded and no measured noise band")
    else:
        out["uncertainty"] = "bootstrap 95% CI"
    return out


def score_summary(d: dict, p: Path, artifact: str, origin: str) -> dict:
    j = d.get("judge") or {}
    judged = d.get("method") == "llm"
    why = ("scored with the deterministic --no-llm matcher (smoke): it detects breakage but is "
           "not a judged recall; never quote it (eval/README.md)") if not judged else ""
    rec, prec = d.get("recall") or {}, d.get("precision") or {}
    runs = _num(j.get("runs"))
    agree = j.get("agreement") or {}
    gold = j.get("golden")
    kappa_reason = "needs at least 2 judge passes" if (runs or 0) < 2 else "not reported"
    if not j:
        kappa_reason = "this scorer version records no agreement statistics"
    est = {
        "artifact": artifact, "label": ARTIFACT_LABELS.get(artifact, artifact),
        "source": str(p), "origin": origin, "method": d.get("method"),
        "scorer_version": d.get("scorer_version"),
        "segmentation": (d.get("segmentation") or {}).get("strategy") or (
            "auto (pre-hardening scorer)" if not d.get("scorer_version") else None),
        "judge_runs": runs if runs is not None else not_measured(
            "this scorer version did not record the number of judge passes"),
        "key_items": _get(d, "counts", "key_items"),
        "findings": _get(d, "counts", "findings"),
        "metrics": {
            "recall_any_match_pct": judged_metric(rec.get("any_match_pct"), "recall.any_match_pct",
                                                  ci=_ci(j, "recall_any_match_pct"),
                                                  across=_across(j, "recall_any_match_pct"),
                                                  judged=judged, reason_if_none=why),
            "recall_strict_full_pct": judged_metric(rec.get("strict_full_pct"), "recall.strict_full_pct",
                                                    ci=_ci(j, "recall_strict_full_pct"),
                                                    across=_across(j, "recall_strict_full_pct"),
                                                    judged=judged, reason_if_none=why),
            "recall_partial_credit_pct": judged_metric(rec.get("partial_credit_pct"),
                                                       "recall.partial_credit_pct",
                                                       ci=_ci(j, "recall_partial_credit_pct"),
                                                       judged=judged, reason_if_none=why),
            "precision_keyed_pct": judged_metric(prec.get("keyed_pct"), "precision.keyed_pct",
                                                 ci=_ci(j, "precision_keyed_pct"),
                                                 across=_across(j, "precision_keyed_pct"),
                                                 judged=judged, reason_if_none=why),
            "precision_correct_pct": judged_metric(prec.get("correct_pct"), "precision.correct_pct",
                                                   judged=judged, reason_if_none=why),
            "incorrect": judged_metric(prec.get("incorrect"), "precision.incorrect",
                                       across=_across(j, "incorrect"), judged=judged,
                                       reason_if_none=why),
            "uncertain": (judged_metric(prec.get("uncertain"), "precision.uncertain",
                                        across=_across(j, "uncertain"), judged=judged,
                                        reason_if_none=why)
                          if "uncertain" in prec or not judged else
                          not_measured("this scorer version did not report abstentions")),
        },
        "agreement": {
            "fleiss_kappa_verdicts": exact_value(agree.get("fleiss_kappa_verdicts"), reason=kappa_reason,
                                                 label=agree.get("fleiss_kappa_verdicts_label")),
            "fleiss_kappa_key_coverage": exact_value(agree.get("fleiss_kappa_key_coverage"),
                                                     reason=kappa_reason,
                                                     label=agree.get("fleiss_kappa_key_coverage_label")),
            "unanimous_pct": exact_value(agree.get("unanimous_pct"), reason=kappa_reason),
            "note": "exact arithmetic on estimated labels (eval/README.md)",
        } if judged else not_measured(why or "not judged"),
        "golden": ({
            "status": "measured", "labelled_by": gold.get("labelled_by"),
            "labelled_on": gold.get("labelled_on"), "n_labelled": gold.get("n_labelled"),
            "agreement_pct": gold.get("agreement_pct"), "cohen_kappa": gold.get("cohen_kappa"),
            "disagreements": [{"key_id": i.get("key_id"), "judge": i.get("observed"),
                               "human": i.get("expected"), "basis": i.get("basis")}
                              for i in gold.get("items") or [] if i.get("agree") is False],
        } if isinstance(gold, dict) and gold.get("n_labelled") else
            not_measured("no human-labelled golden set for this artifact")),
        "judge_cost_usd": _num(_get(d, "judge_usage", "cost_usd")),
        "key_coverage": d.get("key_coverage") if judged else None,
        "key_ref": d.get("key"),
    }
    cit = d.get("citations") or {}
    st = d.get("structural") or {}
    cov = d.get("source_coverage") or {}
    exact = {
        "artifact": artifact, "label": ARTIFACT_LABELS.get(artifact, artifact), "source": str(p),
        "origin": origin,
        "citations_total": exact_value(_get(d, "counts", "citations")),
        "citations_valid": exact_value(cit.get("valid")),
        "citations_invalid": exact_value(cit.get("invalid")),
        "citation_validity_pct": exact_value(cit.get("validity_pct")),
        "citations_off_by_one": exact_value(cit.get("off_by_one"),
                                            reason="not reported by this scorer version"),
        "findings_without_citation": exact_value(cit.get("findings_without_citation")),
        "source_line_coverage_pct": exact_value(cov.get("source_line_coverage_pct"),
                                                reason="not reported by this scorer version"),
        "structural": {
            "duplicate_requirement_ids": exact_value(st.get("duplicate_requirement_ids_n"),
                                                     reason="not reported by this scorer version"),
            "unresolved_cross_references": exact_value(st.get("unresolved_cross_references_n"),
                                                       reason="not reported by this scorer version"),
            "requirements_without_citation": exact_value(st.get("requirements_without_citation_n"),
                                                         reason="not reported by this scorer version"),
            "orphan_citations": exact_value(st.get("orphan_citations"),
                                            reason="not reported by this scorer version"),
        },
    }
    return {"estimated": est, "exact": exact}


def _pct_counts(block: dict | None) -> dict:
    if not isinstance(block, dict):
        return {}
    return {k: block.get(k) for k in ("confirmed", "llm_edges", "parser_edges", "false_positives",
                                      "missing") if k in block}


def _undefined(block: dict, label: str, what: str = "precision") -> str:
    """Why a parser-comparison ratio is null: its denominator is zero."""
    if what == "precision" and block.get("llm_edges") == 0:
        return f"the graph proposed no {label} edges, so there is nothing to confirm (precision undefined)"
    if what == "recall" and block.get("parser_edges") == 0:
        return f"the parser found no {label} edges, so recall is undefined"
    return "not reported by the evaluator"


def graph_summary(d: dict, p: Path, origin: str) -> dict:
    t = d.get("thresholds") or {}
    par = d.get("parity") or {}
    parity_ok = par.get("status") == "ok"
    pr = "parser comparison skipped: " + str(par.get("note") or par.get("status") or "no facts/graph.json")
    langs = {}
    by_lang = par.get("by_source_language") or {}
    for lg, label in (("vbnet", "VB.NET"), ("sql", "SQL"), ("csharp", "C#")):
        b = by_lang.get(lg)
        if not parity_ok:
            langs[lg] = {"label": label, **not_measured(pr)}
        elif not isinstance(b, dict):
            langs[lg] = {"label": label, **not_measured(f"no {label} edges in the parser graph for this corpus")}
        else:
            langs[lg] = {"label": label, "status": "measured",
                         "precision_pct": exact_value(b.get("precision_pct"), reason=_undefined(b, label)),
                         "recall_pct": exact_value(b.get("recall_pct"), reason=_undefined(b, label, "recall")),
                         **_pct_counts(b)}
    typed = par.get("typed") or {}
    ext = par.get("extended") or {}
    mp = (par.get("by_provenance") or {}).get("model_proposed") or {}
    bl = d.get("bridge_llm") or {}
    prov = d.get("provenance") or {}
    out = {
        "source": str(p), "origin": origin, "status": "measured",
        "provenance_check": {"status": prov.get("status"), "detail": prov.get("detail"),
                             "inventory_hash": prov.get("inventory_hash")},
        "counts": d.get("counts"),
        "parser_confirmed_edge_precision_pct": exact_value(t.get("parser_confirmed_edge_precision_pct"),
                                                           reason=pr, **_pct_counts(typed)),
        "parser_edge_recall_pct": exact_value(t.get("parser_edge_recall_pct"), reason=pr),
        "pair_precision_pct": exact_value(t.get("parser_confirmed_pair_precision_pct"), reason=pr),
        "core_relations": par.get("core_relations"),
        "by_language": langs,
        "model_proposed_recall_pct": exact_value(t.get("parser_edge_recall_model_only_pct"), reason=pr,
                                                 **_pct_counts(mp.get("core"))),
        "method_call": {
            "precision_pct": exact_value(t.get("parser_method_call_precision_pct"),
                                         reason=_undefined(ext, "method-call") if parity_ok and ext else
                                         (pr if not parity_ok else "not reported by this graph_eval version")),
            "recall_pct": exact_value(t.get("parser_method_call_recall_pct"),
                                      reason=_undefined(ext, "method-call", "recall") if parity_ok and ext else
                                      (pr if not parity_ok else "not reported by this graph_eval version")),
            **_pct_counts(ext),
        },
        "false_positives_not_in_parser": exact_value(t.get("parser_edge_fp_not_in_parser"), reason=pr),
        "bridge": {
            "link_assertion_pct": exact_value(t.get("bridge_link_assertion_pct"),
                                              reason="not reported by this graph_eval version"),
            "symbol_occurrence_pct": exact_value(t.get("bridge_symbol_occurrence_pct")),
            "symbol_occurrence_bare_pct": exact_value(t.get("bridge_symbol_occurrence_bare_pct")),
            "concept_resolution_pct": exact_value(t.get("bridge_concept_resolution_pct")),
            "concept_exact_key_pct": exact_value(t.get("bridge_concept_exact_key_pct")),
            "links": _get(d, "counts", "bridge_links"),
        },
        "structural": {k: exact_value(t.get(k)) for k in (
            "hard_structural_failures", "anchor_claims_contradicted", "dangling_edges",
            "duplicate_node_ids", "same_as_contradictions", "contributes_to_cycles",
            "unified_id_mismatches")},
        "concept_coverage": {
            "key_item_coverage_any_pct": exact_value(t.get("key_item_coverage_any_pct"),
                                                     reason="graph_eval ran without a key"),
            "key_item_coverage_full_pct": exact_value(t.get("key_item_coverage_full_pct"),
                                                      reason="graph_eval ran without a key"),
            "note": "deterministic line overlap of concept citations with key locations (not semantic recall)",
        },
        "bridge_llm": ({"status": "measured", "agreement_pct": t.get("bridge_llm_agreement_pct"),
                        "ci95_low_pct": t.get("bridge_llm_agreement_ci95_low_pct"),
                        "sample": bl.get("sample") or bl.get("n")}
                       if bl.get("status") == "ok" and _num(t.get("bridge_llm_agreement_pct")) is not None
                       else not_measured(str(bl.get("reason") or "bridge LLM sample not run (--llm)"))),
        "gate_violations": d.get("gate_violations"),
    }
    return out


def benefit_summary(kind: str, d: dict, p: Path, origin: str) -> dict:
    m = d.get("measurement") or {}
    s = d.get("summary") or {}
    status = m.get("status") or s.get("measurement_status") or "measured"
    if d.get("method") not in (None, "llm"):
        return {"kind": kind, "label": BENEFITS[kind][1], "status": "not_measured",
                "reason": (f"only a deterministic --no-llm run ({d.get('method')}, e.g. a smoke check) "
                           "exists; it detects breakage and is not a judged measurement"),
                "metrics": {}, "source": str(p), "origin": origin}
    metrics = {}
    for k in BENEFIT_HEADLINES.get(kind, []):
        if k in s:
            v = s.get(k)
            if status == "not_measured":
                # eval/README.md: never quote a not_measured stage, even the parts it did compute
                metrics[k] = not_measured("the evaluator marked this run not_measured, so none of its "
                                          "numbers are quotable", recorded_value=v)
            else:
                metrics[k] = ({"value": v, "status": "measured"} if _num(v) is not None else
                              not_measured("null in the evaluator's summary (nothing graded for it)"))
    return {"kind": kind, "label": BENEFITS[kind][1], "status": status, "reason": m.get("reason"),
            "graded": m.get("graded"), "total": m.get("total"), "what": m.get("what"),
            "metrics": metrics, "cost_usd": _num(_get(d, "budget", "cost_usd")),
            "model": _get(d, "budget", "model"), "source": str(p), "origin": origin,
            "note": "LLM-graded on a small item set; the spread is wider than the spec recall's"}


# ── discovery ──────────────────────────────────────────────────────────────────────────────
def _origin(p: Path, run_dir: Path, extra_dirs: list[Path], repo: Path) -> str:
    rp = p.resolve()
    if rp.is_relative_to(run_dir.resolve()):
        return "run"
    for e in extra_dirs:
        if e.exists() and rp.is_relative_to(e.resolve()):
            return "launcher_eval"
    return "repo_record"


def discover(run_dir: Path, extra_dirs: list[Path], repo: Path) -> dict:
    run_dir = run_dir.resolve()
    own = [run_dir / "validation"] + list(extra_dirs)
    shared = [repo / "eval" / "runs", repo / "eval" / "ci" / "baselines"]
    names = {"score.json", "graph_eval.json", "suite.json"} | {v[0] for v in BENEFITS.values()}
    found = {"score": [], "graph": [], "suite": [], "benefit": [], "skipped": []}
    seen = set()
    for root, strict in [(r, False) for r in own] + [(r, True) for r in shared]:
        for p in _rglob(root, names):
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            d = read_json(p)
            if not isinstance(d, dict):
                found["skipped"].append({"source": str(p), "reason": "not valid JSON"})
                continue
            origin = _origin(p, run_dir, extra_dirs, repo)
            if p.name == "score.json":
                ref, art = spec_run_ref(d.get("spec"))
                ok = ref_matches(ref, run_dir, repo, allow_copy=not strict) if ref else None
                if art is None or ok is False or (strict and ok is not True):
                    if not strict:
                        found["skipped"].append({"source": str(p), "reason":
                                                 f"scores {d.get('spec')!r}, not this run's spec"})
                    continue
                found["score"].append((p, d, art, origin))
            elif p.name == "graph_eval.json":
                ok = ref_matches(_get(d, "meta", "output_dir"), run_dir, repo, allow_copy=not strict)
                if ok is False or (strict and ok is not True):
                    if not strict:
                        found["skipped"].append({"source": str(p), "reason":
                                                 f"graph_eval of {_get(d, 'meta', 'output_dir')!r}, not this run"})
                    continue
                found["graph"].append((p, d, origin))
            elif p.name.endswith("suite.json"):
                ok = ref_matches(_get(d, "provenance", "vars", "run"), run_dir, repo, allow_copy=not strict)
                if ok is False or (strict and ok is not True):
                    continue
                found["suite"].append((p, d, origin))
            else:
                if strict:                # benefit reports carry no run path; own dirs only
                    continue
                kind = next(k for k, v in BENEFITS.items() if v[0] == p.name)
                spec = d.get("spec")
                if spec:
                    r, _a = spec_run_ref(spec)
                    if r and ref_matches(r, run_dir, repo, allow_copy=True) is False:
                        found["skipped"].append({"source": str(p), "reason": f"evaluates {spec!r}, not this run"})
                        continue
                found["benefit"].append((p, d, kind, origin))
    return found


# ── the aggregate ──────────────────────────────────────────────────────────────────────────
def _conservation(run_dir: Path) -> dict:
    for rel in (".pages/conservation_report.json", "conservation_report.json"):
        p = run_dir / rel
        d = read_json(p)
        if isinstance(d, dict):
            s = d.get("stats") or {}
            return {"status": "measured", "ok": d.get("ok"), "source": str(p),
                    "lines": exact_value(s.get("lines")), "files": exact_value(s.get("emitted_files")),
                    "entries": s.get("entries"), "skipped_files": s.get("skipped_files"),
                    "pages": s.get("pages"), "errors": len(d.get("errors") or []),
                    "meaning": "every source line was paged exactly once (the run fails otherwise)"}
    return not_measured("no conservation_report.json in this run directory (older or incomplete run)")


def _pipeline_cost(run_dir: Path, spent_usd) -> dict:
    tel = read_json(run_dir / "telemetry_cost_summary.json")
    if _num(spent_usd) is not None and (run_dir / "telemetry").is_dir():
        return {"value": round(spent_usd, 4), "status": "measured", "source": "telemetry/ (budget.py)",
                "calls": (tel or {}).get("total_calls"), "basis": "API usage fields at list price"}
    if isinstance(tel, dict) and _num(tel.get("total_cost_usd")) is not None:
        return {"value": tel["total_cost_usd"], "status": "measured", "calls": tel.get("total_calls"),
                "source": "telemetry_cost_summary.json", "basis": "API usage fields at list price"}
    return not_measured("no telemetry in this run directory")


def _run_provenance(run_dir: Path) -> dict:
    rj = read_json(run_dir / "run.json") or {}
    man = read_json(run_dir / "run_manifest.json") or {}
    sp = read_json(run_dir / "source_provenance.json") or {}
    return {"dir": str(run_dir), "commit": sp.get("commit"), "ref": sp.get("ref_resolved"),
            "model_id": _get(rj, "params", "model_id"), "region": _get(rj, "params", "region"),
            "target_name": _get(rj, "env_snapshot", "DISTILL_TARGET_NAME"),
            "source_inventory_hash": _get(man, "run_identity", "source_inventory_hash"),
            "created_at": rj.get("created_at"),
            "repo_url": _get(man, "source", "repo_url") or None}


def _trap_rows(items: list[dict], judged: list[dict], repo_url: str | None) -> list[dict]:
    golden = {}
    for e in judged:
        g = e.get("golden") or {}
        for dis in g.get("disagreements") or []:
            golden.setdefault(dis["key_id"], []).append({**dis, "artifact": e["artifact"]})
    rows = []
    for it in items:
        flags = it.get("flags") or []
        if not flags:
            continue
        locs = []
        for loc in it.get("locations") or []:
            f, s, e_ = loc.get("file"), loc.get("start_line"), loc.get("end_line")
            ref = {"file": f, "start": s, "end": e_, "ref": f"{f}:{s}-{e_}"}
            if repo_url and f:
                ref["url"] = f"{repo_url.rstrip('/')}/{f}#L{s}-L{e_}"
            locs.append(ref)
        outcomes = {}
        for e in judged:
            kc = e.get("key_coverage") or {}
            outcomes[e["artifact"]] = kc.get(it.get("id")) or "not_in_key_coverage"
        stmt = " ".join(str(it.get("statement") or "").split())
        rows.append({"id": it.get("id"), "categories": flags, "type": it.get("type"),
                     "statement": stmt[:240] + ("…" if len(stmt) > 240 else ""),
                     "locations": locs, "outcomes": outcomes,
                     "judge_vs_human": golden.get(it.get("id"), [])})
    return rows


def aggregate(run_dir: Path, *, extra_dirs: list[Path] | None = None, repo: Path = REPO,
              known_keys: list[dict] | None = None, spent_usd=None, run_id: str | None = None) -> dict:
    run_dir = Path(run_dir).resolve()
    extra_dirs = [Path(e) for e in (extra_dirs or [])]
    known_keys = known_keys or []
    f = discover(run_dir, extra_dirs, repo)

    # ---- spec scores: one selected score per artifact (judged, with CI, most passes, newest)
    by_art: dict[str, list] = {}
    for p, d, art, origin in f["score"]:
        by_art.setdefault(art, []).append((p, d, origin))
    est_specs, exact_specs, sources = [], [], []
    for art in ARTIFACTS:
        cands = sorted(by_art.get(art, []), key=lambda t: _score_rank(t[1], t[0]), reverse=True)
        if not cands:
            continue
        p, d, origin = cands[0]
        s = score_summary(d, p, art, origin)
        s["estimated"]["alternatives"] = [str(c[0]) for c in cands[1:]]
        est_specs.append(s["estimated"])
        exact_specs.append(s["exact"])
        sources += [{"kind": "score", "artifact": art, "source": str(c[0]), "origin": c[2],
                     "selected": i == 0, "method": c[1].get("method"),
                     "judge_runs": _get(c[1], "judge", "runs"), "when": _mtime_iso(c[0])}
                    for i, c in enumerate(cands)]

    # ---- graph: newest graph_eval.json for this run
    graph = not_measured("no graph_eval.json for this run (run eval/graph_eval.py or the suite)")
    if f["graph"]:
        gs = sorted(f["graph"], key=lambda t: t[0].stat().st_mtime, reverse=True)
        graph = graph_summary(gs[0][1], gs[0][0], gs[0][2])
        graph["alternatives"] = [str(g[0]) for g in gs[1:]]
        sources += [{"kind": "graph_eval", "source": str(g[0]), "origin": g[2], "selected": i == 0,
                     "when": _mtime_iso(g[0])} for i, g in enumerate(gs)]

    # ---- suites (newest first); a plan suite is a cost plan, not a measurement
    suites = []
    for p, d, origin in sorted(f["suite"], key=lambda t: str(t[1].get("started_at") or ""), reverse=True):
        pv = d.get("provenance") or {}
        b = d.get("budget") or {}
        suites.append({
            "source": str(p), "origin": origin, "suite": pv.get("suite"),
            "mode": "plan" if pv.get("plan") else ("smoke" if pv.get("smoke") else "full"),
            "verdict": d.get("verdict"), "exit_code": d.get("exit_code"),
            "started_at": d.get("started_at"), "duration_s": d.get("duration_s"),
            "cap_usd": b.get("cap_usd"), "spent_usd": b.get("spent_usd"), "calls": b.get("calls"),
            "planned_usd": b.get("planned_usd"), "aborted": b.get("aborted"),
            "judge_runs": pv.get("judge_runs_default"),
            "stages": [{"id": s.get("id"), "status": s.get("status"),
                        "estimated_usd": s.get("estimated_usd"),
                        "spend_usd": _get(s, "spend", "stage_usd"),
                        "reason": (s.get("reason") or "")[:300] or None}
                       for s in d.get("stages") or []],
            "provenance": {"commit": _get(pv, "git", "commit"), "branch": _get(pv, "git", "branch"),
                           "dirty": _get(pv, "git", "dirty"), "model_id": pv.get("model_id"),
                           "region": pv.get("region"),
                           "corpora": {n: {"content_sha256": _get(c, "content", "sha256"),
                                           "n_files": _get(c, "content", "n_files"),
                                           "key": c.get("key"), "key_sha256": c.get("key_sha256")}
                                       for n, c in (pv.get("corpora") or {}).items()}},
        })
        sources.append({"kind": "suite", "source": str(p), "origin": origin,
                        "mode": suites[-1]["mode"], "when": d.get("started_at")})

    # ---- benefits: result file wins; else the newest non-plan suite's stage status says why
    benefits = {}
    for kind in BENEFITS:
        files = sorted([b for b in f["benefit"] if b[2] == kind],
                       key=lambda t: (t[1].get("method") in (None, "llm"), t[0].stat().st_mtime), reverse=True)
        if files:
            p, d, _k, origin = files[0]
            benefits[kind] = benefit_summary(kind, d, p, origin)
            benefits[kind]["alternatives"] = [str(x[0]) for x in files[1:]]
            continue
        reason = "no evaluation of this benefit was found for this run"
        # a full suite's stage status says more than a smoke run's "skipped (no deterministic mode)"
        for s in sorted(suites, key=lambda x: x["mode"] != "full"):
            if s["mode"] == "plan":
                continue
            st = next((x for x in s["stages"] if x["id"] == BENEFITS[kind][2]), None)
            if st:
                reason = (f"only the smoke check ran suite stage {st['id']} ({st['status']}); smoke is "
                          "deterministic and not a judged measurement" if s["mode"] == "smoke" else
                          f"suite stage {st['id']} was {st['status']}: {st['reason'] or '-'}")
                break
        benefits[kind] = {"kind": kind, "label": BENEFITS[kind][1], "status": "not_measured",
                          "reason": reason, "metrics": {}}

    # ---- traps: key items carrying a trap flag, outcome per judged artifact
    judged = [e for e in est_specs if e["method"] == "llm"]
    traps: dict = not_measured("no judged spec score for this run, so no per-trap outcome")
    if judged:
        kref = next((e["key_ref"] for e in judged if e.get("key_ref")), None)
        kpath, how = resolve_key(kref, repo, known_keys)
        items, why = load_key_items(kpath)
        if items is None:
            traps = not_measured(why or "answer key unavailable", key_ref=kref, key_resolution=how)
        else:
            rp = _run_provenance(run_dir)
            rows = _trap_rows(items, judged, rp.get("repo_url"))
            summary = {}
            for e in judged:
                c = summary.setdefault(e["artifact"], {})
                for r in rows:
                    for cat in r["categories"]:
                        o = r["outcomes"].get(e["artifact"])
                        c.setdefault(cat, {"full": 0, "partial": 0, "missed": 0, "other": 0})
                        c[cat][o if o in ("full", "partial", "missed") else "other"] += 1
            totals = {e["artifact"]: {o: sum(1 for r in rows if r["outcomes"].get(e["artifact"]) == o)
                                      for o in ("full", "partial", "missed")} for e in judged}
            traps = {"status": "measured", "key": str(kpath.relative_to(repo)) if kpath.is_relative_to(repo)
                     else str(kpath), "key_resolution": how, "artifacts": [e["artifact"] for e in judged],
                     "rows": rows, "totals": totals, "by_category": summary,
                     "by_category_note": "an item with two trap flags counts once in each category",
                     "links": ("source links point at the run's recorded repository" if rp.get("repo_url")
                               else "no repository URL recorded for this run, so locations are shown "
                                    "as file:lines without links"),
                     "note": "outcome = majority-vote judge verdict (estimated)"}
    for e in est_specs:                 # bulky; the trap table carries what the UI needs
        e.pop("key_coverage", None)

    # ---- spend: exact for calls made
    eval_spend = []
    suite_dirs = [Path(s["source"]).parent.resolve() for s in suites if s["mode"] != "plan"]
    for s in suites:
        if s["mode"] != "plan" and _num(s["spent_usd"]) is not None:
            eval_spend.append({"source": s["source"], "kind": f"suite ({s['mode']})",
                               "usd": s["spent_usd"], "calls": s["calls"]})
    for e in est_specs:
        if _num(e.get("judge_cost_usd")) is not None and not any(
                Path(e["source"]).resolve().is_relative_to(sd) for sd in suite_dirs):
            eval_spend.append({"source": e["source"], "kind": f"score.py ({e['artifact']})",
                               "usd": e["judge_cost_usd"], "calls": None,
                               "note": "judge calls of this score; cached calls are $0"})
    rprov = _run_provenance(run_dir)
    pipeline_cost = _pipeline_cost(run_dir, spent_usd)

    eval_prov = []
    for e in est_specs:
        eval_prov.append({"kind": "score", "artifact": e["artifact"], "source": e["source"],
                          "scorer_version": e["scorer_version"], "judge_runs": e["judge_runs"],
                          "model": _get(read_json(Path(e["source"])) or {}, "judge", "model"),
                          "when": _mtime_iso(Path(e["source"])), "when_basis": "file mtime"})
    for s in suites:
        eval_prov.append({"kind": "suite", "mode": s["mode"], "source": s["source"],
                          "when": s["started_at"], **s["provenance"], "judge_runs": s["judge_runs"]})
    if isinstance(graph, dict) and graph.get("status") == "measured":
        eval_prov.append({"kind": "graph_eval", "source": graph["source"],
                          "corpus_hash": graph["provenance_check"].get("inventory_hash"),
                          "provenance_check": graph["provenance_check"].get("status"),
                          "when": _mtime_iso(Path(graph["source"])), "when_basis": "file mtime"})

    has_key = bool(judged) or any(s["mode"] != "plan" for s in suites)
    return {
        "eval_schema_version": EVAL_SCHEMA_VERSION,
        "run_id": run_id, "run_dir": str(run_dir),
        "legend": LEGEND, "banner": BANNER,
        "has_answer_key": has_key,
        "exact": {
            "label": LEGEND["exact"],
            "conservation": _conservation(run_dir),
            "spec_artifacts": exact_specs or [],
            "spec_artifacts_status": None if exact_specs else not_measured(
                "no score.json for this run's specs (citation and structural checks need eval/score.py)"),
            "graph": graph,
            "cost": {"pipeline_usd": pipeline_cost, "evaluation": eval_spend,
                     "evaluation_total_usd": round(sum(x["usd"] for x in eval_spend), 4) if eval_spend
                     else None,
                     "note": "exact for calls made, list price; estimates (plans) are not costs"},
        },
        "estimated": {
            "label": LEGEND["estimated"],
            "spec_artifacts": est_specs,
            "spec_artifacts_status": None if judged else not_measured(
                "no LLM-judged score for this run (smoke/--no-llm scores are not judged recall)"
                if est_specs else "no score.json for this run"),
            "traps": traps,
            "benefits": benefits,
            "bridge_llm": graph.get("bridge_llm") if isinstance(graph, dict) and "bridge_llm" in graph
            else not_measured("no graph_eval.json"),
            "how_to_read": ("Judge noise measured on re-judged pairs: strict full-match recall moves "
                            "up to 10.5 points, any-match 0 (eval/README.md). A difference inside "
                            "the CI or band is not a finding."),
        },
        "suites": suites,
        "provenance": {"run": rprov, "evaluations": eval_prov},
        "sources": sources,
        "skipped_sources": f["skipped"],
    }


# ── compare ────────────────────────────────────────────────────────────────────────────────
def _flat_metrics(agg: dict) -> dict[str, dict]:
    """name -> {kind, value, ci95, metric(tolerance family), label}"""
    out = {}
    for e in agg["exact"]["spec_artifacts"]:
        a = e["artifact"]
        out[f"{a}.citation_validity_pct"] = {"kind": "exact", "metric": "citations.validity_pct",
                                             "label": f"{e['label']}: citation validity %",
                                             **e["citation_validity_pct"]}
        out[f"{a}.citations_invalid"] = {"kind": "exact", "metric": "citations.invalid",
                                         "label": f"{e['label']}: invalid citations", **e["citations_invalid"]}
    g = agg["exact"]["graph"]
    if isinstance(g, dict) and g.get("status") == "measured":
        for k, lab in (("parser_confirmed_edge_precision_pct", "Graph: parser-confirmed precision %"),
                       ("parser_edge_recall_pct", "Graph: parser edge recall %")):
            out[f"graph.{k}"] = {"kind": "exact", "metric": "graph." + k, "label": lab, **g[k]}
        for lg, b in g["by_language"].items():
            rv = b.get("recall_pct") if b.get("status") == "measured" else b
            out[f"graph.recall_{lg}_pct"] = {"kind": "exact", "metric": "graph.recall_pct",
                                             "label": f"Graph: {b['label']} recall %", **(rv or {})}
        out["graph.method_call_precision_pct"] = {"kind": "exact", "metric": "graph.method_call",
                                                  "label": "Graph: method-call precision %",
                                                  **g["method_call"]["precision_pct"]}
        out["graph.method_call_recall_pct"] = {"kind": "exact", "metric": "graph.method_call",
                                               "label": "Graph: method-call recall %",
                                               **g["method_call"]["recall_pct"]}
        out["graph.bridge_link_assertion_pct"] = {"kind": "exact", "metric": "graph.bridge",
                                                  "label": "Bridge: link assertion holds %",
                                                  **g["bridge"]["link_assertion_pct"]}
    pc = agg["exact"]["cost"]["pipeline_usd"]
    out["cost.pipeline_usd"] = {"kind": "exact", "metric": "cost", "label": "Pipeline cost $", **pc}
    for e in agg["estimated"]["spec_artifacts"]:
        for k, m in e["metrics"].items():
            out[f"{e['artifact']}.{k}"] = {"kind": "estimated", "label": f"{e['label']}: {k}", **m}
    for kind, b in agg["estimated"]["benefits"].items():
        for k, m in (b.get("metrics") or {}).items():
            out[f"benefit.{kind}.{k}"] = {"kind": "estimated", "metric": k,
                                          "label": f"{b['label']}: {k}",
                                          "benefit_status": b.get("status"), **m}
    return out


def _within(a: dict, b: dict, kind: str, metric: str | None) -> tuple[bool | None, str]:
    va, vb = _num(a.get("value")), _num(b.get("value"))
    if va is None or vb is None:
        return None, "not measured on both sides"
    d = abs(va - vb)
    ca, cb = a.get("ci95"), b.get("ci95")
    if kind == "estimated" and ca and cb:
        inside = (ca[0] <= vb <= ca[1]) or (cb[0] <= va <= cb[1])
        return inside, ("each value lies inside the other's 95% CI" if inside else
                        "outside both runs' 95% CIs")
    tol, _dirn, basis = tolerance(metric or "")
    if kind == "exact":
        if metric and metric.startswith(("graph.", "cost")):
            tol, basis = 0.0, "exact"
        t = tol or 0.0
        return d <= t, (f"exact: |Δ| {d:g} ≤ {t:g}" if d <= t else f"exact: |Δ| {d:g} is a real difference")
    if tol is None:
        return None, f"no measured noise band for {metric}"
    return d <= tol, (f"|Δ| {d:g} ≤ measured judge noise {tol:g} ({basis})" if d <= tol else
                      f"|Δ| {d:g} > measured judge noise {tol:g} ({basis})")


def compare(a: dict, b: dict) -> dict:
    fa, fb = _flat_metrics(a), _flat_metrics(b)
    rows = []
    for name in sorted(set(fa) | set(fb), key=lambda n: (0 if (fa.get(n) or fb.get(n))["kind"] == "exact" else 1, n)):
        x = fa.get(name) or not_measured("absent in this run")
        y = fb.get(name) or not_measured("absent in this run")
        kind = (fa.get(name) or fb.get(name))["kind"]
        metric = (fa.get(name) or fb.get(name)).get("metric")
        w, why = _within(x, y, kind, metric)
        va, vb = _num(x.get("value")), _num(y.get("value"))
        rows.append({"name": name, "label": (fa.get(name) or fb.get(name)).get("label"), "kind": kind,
                     "a": {k: x.get(k) for k in ("value", "status", "reason", "ci95", "noise_band")},
                     "b": {k: y.get(k) for k in ("value", "status", "reason", "ci95", "noise_band")},
                     "delta": round(vb - va, 4) if va is not None and vb is not None else None,
                     "within_noise": w, "basis": why})

    def corpus_ids(agg):
        keys = set()
        for e in agg["estimated"]["spec_artifacts"]:
            if e.get("key_ref"):
                keys.add(Path(str(e["key_ref"])).parts[-2:])
        for s in agg["suites"]:
            for c in s["provenance"]["corpora"].values():
                if c.get("key"):
                    keys.add(Path(str(c["key"])).parts[-2:])
        return keys
    ka, kb = corpus_ids(a), corpus_ids(b)
    comparable = bool(ka and kb and ka & kb)
    return {
        "runs": [a["run_id"], b["run_id"]],
        "comparable": comparable,
        "warning": None if comparable else (
            "the two runs were scored against different answer keys / corpora (or one has no key): "
            "read the differences as context, not as an improvement or a regression"),
        "rules": {"estimated": "within_noise when each value lies inside the other's 95% CI; without "
                               "CIs, when |Δ| ≤ the measured judge-noise band (eval/ci/suite_lib.py "
                               "DEFAULT_TOLERANCES)",
                  "exact": "exact metrics have no noise: any difference beyond the exact tolerance is real"},
        "rows": rows,
        "summary": {"rows": len(rows),
                    "outside_noise": sum(1 for r in rows if r["within_noise"] is False),
                    "inside_noise": sum(1 for r in rows if r["within_noise"] is True),
                    "not_comparable": sum(1 for r in rows if r["within_noise"] is None)},
    }
