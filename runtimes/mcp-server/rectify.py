#!/usr/bin/env python3
"""rectify.py — the LIVE rectification loop for never-seen-before file types (the "route + learn"
arm ratified by the owner). Realizes the contract documented in signal_classes.py:

    INTERROGATE (open/hybrid, deterministic-first) → VALIDATE (judge ladder, GRADE not gate)
      → ROUTE + LEARN (auto-bind by VERIFIED delta into the owned versioned map)  |  ESCALATE

WHY (SYSTEM_SPEC L4 omission/commission symmetry; L5 deterministic-first; L12 convergence).
A census turns up file types the ontology has never seen (the 2.1% frontier). Each MUST be
booked (omission guard — never silently dropped) and MUST NOT be force-fit/asserted (commission
guard — an LLM identification is a hypothesis, not a fact, until validated). This loop:

  1. INTERROGATE an ACTUAL instance sampled from the pages. Technique is OPEN + HYBRID and
     DETERMINISTIC-FIRST (L5): a structure/pattern sniff (filename family, XML/JSON/text/binary
     shape) runs first and CHEAP; the LLM reasons only on the residue the sniff can't settle.
     Produces an identity + signal-class HYPOTHESIS with a confidence, NEVER a fact.
  2. VALIDATE by the strongest available mechanism and GRADE (don't gate) — route WITH the
     confidence logged + propagated (owner's correction: "just log the uncertainty"):
       ladder a. structural/functional agreement — does the sniff's structural verdict cohere
                 with the hypothesis (e.g. hypothesis says config-wiring AND the bytes parse as
                 XML/JSON key-values)? behavioral-ish proof, cheap, strong when it fires.
       ladder b. INDEPENDENT judge — a SECOND independent LLM pass adjudicates the first
                 (convergence = confidence, L12; the judge pattern reused across this codebase);
                 disagreement lowers confidence + flags, never fabricates agreement.
     Higher-confidence validation later MONOTONICALLY upgrades a cell; confidence is never
     silently asserted.
  3. ROUTE + LEARN — confidence ≥ AUTO_BIND_THRESHOLD → persist the VERIFIED delta into the owned
     versioned overlay (learned_factorization.json) so next run classify() knows it, no
     re-interrogation. Self-extending by verified delta ONLY — never by silent mid-run mutation
     of the curated map.
  4. ESCALATE + PROPOSE-A-RIG — below threshold / conflicting → booked EXCLUDED, surfaced with an
     enriched ticket ("likely X carrying Y; ratify?") AND a concrete PATH-TO-COVER proposal
     (System L15 coverage-doctrine, clause 3): reason the reservoir's nature → emit either a
     DERIVED-TEMPLATE proposal (raw-extractable: the normalization gate is already covered because
     the container is text-native, so only an extraction template is needed — the VB.NET/_LANG_PROMPTS
     shape) or an INGEST-HELPER proposal (needs an intermediary to lift the container to text first —
     the .rpt→JRC shape, an ingest_helpers.IngestHelper spec with a path→TextStream contract +
     fallback ladder). The proposal is `exploration` — a named-but-unbuilt technique, EMITTED for a
     human to ratify + build, NEVER auto-installed (covered-requires-proof; the L4-commission guard
     at the coverage frontier). An LLM guess with no corroboration escalates, never auto-binds.

This is the code stills' own shape: hypothesize → check → keep the confirmed, flag the residual.
The framework is not the observer: a type it cannot validate is surfaced for a human, not asserted.

CLI:
  python rectify.py preflight --pages <dir> [--out report.json] [--limit N] [--commit]
      Survey census, rectify every UNKNOWN ext (deterministic sniff + optional LLM), report
      coverage/auto-bound/escalated. --commit writes verified deltas to the learned overlay;
      without it, a DRY RUN (grade everything, write nothing) — the --preflight go/no-go gate.
  python rectify.py show-learned        # print the current learned overlay
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import signal_classes as SC
from code_graph import marker_path as _marker_path  # canonical [FILE:] path parse
from ingest_helpers import _TEXT_NATIVE  # what resolves to `raw` with NO intermediary (normalize gate covered)

SCHEMA_VERSION = 1
AUTO_BIND_THRESHOLD = 0.80          # confidence at/above which a delta auto-binds (grade→route)
DEFAULT_MODEL = "anthropic.claude-haiku-4-5"   # same id + meter as the rest of the pipeline
PRICE_IN, PRICE_OUT = 1.0, 5.0                 # $/M tokens (Haiku 4.5 US)
_LEARNED_PATH = Path(
    os.environ.get(SC._LEARNED_PATH_ENV, str(SC._DEFAULT_LEARNED_PATH)))

# The prime signal-class vocabulary the interrogator/judge must choose FROM (never invent a class;
# an unmappable-but-signal-bearing type escalates as an ontology-expansion candidate).
_PRIME_CLASSES = sorted(SC.PRIME_SIGNAL_CLASSES.keys())


# ── deterministic structure sniff (L5: cheap, first, reason only on residue) ──────────────────
def _strip_preamble(sample: str) -> str:
    """Strip UTF-8 BOM + leading whitespace so shape detection sees the real first token. (BOM is
    NOT whitespace, so a bare lstrip() leaves it and breaks '<?xml' / '{' detection — the bug that
    made BOM-prefixed VS-generated XML register as 'text'.)"""
    return (sample or "").lstrip("\ufeff").lstrip()


def _xml_body_after_prolog(s: str) -> str:
    """Return the XML content with the <?xml …?> declaration and leading <!-- … --> comments
    removed, so the ROOT element is the first tag seen (VS files put a boilerplate comment first)."""
    s = re.sub(r"^\s*<\?xml[^>]*\?>", "", s)
    while True:
        s2 = re.sub(r"^\s*<!--.*?-->", "", s, count=1, flags=re.S)
        if s2 == s:
            return s.lstrip()
        s = s2


def _sniff_structure(sample: str, name: str) -> dict:
    """Cheap deterministic read of an instance's shape. Returns {shape, signals[], confidence}
    where shape ∈ {xml, json, keyvalue, markup, text, binary, empty}. No LLM."""
    s = _strip_preamble(sample)
    if not s:
        return {"shape": "empty", "signals": [], "confidence": 0.0}
    # binary heuristic: NUL byte or high proportion of non-text
    nontext = sum(1 for ch in sample[:2000] if ord(ch) < 9 or (13 < ord(ch) < 32))
    if "\x00" in sample[:2000] or nontext > 40:
        return {"shape": "binary", "signals": ["binary"], "confidence": 0.5}
    signals = []
    shape = "text"
    if s[:1] == "{" or s[:1] == "[":
        try:
            json.loads(s)
            shape = "json"; signals.append("parses-json")
        except Exception:
            if re.search(r'"\w+"\s*:', s):
                shape = "json"; signals.append("json-like")
    # XML/markup: starts with <?xml, or an element/comment/PI/DOCTYPE angle bracket
    if shape == "text" and (s[:5].lower().startswith("<?xml") or re.match(r"<[A-Za-z!?]", s)):
        shape = "markup" if re.search(r"</(html|body|div|span|p)\b", s, re.I) else "xml"
        signals.append("angle-bracket-markup")
    if shape == "text" and re.search(r"^\s*[\w.\-]+\s*=\s*.+$", s[:2000], re.M):
        shape = "keyvalue"; signals.append("key=value")
    # topical token signals (weak, corroborating only)
    low = s[:4000].lower()
    for tok, sig in (("<projectreference", "dep-ref"), ("packagereference", "dep-ref"),
                     ("add_subdirectory", "cmake-dep"), ("\"dependencies\"", "npm-dep"),
                     ("connectionstring", "config-conn"), ("<dataset", "report-data"),
                     ("select ", "sql-ish"), ("<setting", "settings")):
        if tok in low:
            signals.append(sig)
    conf = 0.7 if shape in ("json", "xml", "markup", "keyvalue") else 0.3
    return {"shape": shape, "signals": signals, "confidence": conf}


# structural shape → the prime classes it is CONSISTENT with (used to validate a hypothesis; a
# hypothesis coherent with the deterministic shape earns a confidence bump — ladder rung a).
_SHAPE_CONSISTENT = {
    "xml": {"config-wiring", "data-model", "dependency-topology", "report-definition",
            "presentation-ui", "interface-contract"},
    "markup": {"presentation-ui"},
    "json": {"config-wiring", "dependency-topology", "data-model"},
    "keyvalue": {"config-wiring", "domain-vocabulary"},
    "text": set(_PRIME_CLASSES),   # text can be anything textual
    "binary": {"visual-diagram"},
    "empty": set(),
}


# ── structural SIGNATURE + convolver (the cheap "hot-dog / not-hot-dog" discriminator) ─────────
# A signature is a DETERMINISTIC structural fingerprint extracted from a seed instance — root XML
# element+namespace, JSON top-level key shape, key=value schema, DOCTYPE, magic bytes, or a
# characteristic-token set. The convolver `_signature_matches(seed_sig, other)` is a cheap boolean:
# does `other` exhibit the SAME structural shape? This is what lets ONE LLM identification amortize
# across a whole homogeneous cohort — and, crucially, lets a HETEROGENEOUS extension (50 `.settings`
# of different kinds) split into multiple cohorts instead of projecting the first file's meaning
# onto all. No LLM in the convolver (L5: reason once per cohort, fan out cheaply).
def _signature(sample: str, name: str) -> dict:
    """Extract a deterministic structural signature from one instance. Keys are stable across
    same-kind files and differ across different-kind files of the same extension."""
    sniff = _sniff_structure(sample, name)
    shape = sniff["shape"]
    sig = {"shape": shape, "root": "", "keys": [], "doctype": "", "magic": ""}
    if shape == "binary":
        # opaque: the only cheap same-kind discriminator is the leading magic-byte prefix. Files
        # of one binary family (all .cab, all .png) share it → ONE cohort + ONE escalation ticket,
        # instead of N singleton cohorts each burning an LLM call on bytes it cannot read.
        sig["magic"] = (sample or "")[:8].encode("utf-8", "replace").hex()
        return sig
    s = _strip_preamble(sample)
    if shape in ("xml", "markup"):
        dm = re.search(r"<!DOCTYPE\s+([^\s>]+)", s, re.I)
        if dm:
            sig["doctype"] = dm.group(1).lower()
        # skip the <?xml?> declaration + leading comments so the ROOT is the first real element
        body = _xml_body_after_prolog(s)
        m = re.search(r"<([A-Za-z_][\w.\-]*)((?:\s[^>]*)?)>", body)
        if m:
            root = m.group(1).lower()
            attrs = m.group(2) or ""
            nsm = re.search(r'xmlns(?::\w+)?\s*=\s*"([^"]+)"', attrs)
            sig["root"] = root + ("|" + nsm.group(1) if nsm else "")
    elif shape == "json":
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                sig["keys"] = sorted(obj.keys())[:12]
            elif isinstance(obj, list):
                sig["keys"] = ["[array]"]
        except Exception:
            sig["keys"] = sorted(set(re.findall(r'"(\w+)"\s*:', s)))[:12]
    elif shape == "keyvalue":
        sig["keys"] = sorted({m.group(1).lower()
                              for m in re.finditer(r"(?m)^\s*([\w.\-]+)\s*=", s)})[:12]
    return sig


def _signature_matches(seed_sig: dict, sample: str, name: str) -> tuple[bool, float]:
    """Cheap convolver: does `sample` share the seed's structural signature? Returns (passes,
    strength). A pass REQUIRES a discriminating feature to agree (never a rubber stamp: shape
    alone is not enough for text/binary — those need the seed to have a real discriminator)."""
    other = _signature(sample, name)
    if other["shape"] != seed_sig["shape"]:
        return False, 0.0
    shape = seed_sig["shape"]
    if shape == "binary":
        # same binary family = same magic-byte prefix (all .cab share it). We can't read the bytes;
        # magic agreement is the honest same-kind signal → one cohort, one escalation for the family.
        ok = bool(seed_sig.get("magic")) and other.get("magic") == seed_sig["magic"]
        return ok, 0.6 if ok else 0.0
    if shape in ("xml", "markup"):
        if not seed_sig["root"]:
            # seed had no extractable root → weak; require doctype agreement or fail
            ok = bool(seed_sig["doctype"]) and other["doctype"] == seed_sig["doctype"]
            return ok, 0.6 if ok else 0.0
        ok = other["root"] == seed_sig["root"]
        return ok, 0.95 if ok else 0.0
    if shape == "json":
        sk, ok_ = set(seed_sig["keys"]), set(other["keys"])
        if not sk:
            return (other["keys"] == []), 0.5
        jac = len(sk & ok_) / len(sk | ok_) if (sk | ok_) else 0.0
        return (jac >= 0.6), round(jac, 2)
    if shape == "keyvalue":
        sk, ok_ = set(seed_sig["keys"]), set(other["keys"])
        if not sk:
            return True, 0.4       # both key=value with no extractable keys → weak same-kind
        jac = len(sk & ok_) / len(sk | ok_) if (sk | ok_) else 0.0
        return (jac >= 0.5), round(jac, 2)
    # text / binary / empty: shape agreement alone is NOT a discriminator → do NOT auto-cohort;
    # each such file is its own cohort (forces per-file identification, honest for opaque blobs).
    return False, 0.0


# ── LLM interrogate + independent judge (reason ONLY on the residue the sniff can't settle) ────
_INTERROGATE_SYS = (
    "You identify an unknown source file type for a code-distillation ontology. You are given a "
    "filename and a content sample. Decide what KIND of file it is and which SIGNAL CLASSES it "
    "carries, choosing ONLY from the allowed class list. Do NOT invent classes. If the file "
    "carries real signal but no allowed class fits, return an empty classes list and set "
    "\"ontology_expansion\": true with a short proposed_class name. Be honest about uncertainty. "
    "\"is_prior_spec_document\" is TRUE ONLY if this file is an AUTHORED PRIOR-SPECIFICATION "
    "document exported from a human authoring tool (a Word/PDF spec, a Confluence/JIRA export, a "
    "requirements doc) — NOT merely a config/project/settings file that 'declares' values. A "
    "source, config, project, build, or settings file is NOT a prior-spec document. When in "
    "doubt, FALSE. Output ONLY a JSON object: {\"identity\": str, \"classes\": [str], "
    "\"is_prior_spec_document\": bool, \"confidence\": 0.0-1.0, \"ontology_expansion\": bool, "
    "\"proposed_class\": str, \"rationale\": str}."
)
_JUDGE_SYS = (
    "You are an INDEPENDENT adjudicator. Another pass classified an unknown file type. Given the "
    "same filename + sample and that proposed classification, decide if it is correct. Choose "
    "classes ONLY from the allowed list. Agreement raises confidence; disagreement must be stated "
    "plainly (do not rubber-stamp). Output ONLY JSON: {\"agree\": bool, \"classes\": [str], "
    "\"confidence\": 0.0-1.0, \"rationale\": str}."
)


def _call_json(llm, model, system, user, max_tokens=400):
    """One bounded-emit LLM call → parsed object. validate→recast-once→None (never fabricate)."""
    import bedrock_client
    import json_extract
    cur = user
    for attempt in range(2):
        try:
            res = bedrock_client.call_llm(llm, model, system, cur,
                                             max_tokens=max_tokens, temperature=0.0)
        except Exception as e:
            return None, 0, 0, f"call_error: {type(e).__name__}: {e}"
        ti, to = res.input_tokens, res.output_tokens
        try:
            return (json.loads(json_extract.strip_json(res.output_text, "object")), ti, to, None)
        except Exception as e:
            cur = (user + "\n\nYour previous reply was not valid JSON (" + str(e)[:80] +
                   "). Reply with ONLY the JSON object, nothing else.")
    return None, ti, to, "unparseable"


def _allowed(classes) -> list:
    """Keep only real prime classes (commission guard — the model cannot invent a class)."""
    return sorted({c for c in (classes or []) if c in SC.PRIME_SIGNAL_CLASSES})


def interrogate(name: str, sample: str, llm=None, model=None) -> dict:
    """Hybrid identity hypothesis. Deterministic sniff first; LLM only if a client is provided and
    the sniff is not already decisive. Returns a hypothesis dict (never a committed fact)."""
    sniff = _sniff_structure(sample, name)
    hyp = {"identity": None, "classes": [], "provenance": "recovered", "confidence": 0.0,
           "ontology_expansion": False, "proposed_class": "", "rationale": "",
           "sniff": sniff, "method": "sniff", "in_tok": 0, "out_tok": 0, "error": None}
    if llm is None or model is None:
        # deterministic-only: we can bound the SHAPE but not assign classes with confidence.
        hyp["rationale"] = f"structural sniff only: shape={sniff['shape']} signals={sniff['signals']}"
        return hyp
    user = (f"Filename: {name}\nAllowed classes: {_PRIME_CLASSES}\n"
            f"Deterministic structure sniff: {sniff}\n\nContent sample (truncated):\n"
            f"{sample[:3000]}")
    obj, ti, to, err = _call_json(llm, model, _INTERROGATE_SYS, user)
    hyp["in_tok"], hyp["out_tok"], hyp["method"] = ti, to, "sniff+llm"
    if err or obj is None:
        hyp["error"] = err or "no_object"
        return hyp
    hyp["identity"] = str(obj.get("identity") or "")[:120]
    hyp["classes"] = _allowed(obj.get("classes"))
    # Provenance is STRUCTURAL, not an LLM free-choice. Only true prior-spec document containers
    # (Word/PDF/Confluence-export) are 'declared'; everything else from a code tree is 'recovered'.
    hyp["provenance"] = "declared" if obj.get("is_prior_spec_document") else "recovered"
    hyp["confidence"] = max(0.0, min(1.0, float(obj.get("confidence") or 0.0)))
    hyp["ontology_expansion"] = bool(obj.get("ontology_expansion"))
    hyp["proposed_class"] = str(obj.get("proposed_class") or "")[:60]
    hyp["rationale"] = str(obj.get("rationale") or "")[:300]
    return hyp


def validate(name: str, sample: str, hyp: dict, llm=None, model=None) -> dict:
    """Judge ladder, GRADE not gate. Returns {confidence, agree, classes, flags[], in_tok, out_tok}.
    Ladder a (structural coherence, deterministic) always runs; ladder b (independent judge) runs
    only if an LLM client is present. Confidence is COMBINED, never silently asserted."""
    flags = []
    # ── rung a: structural coherence (deterministic, cheap) ──
    shape = hyp["sniff"]["shape"]
    consistent = _SHAPE_CONSISTENT.get(shape, set())
    hyp_classes = set(hyp["classes"])
    if hyp_classes and shape not in ("text", "empty"):
        coherent = hyp_classes <= consistent
        struct_conf = 0.85 if coherent else 0.35
        if not coherent:
            flags.append(f"hypothesis {sorted(hyp_classes - consistent)} inconsistent with shape={shape}")
    else:
        struct_conf = 0.5  # text/empty shape can't corroborate class choice structurally
    out = {"struct_confidence": struct_conf, "agree": None, "judge_classes": [],
           "flags": flags, "in_tok": 0, "out_tok": 0, "error": None}
    if llm is None or model is None or not hyp_classes:
        # no independent judge available (or nothing to judge): confidence = sniff·struct blend
        out["confidence"] = round(min(hyp["confidence"], struct_conf) if hyp_classes
                                  else 0.0, 3)
        return out
    # ── rung b: independent judge (L12 convergence) ──
    user = (f"Filename: {name}\nAllowed classes: {_PRIME_CLASSES}\n"
            f"Proposed: identity={hyp['identity']!r} classes={hyp['classes']} "
            f"provenance={hyp['provenance']}\nStructure sniff: {hyp['sniff']}\n\n"
            f"Content sample (truncated):\n{sample[:3000]}\n\nIs the proposed classification correct?")
    obj, ti, to, err = _call_json(llm, model, _JUDGE_SYS, user, max_tokens=300)
    out["in_tok"], out["out_tok"] = ti, to
    if err or obj is None:
        out["error"] = err or "no_object"
        out["confidence"] = round(min(hyp["confidence"], struct_conf) * 0.7, 3)  # penalize: unjudged
        flags.append("judge_unavailable")
        return out
    agree = bool(obj.get("agree"))
    judge_classes = _allowed(obj.get("classes"))
    judge_conf = max(0.0, min(1.0, float(obj.get("confidence") or 0.0)))
    out["agree"], out["judge_classes"] = agree, judge_classes
    # convergence: interrogator ∩ judge classes; agreement + structural coherence → high confidence
    inter = hyp_classes & set(judge_classes)
    if agree and inter == hyp_classes and hyp_classes:
        combined = min(0.99, (hyp["confidence"] + judge_conf) / 2 * (0.6 + 0.4 * struct_conf) + 0.1)
    elif inter:
        combined = min(0.75, (hyp["confidence"] + judge_conf) / 2 * struct_conf)
        flags.append(f"partial judge agreement: interrogator={sorted(hyp_classes)} judge={judge_classes}")
    else:
        combined = 0.2
        flags.append(f"judge DISAGREES: judge={judge_classes} vs proposed={sorted(hyp_classes)}")
    out["confidence"] = round(combined, 3)
    return out


# ── ROUTE + LEARN: persist a VERIFIED delta into the owned, versioned overlay ─────────────────
def _load_overlay() -> dict:
    if _LEARNED_PATH.exists():
        try:
            return json.loads(_LEARNED_PATH.read_text())
        except Exception:
            pass
    return {"schema_version": SCHEMA_VERSION, "entries": {}}


def _cohort_universal(ext: str, sig: dict, cohort_size: int, ext_total: int) -> bool:
    """Heuristic: does this cohort look ECOSYSTEM-UNIVERSAL (a property of the file TYPE) vs
    INSTANCE-IDIOSYNCRATIC (a property of this codebase)? Universal ⇒ a graduation candidate for
    the shipped curated map (human-ratified, never auto). Signal: the cohort covers (near) ALL
    instances of the ext (homogeneous ecosystem type) AND has a strong structural discriminator
    (named XML root/namespace or a stable key schema). A cohort that is one of several for an ext
    is by definition idiosyncratic (the ext is polymorphic here). GRADUATION IS PROPOSED, NOT
    APPLIED — this only flags a candidate."""
    homogeneous = ext_total > 0 and cohort_size / ext_total >= 0.95
    strong = bool(sig.get("root")) or (len(sig.get("keys") or []) >= 3)
    return homogeneous and strong


def route_and_learn(ext: str, sig: dict, cohort_paths: list, hyp: dict, val: dict,
                    ext_total: int, commit: bool) -> dict:
    """Persist a verified COHORT delta additively, keyed by (ext + structural signature) so a
    polymorphic extension yields MULTIPLE cohort entries (never one projected meaning). NEVER
    overwrites a curated classification (classify() consults curated first; the overlay only fills
    unknowns) and NEVER downgrades a higher-confidence entry for the same key. Reversible: delete
    the overlay → curated-only. `graduation_candidate` flags ecosystem-universal cohorts for
    human-ratified promotion to the curated map (proposed, never auto-applied). Returns the entry."""
    key = _cohort_key(ext, sig)
    entry = {
        "ext": ext,
        "signature": sig,
        "classes": sorted(hyp["classes"]),
        "provenance": hyp["provenance"],
        "confidence": val["confidence"],
        "identity": hyp["identity"],
        "method": hyp["method"],
        "cohort_size": len(cohort_paths),
        "ext_total": ext_total,
        "seed_path": cohort_paths[0] if cohort_paths else "",
        "graduation_candidate": _cohort_universal(ext, sig, len(cohort_paths), ext_total),
        "evidence": {"flags": val.get("flags", []), "judge_agree": val.get("agree")},
        "validated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "schema_version": SCHEMA_VERSION,
    }
    if not commit:
        return entry
    overlay = _load_overlay()
    # cohort deltas live under a distinct 'cohorts' map keyed by ext+signature; a compact
    # 'entries' map (ext -> classes) is ALSO maintained for classify() BUT only for exts that are
    # MONO-cohort (homogeneous) — a polymorphic ext is deliberately NOT written to 'entries', so
    # classify() leaves it UNKNOWN and each file is re-cohorted at use-time (never mis-projected).
    cohorts = overlay.setdefault("cohorts", {})
    prev = cohorts.get(key)
    if not (prev and prev.get("confidence", 0) >= entry["confidence"]):
        cohorts[key] = entry
    overlay["schema_version"] = SCHEMA_VERSION
    _LEARNED_PATH.write_text(json.dumps(overlay, indent=2, sort_keys=True))
    SC.reset_learned_cache()
    return entry


def _cohort_key(ext: str, sig: dict) -> str:
    """Stable key for a cohort = ext + a compact rendering of its structural signature."""
    disc = sig.get("root") or sig.get("doctype") or ",".join(sig.get("keys") or []) or sig.get("shape", "")
    return f"{ext}#{sig.get('shape','?')}:{disc}"[:160]


def finalize_ext_entries(commit: bool) -> int:
    """After all cohorts for a run are written, promote MONO-cohort exts (homogeneous — exactly one
    cohort covering the ext) into the compact 'entries' map classify() reads. Polymorphic exts
    (>1 cohort) are intentionally left out of 'entries' → they stay UNKNOWN to classify() and are
    re-cohorted per-file at use-time (no first-file projection). Returns count promoted."""
    if not commit:
        return 0
    overlay = _load_overlay()
    cohorts = overlay.get("cohorts", {})
    by_ext = defaultdict(list)
    for k, e in cohorts.items():
        by_ext[e["ext"]].append(e)
    entries = overlay.setdefault("entries", {})
    promoted = 0
    for ext, cos in by_ext.items():
        if len(cos) == 1 and cos[0].get("classes"):
            c = cos[0]
            entries[ext] = {"classes": c["classes"], "provenance": c["provenance"],
                            "confidence": c["confidence"], "cohort_key": _cohort_key(ext, c["signature"]),
                            "polymorphic": False}
            promoted += 1
        else:
            # polymorphic: record the fact (audit) but do NOT give classify() a single mapping
            entries.pop(ext, None)
    overlay["entries"] = entries
    _LEARNED_PATH.write_text(json.dumps(overlay, indent=2, sort_keys=True))
    SC.reset_learned_cache()
    return promoted


# ── PROPOSE-A-RIG: reason a PATH-TO-COVER for a surfaced untapped reservoir (System L15) ──────
# The coverage-doctrine's third clause made mechanical. When a reservoir can't auto-bind (it
# escalated), we don't just book it — we reason WHAT WOULD BE NEEDED to cover it and emit a
# concrete, near-spec proposal, generalizing what the .rpt→JRC episode did by hand.
#
# The decision uses the ontology's TWO-GATE cell model (signal_classes _EXTRACTION_RIGS header):
#   normalization gate — can the container be turned into rig-compatible TEXT?
#   extraction gate    — does a rig recover signal from that text?
# A file that is TEXT-NATIVE has its normalization gate already covered (content IS the text) —
# the only weak gate is EXTRACTION → propose a DERIVED TEMPLATE (the code_graph _LANG_PROMPTS /
# still-template path; add to the covered-set on ratify). A file that is NOT text-native needs an
# INTERMEDIARY first — the normalization gate is weak → propose an INGEST-HELPER spec (the
# ingest_helpers.IngestHelper contract: handles/available/lift, format, intermediary,
# path→TextStream, fallback ladder), exactly the shape the .rpt→JRC helper realized.
#
# EVERY proposal is maturity `exploration`: a NAMED technique, UNBUILT — never `covered`
# (covered-requires-proof; a proposed rig is EXPLORATION until built + validated, the L4-commission
# guard at the coverage frontier). The proposal is EMITTED for a human to ratify + build; it is
# NEVER auto-installed into the registry or the covered-set.

# Structural shape → the raw-extraction technique that would cover it (all `exploration`; the
# extraction gate's named-but-unbuilt rig for a text-native container of that shape).
_RAW_TECHNIQUE = {
    "xml":      "deterministic XML parse → element/attr signal; LLM interpretive pass on residue",
    "markup":   "DOM/markup structure parse → presentation/text signal",
    "json":     "deterministic JSON parse → key/shape signal; LLM interpretive pass on residue",
    "keyvalue": "key=value line parse → config/vocabulary term extraction",
    "text":     "per-language extraction template (code_graph _LANG_PROMPTS entry) at temp 0.0, "
                "back-validated (cited symbols must ground to source) — the VB.NET rig's shape",
}


def propose_rig(ext: str, seed_sig: dict, hyp: dict, sample_name: str) -> dict:
    """Reason a PATH-TO-COVER for one surfaced-untapped reservoir (grade-not-gate; a PROPOSAL, not
    an install). Returns a structured proposal:
      kind            : "derived-template" (raw-extractable) | "ingest-helper" (needs intermediary)
      maturity        : always "exploration" (named technique, unbuilt — covered-requires-proof)
      target_classes  : the prime signal classes the reservoir is hypothesized to carry (or the
                        proposed_class if the ontology needs expansion) — never invented
      normalization/extraction: the two-gate verdict (which gate is weak, what covers it)
      contract        : for ingest-helper, the IngestHelper spec fields to implement; for
                        derived-template, the _LANG_PROMPTS/still-template shape to add
      fallback_ladder : the grade-not-gate degradation (helper unavailable → next → referential)
    Deterministic — no LLM (the interrogation already spent its call; this reasons over the sniff +
    hypothesis)."""
    shape = (seed_sig or {}).get("shape", "text")
    is_text_native = (ext in _TEXT_NATIVE) or shape in ("xml", "markup", "json", "keyvalue", "text")
    # target classes: prefer validated hypothesis classes; else the proposed ontology-expansion
    # class (clearly marked as needing ratification); never fabricate a class.
    target_classes = sorted(hyp.get("classes") or [])
    proposed_class = hyp.get("proposed_class") or ""
    needs_ontology_expansion = bool(hyp.get("ontology_expansion")) and not target_classes

    base = {
        "ext": ext,
        "maturity": "exploration",   # NEVER covered — a proposed rig is exploration until built+validated
        "target_classes": target_classes,
        "proposed_ontology_class": proposed_class if needs_ontology_expansion else "",
        "needs_ontology_expansion": needs_ontology_expansion,
        "hypothesis_identity": hyp.get("identity") or "",
        "shape": shape,
        "status": "proposed",        # exploration, awaiting human ratify + build (never auto-installed)
        "commission_guard": ("this is a PROPOSAL, not a covered rig — build it, then a passing "
                             "functional test / back-validation graduates it (covered-requires-proof)"),
    }

    if is_text_native:
        technique = _RAW_TECHNIQUE.get(shape, _RAW_TECHNIQUE["text"])
        base.update({
            "kind": "derived-template",
            "normalization_gate": {"maturity": "covered",
                                   "why": "text-native: the file content IS rig-compatible text; "
                                          "no intermediary needed (ingest_helpers → raw)"},
            "extraction_gate": {"maturity": "exploration", "technique": technique},
            "contract": {
                "add_to": ("code_graph._LANG_PROMPTS (a {lang: (SYSTEM, USER)} entry) if this is a "
                           "programming language; else a still-template / deterministic parser for "
                           "the shape"),
                "shape": shape,
                "extraction": technique,
                "back_validation": ("REQUIRED before graduation: recovered symbols/edges must ground "
                                    "to a literal source token (L4 — commission flagged if invented, "
                                    "omission flagged if a source token has no recovered signal)"),
                "on_ratify": "add the ext to code_graph.CODE_EXTS / the covered-set (seed grows by one)",
            },
        })
    else:
        # opaque / binary → an intermediary is required (the normalization gate is the weak one).
        magic = (seed_sig or {}).get("magic", "")
        base.update({
            "kind": "ingest-helper",
            "normalization_gate": {"maturity": "exploration",
                                   "why": "NOT text-native: an intermediary must lift the container "
                                          "to text before any rig can read it (the .rpt→JRC case)"},
            "extraction_gate": {"maturity": "exploration",
                                "technique": "run the ontology's rig for the resulting text shape "
                                             "once the helper emits it"},
            "contract": {
                "implements": "ingest_helpers.IngestHelper",
                "id": f"{ext}-helper",
                "runtime": "local (shell/subprocess) or remote (SaaS/API) — declare which",
                "handles": f"_ext(path) == {ext!r}",
                "available": ("probe the intermediary is installed (binary/jars/lib present or the "
                              "API reachable) → skip to fallback if not (grade-not-gate)"),
                "lift": ("path → LiftResult{text, method, confidence, notes}; the DERIVED text is "
                         "`inferred` kind (line-numbered against the DERIVED artifact, NOT source — "
                         "auditable, anti-hallucination)"),
                "intermediary": ("NAME the tool/library/service that decodes this format "
                                 f"(magic-bytes prefix seen: {magic or 'n/a'}); the .rpt→JRC helper "
                                 "is the reference instance"),
                "path_to_textstream": "path → TextStream (the one narrow contract, MCP-tool-like)",
            },
            "fallback_ladder": [
                "1. the proposed helper (available + returns text) → inferred",
                "2. a weaker envelope/metadata helper if one exists (e.g. OLE envelope) → inferred (low conf)",
                "3. none available → referential (booked present, marker+size only, NO body — no token waste)",
            ],
        })
    return base


# ── sample a real instance of an ext from the pages (interrogate an ACTUAL file, never a guess) ─
def _collect_instances(pages_dir: Path, ext: str, max_bytes: int = 4000,
                       cap: int = 200) -> list[tuple[str, str]]:
    """Return [(relpath, content-sample), …] for census files with this ext (up to `cap`). The
    cover-and-partition loop needs the WHOLE worklist for an ext — not one sample — so a
    heterogeneous extension can be split into cohorts instead of projecting the first file's
    meaning onto all. Reads the ACTUAL bytes from the reconstructed [FILE:] segments."""
    target = ext.lower()
    out: list[tuple[str, str]] = []
    seen = set()
    for page in sorted(pages_dir.glob("page_*")):
        concat = page / "concat.txt"
        if not concat.exists():
            concat = next(iter(page.glob("concat*")), None)
        if not concat or not concat.exists():
            continue
        text = concat.read_text(errors="replace")
        # Anchor the marker to its own line: a [PART k/n] attribute nests brackets, so an
        # unanchored `.*?\]` would stop inside the marker and leak its tail into the body.
        for m in re.finditer(r"^\[FILE:\s*(.*?)\][ \t]*\n(.*?)^\[END FILE:", text, re.S | re.M):
            path = _marker_path(m.group(1))  # strip KIND/SIZE/VIA/PART attributes → bare path
            e = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
            if e != target or path in seen:
                continue
            seen.add(path)
            body = re.sub(r"(?m)^\s*\d+\s*\|\s?", "", m.group(2))  # strip "NNN | " prefixes
            out.append((path, body[:max_bytes]))
            if len(out) >= cap:
                return out
    return out


# ── the driver: rectify every UNKNOWN census ext (preflight go/no-go) ─────────────────────────
def rectify_census(pages_dir: Path, commit: bool, limit: int | None,
                   profile: str, region: str, model: str, use_llm: bool) -> dict:
    import code_graph as CG
    census = Counter()
    for _, ext, _ in CG.census_source_files(pages_dir):
        census[ext] += 1
    # UNKNOWN = classify() says not-known (curated + existing-learned both miss it)
    unknowns = [(ext, n) for ext, n in census.most_common()
                if not SC.classify("x." + ext if ext else "x")[1]]
    if limit:
        unknowns = unknowns[:limit]

    llm = None
    if use_llm:
        import bedrock_client
        llm = bedrock_client.create_llm_client(profile, region)

    counters = {"tot_in": 0, "tot_out": 0}

    def _identify(paths_samples, ext, n, results, i, total):
        """COVER-AND-PARTITION: peel homogeneous cohorts off an ext's worklist. Each pass takes the
        first remaining file as SEED, identifies it ONCE (LLM), builds a cheap structural convolver
        from it, and sweeps the remainder — matches join the cohort (no LLM), non-matches fall to
        the PassBucket (a strict subset). Repeat on the PassBucket until empty. A homogeneous ext =
        1 LLM call amortized over all N; a polymorphic ext splits into as many cohorts as it has
        real kinds (never projects the first file's meaning onto the rest)."""
        work = list(paths_samples)
        cohort_idx = 0
        while work:
            cohort_idx += 1
            seed_path, seed_sample = work[0]
            seed_sig = _signature(seed_sample, seed_path)
            hyp = interrogate(seed_path, seed_sample, llm, model if use_llm else None)
            val = validate(seed_path, seed_sample, hyp, llm, model if use_llm else None)
            counters["tot_in"] += hyp["in_tok"] + val["in_tok"]
            counters["tot_out"] += hyp["out_tok"] + val["out_tok"]
            conf = val.get("confidence", 0.0)
            # sweep remainder with the cheap convolver (NO LLM): same-signature → this cohort
            cohort = [(seed_path, seed_sample)]
            pass_bucket = []
            for g_path, g_sample in work[1:]:
                ok, _strength = _signature_matches(seed_sig, g_sample, g_path)
                (cohort if ok else pass_bucket).append((g_path, g_sample))
            cohort_paths = [p for p, _ in cohort]
            rec = {"ext": ext, "ext_total": n, "cohort_index": cohort_idx,
                   "cohort_size": len(cohort_paths), "seed_path": seed_path,
                   "signature": seed_sig, "identity": hyp["identity"],
                   "classes": sorted(hyp["classes"]), "provenance": hyp["provenance"],
                   "confidence": conf, "flags": val.get("flags", []),
                   "ontology_expansion": hyp["ontology_expansion"],
                   "proposed_class": hyp["proposed_class"], "method": hyp["method"],
                   "error": hyp.get("error") or val.get("error")}
            if hyp["classes"] and conf >= AUTO_BIND_THRESHOLD and not hyp["ontology_expansion"]:
                entry = route_and_learn(ext, seed_sig, cohort_paths, hyp, val, n, commit)
                rec["action"] = "auto-bound" + ("" if commit else " (dry-run)")
                rec["cohort_key"] = _cohort_key(ext, seed_sig)
                rec["graduation_candidate"] = entry.get("graduation_candidate", False)
                results["auto_bound"].append(rec)
            else:
                rec["action"] = "escalate"
                # PATH-TO-COVER (System L15): reason a concrete rig proposal for the surfaced
                # reservoir — raw-extractable → derived template, else → ingest-helper spec. This is
                # grade-not-gate EXPLORATION (a proposal to ratify+build, never an auto-install).
                rec["rig_proposal"] = propose_rig(ext, seed_sig, hyp, seed_path)
                _prop = rec["rig_proposal"]
                rec["ticket"] = (f"Unknown .{ext} cohort {cohort_idx} ({len(cohort_paths)}/{n} files, "
                                 f"sig={seed_sig.get('shape')}:{seed_sig.get('root') or seed_sig.get('keys')}). "
                                 f"Likely: {hyp['identity'] or '?'} carrying "
                                 f"{sorted(hyp['classes']) or hyp['proposed_class'] or '?'} (conf {conf}). "
                                 f"{'; '.join(val.get('flags', [])) or 'low corroboration'}. "
                                 f"PATH-TO-COVER: {_prop['kind']} (maturity=exploration) — "
                                 f"{'add _LANG_PROMPTS/parser template' if _prop['kind']=='derived-template' else 'build an IngestHelper (path→TextStream)'}. Ratify + build?")
                results["escalated"].append(rec)
            print(f"[rectify {i}/{total}] .{ext} cohort {cohort_idx}: {len(cohort_paths)}/{n} "
                  f"→ {rec['action']} conf={conf} classes={sorted(hyp['classes'])}", file=sys.stderr)
            work = pass_bucket  # strict subset → terminates

    results = {"auto_bound": [], "escalated": [], "no_instance": []}
    t0 = _dt.datetime.now()
    for i, (ext, n) in enumerate(unknowns, 1):
        instances = _collect_instances(pages_dir, ext)
        if not instances:
            results["no_instance"].append({"ext": ext, "count": n})
            print(f"[rectify {i}/{len(unknowns)}] .{ext} ({n}) — NO instance sample found",
                  file=sys.stderr)
            continue
        _identify(instances, ext, n, results, i, len(unknowns))

    promoted = finalize_ext_entries(commit)
    elapsed = (_dt.datetime.now() - t0).total_seconds()
    ti, to = counters["tot_in"], counters["tot_out"]
    cost = ti / 1e6 * PRICE_IN + to / 1e6 * PRICE_OUT  # Haiku meter
    polymorphic = sorted({r["ext"] for r in results["auto_bound"] + results["escalated"]
                          if r.get("cohort_index", 1) > 1 or
                          sum(1 for x in results["auto_bound"] + results["escalated"]
                              if x["ext"] == r["ext"]) > 1})
    return {
        "pages_dir": str(pages_dir),
        "commit": commit, "used_llm": use_llm, "auto_bind_threshold": AUTO_BIND_THRESHOLD,
        "unknown_ext_count": len(unknowns),
        "unknown_file_count": sum(n for _, n in unknowns),
        "cohorts_auto_bound": len(results["auto_bound"]),
        "cohorts_escalated": len(results["escalated"]),
        "no_instance_count": len(results["no_instance"]),
        "mono_cohort_exts_promoted": promoted,
        "polymorphic_exts": polymorphic,
        "graduation_candidates": sorted({r["ext"] for r in results["auto_bound"]
                                         if r.get("graduation_candidate")}),
        # PATH-TO-COVER rollup (System L15): every escalated reservoir carries a rig_proposal;
        # summarize by kind so a human sees, at a glance, how much is raw-extractable (a template)
        # vs. needs-an-intermediary (an ingest-helper) — all `exploration` until built + validated.
        "rig_proposals": {
            "derived_template": sorted({r["ext"] for r in results["escalated"]
                                        if r.get("rig_proposal", {}).get("kind") == "derived-template"}),
            "ingest_helper": sorted({r["ext"] for r in results["escalated"]
                                     if r.get("rig_proposal", {}).get("kind") == "ingest-helper"}),
            "needs_ontology_expansion": sorted({r["ext"] for r in results["escalated"]
                                                if r.get("rig_proposal", {}).get("needs_ontology_expansion")}),
        },
        "tokens": {"in": ti, "out": to}, "cost_usd": round(cost, 4),
        "elapsed_s": round(elapsed, 1),
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Live rectification loop for unknown file types")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("preflight", help="rectify unknown census exts; --commit to learn deltas")
    pf.add_argument("--pages", required=True)
    pf.add_argument("--out", default="")
    pf.add_argument("--limit", type=int, default=0)
    pf.add_argument("--commit", action="store_true", help="write verified deltas to the overlay")
    pf.add_argument("--no-llm", action="store_true", help="deterministic sniff only (no LLM spend)")
    pf.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    pf.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    pf.add_argument("--model", default=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL))

    sub.add_parser("show-learned", help="print the current learned overlay")

    args = ap.parse_args()

    if args.cmd == "show-learned":
        print(json.dumps(_load_overlay(), indent=2, sort_keys=True))
        return 0

    if args.cmd == "preflight":
        report = rectify_census(Path(args.pages), commit=args.commit,
                                limit=(args.limit or None), profile=args.profile,
                                region=args.region, model=args.model,
                                use_llm=not args.no_llm)
        out = json.dumps(report, indent=2)
        if args.out:
            Path(args.out).write_text(out)
            _rp = report.get("rig_proposals", {})
            print(f"[rectify] wrote {args.out}: {report['cohorts_auto_bound']} cohorts auto-bound, "
                  f"{report['cohorts_escalated']} escalated, {report['mono_cohort_exts_promoted']} "
                  f"mono-cohort exts promoted, {len(report['polymorphic_exts'])} polymorphic, "
                  f"{report['no_instance_count']} no-instance, ${report['cost_usd']} — "
                  f"commit={report['commit']}. PATH-TO-COVER proposals: "
                  f"{len(_rp.get('derived_template', []))} derived-template, "
                  f"{len(_rp.get('ingest_helper', []))} ingest-helper (all exploration)", file=sys.stderr)
        else:
            print(out)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
