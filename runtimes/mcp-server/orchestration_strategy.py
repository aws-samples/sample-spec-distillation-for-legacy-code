#!/usr/bin/env python3
"""orchestration_strategy.py — pluggable MAP ORCHESTRATION strategy (how stills sweep over
pages). One abstraction; still-centric is the historically-built loop, page-centric is the
cost-amortizing page-major loop. Sibling of plate_strategy (epic X-map-reduce-extraction-7)
one axis over: plate_strategy sets WHICH files rack onto a page; this sets HOW stills sweep
over pages (epic X-map-reduce-extraction-9; story orchestration-strategy).

WHY (SYSTEM_SPEC §7.2; L2 same-signal; L8; §9.2 non-absoluteness). The page body dominates
the map phase's input token count (a page is hundreds of thousands of est-tokens; a still
prompt a few thousand). A still-major loop re-pays that dominant page cost once per still.
A page-major loop caches the page as a prefix across the stills applied to it, so it is
billed once at write rate and thereafter at read rate — the single largest map-phase cost
lever. Proven in a spike: 67% input saving at 5 stills/page, break-even N>=2.

THE SHARED COMPOSITE (contract): both strategies build the SAME message shape; only loop
order and the cache breakpoint differ:
    system   = [ {text: PREAMBLE}, {text: PAGE} (, {cachePoint} if cache_on) ]
    messages = [ {role: user, content: [ {text: STILL_INSTRUCTION} ]} ]
The PAGE leads (cacheable prefix); the STILL_INSTRUCTION trails (small dynamic tail).

REGISTRY (framework/System code, L7 class — generic, no per-run specifics):
  still-centric  — one still swept over all its pages (still-major). Page re-sent per still,
                   NO caching (cache_on=False; the page never repeats within a cache TTL, so a
                   cachePoint would be all-cost-no-benefit). The historically-built shape;
                   default only by population-of-one, not rigor. Built → COVERED.
  page-centric   — hold one page, sweep every still over it while its cache is warm
                   (page-major). Page cached as prefix (cache_on=True). Built → COVERED.
                   DEFAULT (provisional): better-justified than still-centric (proven cost
                   win + evidence-grounded extraction), NOT proven-equivalent.

SELECTION precedence (resolved per run, recorded into the run manifest — L7 instance / L8):
  1. run manifest's recorded orchestration_strategy   (HIGHEST — an existing run reproduces)
  2. explicit orchestrator/CLI pass-in                (a new run overrides the default)
  3. registry effective-default                       (LOWEST — best BUILT default-intent)

NO-ORACLE DISCIPLINE (§9.2, R-orchestration-strategy-4): neither strategy is the authoritative
reference. Two strategies may surface a different-yet-materially-similar subset of a page's
findings — EXPECTED, not a defect. Cross-strategy difference is non-diagnostic; a strategy is
defective only by INTERNAL failure (findings that don't trace to evidence, or contradict each
other), adjudicated at reduce. still-centric remains first-class selectable regardless.

CLI:
    python orchestration_strategy.py registry                 # print the registry
    python orchestration_strategy.py resolve <manifest|-> [--passin S]
    python orchestration_strategy.py preamble                 # print the composed steering preamble
    python orchestration_strategy.py selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys


# ── The universal "rhumb-line" PREAMBLE (T-5) ────────────────────────────────────────────────
# ── STEERING_PREAMBLE: the static, cacheable steering prefix that precedes the page ──────────
# Named for its ROLE (steer the MAP-phase reasoning), NOT for any single reason it exists. Today
# its sole component is the rhumb-line block below (layout-invariance / common-bearing steering);
# it is a COMPOSITION of labeled blocks so future steering that is NOT about rhumb-line
# preservation (e.g. added format canonicalization or domain-neutrality framing) appends as a new
# block WITHOUT renaming the slot or fragmenting "the preamble" across constants. Whatever the
# blocks, STEERING_PREAMBLE is the whole composed prefix — so hashing it (run-identity
# preamble_hash) always covers all steering, and it sits inside the cached prefix for free.
#
# Still-agnostic + layout-agnostic: says nothing about WHAT to extract (the still's dynamic
# instruction owns that) — only HOW to reason and emit, tightly enough that positional layout
# (page-first vs instruction-first) cannot steer WHICH findings are selected.

# Rhumb-line block (the layout-invariance / common-bearing steer). Chosen by convergence
# measurement (spike bake-off winner "V3_framing"; enumerate-first variants HURT convergence).
# The MAP-phase boilerplate-suppression that _process_page historically prepended folds in here.
_RHUMB_LINE_BLOCK = (
    "MAP-PHASE EXTRACTION PROTOCOL (universal; applies identically regardless of where the "
    "source appears relative to these instructions).\n"
    "\n"
    "POSITION INVARIANCE: The SOURCE PAGE may appear either BEFORE or AFTER the task "
    "instructions in this context. Treat the two arrangements as identical — the same page and "
    "the same task always yield the same findings in the same order.\n"
    "\n"
    "SCAN ORDER (deterministic): Traverse the source STRICTLY top-to-bottom in its written "
    "order. Emit findings in the order their evidence FIRST appears. Do NOT reorder by perceived "
    "importance, confidence, or interest. The source's own order is the only ordering.\n"
    "\n"
    "EXHAUSTIVENESS BEFORE SELECTION: Surface EVERY item in the source that matches the task "
    "instruction's criterion — not a curated 'best few'. Do not stop early, summarize, or merge "
    "across findings. Completeness first; the downstream reduce phase handles curation.\n"
    "\n"
    "CANONICAL FRAMING: When a source construct admits multiple characterizations (e.g. a unique "
    "constraint is equally 'uniqueness', 'identity', or 'referential integrity'), ALWAYS name the "
    "finding after the SUBJECT it constrains, and report exactly ONE finding per distinct source "
    "construct — never split one construct into multiple lens-findings, never merge distinct "
    "constructs.\n"
    "\n"
    "OUTPUT SHAPE (fixed): Emit ONLY raw findings, one per '### ' header, fields inline under "
    "each. No preface, no 'How to Read This Document', no scoring-guide tables, no term "
    "definitions/glossaries, no document header or introduction, no closing summary. Reader-facing "
    "formatting is deferred to the reduce phase.\n"
    "\n"
    "GROUNDING: Every finding cites its source evidence (file + line range where present). Do not "
    "invent identifiers, values, or citations not present in the source.\n"
    "\n"
    "The task-specific extraction CRITERION follows as your instruction. Apply this protocol to it "
    "verbatim.\n"
)

# The composed steering prefix. Ordered blocks joined deterministically; add future steering by
# appending a labeled block here (its bytes then flow into the cached prefix AND the preamble_hash).
_STEERING_BLOCKS = (_RHUMB_LINE_BLOCK,)
STEERING_PREAMBLE = "\n".join(_STEERING_BLOCKS)


# ── the shared composite builder (T-4) ────────────────────────────────────────────────────────
def compose_map_request(page: str, instruction: str, *, cache_on: bool,
                        preamble: str = STEERING_PREAMBLE) -> tuple[list, list]:
    """Build the canonical Converse (system, messages) composite used by BOTH strategies.

    system   = [ {text: preamble}, {text: page} (, {cachePoint} if cache_on) ]
    messages = [ {role: user, content: [ {text: instruction} ]} ]

    cache_on plants a {"cachePoint": {"type": "default"}} after the page (the cacheable prefix)
    — ON for page-centric (page reused across a page's still sweep), OFF for still-centric (the
    page never repeats within a cache TTL, so a breakpoint is all-cost-no-benefit; omitting it
    is byte-for-byte the un-cached cost). The bytes of preamble+page are identical either way,
    so the two strategies differ only by the breakpoint, not the composition.
    """
    system: list = [{"text": preamble}, {"text": page}]
    if cache_on:
        system.append({"cachePoint": {"type": "default"}})
    messages = [{"role": "user", "content": [{"text": instruction}]}]
    return system, messages


# ── the strategies (name -> config) ─────────────────────────────────────────────────────────
# cache_on: whether the page prefix is marked cacheable under this strategy.
# page_major: loop order — True = hold a page, sweep its stills (page-major); False = still-major.
# name -> (cache_on, page_major, maturity, is_default_intent, description)
REGISTRY = {
    "still-centric": (False, False, "covered", False,
                      "one still swept over all its pages (still-major); page re-sent per still, "
                      "no caching; the historically-built shape (default by population-of-one)"),
    "page-centric": (True, True, "covered", True,
                     "hold one page, sweep every still over it while its cache is warm "
                     "(page-major); page cached as prefix; provisional default (cost-justified, "
                     "not proven-equivalent)"),
}


def cache_on(strategy: str) -> bool:
    _require(strategy)
    return REGISTRY[strategy][0]


def is_page_major(strategy: str) -> bool:
    _require(strategy)
    return REGISTRY[strategy][1]


def _require(strategy: str) -> None:
    if strategy not in REGISTRY:
        raise ValueError(f"unknown orchestration_strategy {strategy!r}; registered: {sorted(REGISTRY)}")


def effective_default() -> str:
    """The best BUILT (covered) strategy marked default-intent; falls back to the only/first
    covered strategy. Guards against defaulting to an unbuilt strategy (never default to vapor)."""
    built = [(n, d) for n, (co, pm, mat, d, _) in REGISTRY.items() if mat == "covered"]
    for name, is_default in built:
        if is_default:
            return name
    return built[0][0] if built else "still-centric"


def resolve(manifest_recorded: str | None, passin: str | None) -> str:
    """Precedence: recorded manifest strategy > orchestrator pass-in > effective default.
    An UNKNOWN recorded/pass-in strategy is an error (fail loud — never silently fall back and
    mislabel the run's provenance)."""
    for src, val in (("manifest", manifest_recorded), ("passin", passin)):
        if val:
            if val not in REGISTRY:
                raise ValueError(f"unknown orchestration_strategy {val!r} (from {src}); "
                                 f"registered: {sorted(REGISTRY)}")
            return val
    return effective_default()


def main() -> int:
    ap = argparse.ArgumentParser(description="Map orchestration strategy: still-centric | page-centric")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("registry", help="print the registered strategies")
    rs = sub.add_parser("resolve", help="print the effective strategy given manifest+passin")
    rs.add_argument("manifest_json", help="run manifest JSON path or '-' for none")
    rs.add_argument("--passin", default="")
    sub.add_parser("preamble", help="print the composed steering preamble")
    sub.add_parser("selftest", help="run built-in self-checks")
    args = ap.parse_args()

    if args.cmd == "registry":
        print(json.dumps({n: {"cache_on": co, "page_major": pm, "maturity": mat,
                              "default_intent": d, "desc": desc}
                          for n, (co, pm, mat, d, desc) in REGISTRY.items()}, indent=2))
        print(json.dumps({"effective_default": effective_default()}))
        return 0
    if args.cmd == "resolve":
        recorded = None
        if args.manifest_json and args.manifest_json != "-" and os.path.exists(args.manifest_json):
            try:
                recorded = json.load(open(args.manifest_json)).get("orchestration_strategy")
            except Exception:
                recorded = None
        print(resolve(recorded, args.passin or None))
        return 0
    if args.cmd == "preamble":
        print(STEERING_PREAMBLE)
        return 0
    if args.cmd == "selftest":
        assert effective_default() == "page-centric", "page-centric must be the effective default"
        assert cache_on("page-centric") is True and cache_on("still-centric") is False
        assert is_page_major("page-centric") is True and is_page_major("still-centric") is False
        assert resolve("still-centric", "page-centric") == "still-centric", "manifest wins over passin"
        assert resolve(None, "still-centric") == "still-centric", "passin wins over default"
        assert resolve(None, None) == "page-centric", "default is page-centric"
        try:
            resolve(None, "bogus"); assert False, "unknown strategy must raise"
        except ValueError:
            pass
        s_on, m_on = compose_map_request("PAGE", "INSTR", cache_on=True)
        s_off, m_off = compose_map_request("PAGE", "INSTR", cache_on=False)
        assert s_on[-1] == {"cachePoint": {"type": "default"}}, "cache_on plants cachePoint"
        assert "cachePoint" not in json.dumps(s_off), "cache_off omits cachePoint"
        # same composition bytes for the preamble+page prefix, differ only by breakpoint
        assert s_on[0] == s_off[0] and s_on[1] == s_off[1] and m_on == m_off
        # STEERING_PREAMBLE is the composed steering prefix; compose_map_request uses it by default
        assert STEERING_PREAMBLE == "\n".join(_STEERING_BLOCKS), "preamble must equal its blocks"
        assert _RHUMB_LINE_BLOCK in STEERING_PREAMBLE, "rhumb block must be inside the preamble"
        assert s_on[0] == {"text": STEERING_PREAMBLE}, "default preamble must be STEERING_PREAMBLE"
        print("orchestration_strategy selftest: OK")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
