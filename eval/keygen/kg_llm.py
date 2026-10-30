# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""OPTIONAL AI DRAFT (``--llm-draft``): one bounded pass that proposes rules the harvest missed.

Independence rules (the reason this module looks the way it does):

* **Never feed pipeline outputs into the key builder.** The only inputs are the source tree and
  the items harvest/tests/docs already produced from that same tree. No spec, no page summary,
  no knowledge graph from ``run_distillation.sh`` is read here, so a key built with
  ``--llm-draft`` still does not share an error with the pipeline it will score.
* **A different prompt.** The pipeline's extraction prompts (``still_specs/``,
  ``runtimes/pipeline``) ask a model to *describe* code. This prompt asks a reviewer to *audit a
  key for gaps*: it shows the rules already captured and asks only for business rules that are
  missing, each with a verbatim quote from numbered source lines.
* **Every proposal must cite verified code or it is dropped.** The cited file must exist, the
  line range must lie inside it, and the quoted text must occur (whitespace-normalised) inside
  those lines. Proposals that restate an existing item are dropped too.
* **Bounded and capped.** One pass over the files in a fixed order, a hard ``--max-usd`` cap
  checked *before* every call against a worst-case estimate (prompt chars / 2.5 tokens at the
  input price plus the full output budget at the output price), and a call ceiling. Off by
  default.
* **The model is the judge model already used by ``eval/``** (``bedrock_util.MODEL_ID``, which
  ``eval/judge`` also uses), unless ``--llm-model`` overrides it. Nothing new is hard-coded.

Items come out as ``source: llm-draft``, ``status: unreviewed``, ``evidence_class: Inferred``.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

from kg_merge import content, overlap_coeff
from kg_source import Candidate, SourceFile, clip, loc

EVAL_DIR = Path(__file__).resolve().parents[1]

SYSTEM = (
    "You audit an answer key for a legacy-code specification exercise. The key lists business "
    "rules that a correct specification of the code must state. Your job is to find business "
    "rules that the key is MISSING. You never restate a rule the key already has. You only "
    "report a rule when you can quote the exact source line(s) that enforce it."
)

PROMPT = """Rules already in the key (do not repeat these, even reworded):
{known}

Source files, every line prefixed with its 1-based line number:
{source}

List up to {max_items} business rules that are enforced by the code above but are NOT in the key.
A business rule is a constraint, calculation, state transition, permission or workflow step that
a domain expert would care about. Ignore logging, dependency injection, UI layout and framework
plumbing.

Answer with JSON only:
{{"rules": [{{"statement": "<one plain-English sentence>",
             "type": "rule|data|state|workflow|exception|interface|permission",
             "file": "<path exactly as shown>", "start_line": <int>, "end_line": <int>,
             "quote": "<text copied verbatim from those lines>"}}]}}
"""

#: worst-case characters per token for the pre-call estimate. Code tokenises denser than prose,
#: so this is deliberately lower than the 3.6 the judge planner uses for its *expected* cost.
CHARS_PER_TOKEN = 2.5


def _bedrock():
    sys.path.insert(0, str(EVAL_DIR))
    import bedrock_util                                      # noqa: E402  (read-only use)
    return bedrock_util


def default_model() -> str:
    """The model ``eval/judge`` uses (``bedrock_util.MODEL_ID``); read, never hard-coded here."""
    return getattr(_bedrock(), "MODEL_ID", "")


def prices() -> tuple[float, float]:
    bu = _bedrock()
    return float(getattr(bu, "PRICE_IN", 3.0)), float(getattr(bu, "PRICE_OUT", 15.0))


def worst_case_usd(prompt_chars: int, max_tokens: int, price_in: float, price_out: float) -> float:
    return prompt_chars / CHARS_PER_TOKEN / 1e6 * price_in + max_tokens / 1e6 * price_out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def verify(prop: dict, files: dict[str, SourceFile]) -> tuple[bool, str]:
    """A proposal survives only if it cites real lines that contain its quote."""
    f = files.get(str(prop.get("file", "")).lstrip("./"))
    if f is None:
        return False, "file_not_found"
    try:
        a, b = int(prop.get("start_line")), int(prop.get("end_line", prop.get("start_line")))
    except (TypeError, ValueError):
        return False, "bad_line_numbers"
    if a < 1 or b < a or b > len(f.lines):
        return False, "out_of_range"
    quote = _norm(str(prop.get("quote", "")))
    if len(quote) < 8:
        return False, "no_quote"
    span = _norm("\n".join(f.lines[a - 1:b]))
    if quote not in span:
        return False, "quote_not_in_cited_lines"
    if not str(prop.get("statement", "")).strip():
        return False, "no_statement"
    return True, "ok"


def _chunks(files: dict[str, SourceFile], max_chars: int, include: Callable[[SourceFile], bool]):
    buf, size = [], 0
    for rel, f in sorted(files.items()):
        if not include(f):
            continue
        numbered = "\n".join(f"{i:>4}: {l}" for i, l in enumerate(f.lines, 1))
        block = f"=== {rel} ===\n{numbered}\n"
        if len(block) > max_chars:
            block = block[:max_chars]
        if size + len(block) > max_chars and buf:
            yield buf
            buf, size = [], 0
        buf.append(block)
        size += len(block)
    if buf:
        yield buf


def draft(files: dict[str, SourceFile], known_items: list[dict], max_usd: float,
          model: str | None = None, call: Callable | None = None, max_calls: int = 12,
          chunk_chars: int = 60000, max_tokens: int = 4000, max_items: int = 15,
          include: Callable[[SourceFile], bool] | None = None, log_path: Path | None = None) -> tuple[list[Candidate], dict]:
    """Run the bounded draft pass. ``call(prompt, system, max_tokens, model)`` -> {text,
    cost_usd} is injectable for tests; the default goes through ``bedrock_util.converse``."""
    include = include or (lambda f: not f.is_test and f.lang in ("cs", "vb", "sql"))
    price_in, price_out = prices() if call is None else (3.0, 15.0)
    model = model or (default_model() if call is None else "mock-model")
    if call is None:
        bu = _bedrock()

        def call(prompt, system, max_tokens, model):          # noqa: E306
            prev = bu.MODEL_ID
            bu.MODEL_ID = model
            try:
                return bu.converse(prompt, system=system, max_tokens=max_tokens,
                                   tag="keygen-llm-draft", log_path=log_path)
            finally:
                bu.MODEL_ID = prev
    known = "\n".join(f"- {it['id']}: {clip(it['statement'], 160)}" for it in known_items[:400]) or "- (none)"
    report = {"model": model, "max_usd": max_usd, "calls": 0, "spent_usd": 0.0, "proposed": 0,
              "kept": 0, "dropped": {}, "stopped": None, "chunks": 0}
    out: list[Candidate] = []
    known_toks = [content(it["statement"]) for it in known_items]
    for chunk in _chunks(files, chunk_chars, include):
        report["chunks"] += 1
        prompt = PROMPT.format(known=known, source="".join(chunk), max_items=max_items)
        est = worst_case_usd(len(prompt) + len(SYSTEM), max_tokens, price_in, price_out)
        if report["spent_usd"] + est > max_usd:
            report["stopped"] = f"budget: next call worst case ${est:.4f} would exceed the ${max_usd:.2f} cap"
            break
        if report["calls"] >= max_calls:
            report["stopped"] = f"call ceiling {max_calls} reached"
            break
        resp = call(prompt, SYSTEM, max_tokens, model)
        report["calls"] += 1
        report["spent_usd"] = round(report["spent_usd"] + float(resp.get("cost_usd", 0.0)), 6)
        try:
            data = json.loads(re.search(r"\{.*\}", resp.get("text", ""), re.S).group(0))
            props = data.get("rules") or []
        except Exception:
            report["dropped"]["unparseable_response"] = report["dropped"].get("unparseable_response", 0) + 1
            continue
        for p in props:
            report["proposed"] += 1
            ok, why = verify(p, files)
            if ok:
                toks = content(str(p["statement"]))
                if any(overlap_coeff(toks, k) >= 0.7 for k in known_toks):
                    ok, why = False, "duplicates_existing_item"
            if not ok:
                report["dropped"][why] = report["dropped"].get(why, 0) + 1
                continue
            rel = str(p["file"]).lstrip("./")
            typ = p.get("type") if p.get("type") in ("rule", "data", "state", "workflow", "exception",
                                                      "interface", "permission") else "rule"
            out.append(Candidate(typ, clip(str(p["statement"]), 300),
                                 [loc(rel, int(p["start_line"]), int(p.get("end_line", p["start_line"])))],
                                 "llm-draft", "llm_draft", evidence_class="Inferred",
                                 extra={"snippet": clip(str(p["quote"])), "model": model}))
            known_toks.append(content(str(p["statement"])))
            report["kept"] += 1
    return out, report
