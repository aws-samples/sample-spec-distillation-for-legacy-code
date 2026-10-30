#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for the eval/benefits scripts.

- Budget guard: every Bedrock call goes through Budget.call(), which estimates the cost of the call
  before it is made and raises BudgetExceeded if spent + estimate > --max-usd.
- Bedrock calls reuse eval/bedrock_util.py (token/cost logging); the log is redirected to
  <out>/bedrock_calls.jsonl.
- Source loading, numbering and citation validation reuse eval/score.py.
- Answerer adapters: raw-code (full numbered source + task), spec (BM25-retrieved spec chunks +
  task, see retrieval.py), command (external tool returning JSON).
- Measurement status: every evaluator reports ``measurement.status`` = measured | partial |
  not_measured. A metric over zero graded items is ``None`` (JSON null), never 0.
- Key loading: a key that loads zero items raises :class:`KeyLoadError` (exit 2), so an empty or
  mis-shaped key can never come back as a quiet "ok".
- ``--dry-run``: build the real prompts, estimate the cost, write ``plan`` and exit; no Bedrock.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

BENEFITS_DIR = Path(__file__).resolve().parent
EVAL_DIR = BENEFITS_DIR.parent
REPO_DIR = EVAL_DIR.parent
sys.path.insert(0, str(EVAL_DIR))

sys.path.insert(0, str(BENEFITS_DIR))

import bedrock_util  # noqa: E402
import score as score_mod  # noqa: E402

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

CHARS_PER_TOKEN = 3.5

#: exit code for a key that loads no items (also used for unreadable keys)
EXIT_BAD_KEY = 2


class KeyLoadError(SystemExit):
    """The key file loaded zero items, or none in a recognised shape. Raised as SystemExit
    with exit code 2 so a CLI run fails loudly instead of reporting null metrics as ok."""

    def __init__(self, path, what: str, detail: str = ""):
        msg = (f"{path}: loaded 0 {what}" + (f" ({detail})" if detail else "")
               + "; refusing to report metrics over an empty key")
        print(f"KEY ERROR: {msg}", file=sys.stderr)
        super().__init__(EXIT_BAD_KEY)
        self.message = msg


def measurement(total: int, graded: int, *, what: str = "items", reason: str | None = None) -> dict:
    """Status of the judged (headline) metrics.

    measured     every one of ``total`` items was graded
    partial      some were; metrics cover only the graded ones
    not_measured none were; every judged metric is null
    """
    if total and graded >= total:
        status = "measured"
    elif graded:
        status = "partial"
    else:
        status = "not_measured"
    if status != "measured" and not reason:
        reason = f"{graded} of {total} {what} graded"
    return {"status": status, "graded": graded, "total": total, "what": what,
            "reason": reason if status != "measured" else None}


# ----------------------------------------------------------------------------- io
def load_yaml(path: Path):
    text = Path(path).read_text()
    if Path(path).suffix in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("PyYAML not available; provide JSON")
        return yaml.safe_load(text)
    return json.loads(text)


def write_outputs(out: Path, stem: str, report: dict, markdown: str):
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{stem}.json").write_text(json.dumps(report, indent=2, default=str))
    (out / f"{stem}.md").write_text(markdown)
    print(f"wrote {out}/{stem}.json and {out}/{stem}.md")


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# ----------------------------------------------------------------------------- budget
class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Pre-call spend guard. Estimate = prompt chars / 3.5 input tokens + expected output tokens."""

    def __init__(self, max_usd: float, out_dir: Path):
        self.max_usd = max_usd
        self.spent = 0.0
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.stopped = None
        out_dir.mkdir(parents=True, exist_ok=True)
        self.log = out_dir / "bedrock_calls.jsonl"
        # score.py calls bedrock_util.converse without a log path; route those calls here too
        bedrock_util.DEFAULT_LOG = self.log

    def estimate(self, chars: int, out_tokens: int) -> float:
        return bedrock_util.cost_usd(int(chars / CHARS_PER_TOKEN), out_tokens)

    def check(self, chars: int, out_tokens: int, what: str = "", reserve: float = 0.0):
        """Raise BudgetExceeded unless spent + this call + ``reserve`` fits the cap.

        ``reserve`` keeps money back for work that must follow this call (e.g. grading the
        answers already produced), so answering cannot consume the whole budget and leave
        nothing to grade with."""
        est = self.estimate(chars, out_tokens)
        if self.spent + est + reserve > self.max_usd:
            self.stopped = (f"budget guard: {what} estimated ${est:.4f}"
                            + (f" (+${reserve:.4f} reserved for grading)" if reserve else "")
                            + f", spent ${self.spent:.4f}, cap ${self.max_usd:g}")
            raise BudgetExceeded(self.stopped)
        return est

    def add(self, input_tokens: int, output_tokens: int, cost: float | None = None):
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.spent += cost if cost is not None else bedrock_util.cost_usd(input_tokens, output_tokens)

    def call(self, prompt: str, *, system: str | None = None, max_tokens: int = 4000,
             expected_out: int | None = None, tag: str = "", reserve: float = 0.0) -> dict:
        """Guarded Converse call. expected_out (default max_tokens/2) is used for the estimate."""
        self.check(len(prompt) + len(system or ""), expected_out or max_tokens // 2, tag, reserve)
        t0 = time.time()
        r = bedrock_util.converse(prompt, system=system, max_tokens=max_tokens, tag=tag, log_path=self.log)
        r["latency_s"] = round(time.time() - t0, 2)
        self.add(r["input_tokens"], r["output_tokens"], r["cost_usd"])
        return r

    def summary(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.spent, 4), "max_usd": self.max_usd, "stopped": self.stopped,
                "region": bedrock_util.region_in_use(), "model": bedrock_util.MODEL_ID}


class Plan:
    """Cost plan for ``--dry-run``: the same prompts a real run would send, priced, no calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def add(self, what: str, prompt_chars: int, out_tokens: int, note: str = ""):
        in_tok = int(prompt_chars / CHARS_PER_TOKEN)
        self.calls.append({"what": what, "input_tokens": in_tok, "output_tokens": int(out_tokens),
                           "cost_usd": round(bedrock_util.cost_usd(in_tok, int(out_tokens)), 6),
                           **({"note": note} if note else {})})

    def summary(self, **extra) -> dict:
        by: dict[str, dict] = {}
        for c in self.calls:
            b = by.setdefault(c["what"], {"calls": 0, "cost_usd": 0.0})
            b["calls"] += 1
            b["cost_usd"] = round(b["cost_usd"] + c["cost_usd"], 6)
        return {"calls": len(self.calls),
                "est_input_tokens": sum(c["input_tokens"] for c in self.calls),
                "est_output_tokens": sum(c["output_tokens"] for c in self.calls),
                "est_cost_usd": round(sum(c["cost_usd"] for c in self.calls), 4),
                "by_step": by, "model": bedrock_util.MODEL_ID,
                "region": bedrock_util.region_in_use(), **extra}


def write_plan(out: Path, stem: str, plan: dict, script: str):
    """Write the --dry-run result where the real result would go, marked as a plan."""
    r = {"dry_run": True, "script": script, "plan": plan,
         "measurement": {"status": "not_measured", "reason": "dry run: nothing was called"}}
    write_outputs(out, stem, r, "# Cost plan (dry run)\n\n```json\n" + json.dumps(plan, indent=2)
                  + "\n```\n")
    print(json.dumps(plan, indent=2))
    return r


def call_json(budget: Budget, prompt: str, *, system: str | None = None, max_tokens: int = 4000,
              expected_out: int | None = None, tag: str = "", retries: int = 1, reserve: float = 0.0):
    """Guarded call that parses JSON from the response; retries once on unparsable output.
    Returns (data_or_None, merged_usage_dict)."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0, "text": ""}
    for _ in range(retries + 1):
        r = budget.call(prompt, system=system, max_tokens=max_tokens, expected_out=expected_out, tag=tag,
                        reserve=reserve)
        for k in ("input_tokens", "output_tokens", "cost_usd", "latency_s"):
            usage[k] += r[k]
        usage["text"] = r["text"]
        try:
            return bedrock_util.extract_json(r["text"]), usage
        except ValueError:
            continue
    return None, usage


# ----------------------------------------------------------------------------- source + citations
class Source:
    def __init__(self, root: Path, exclude: list[str] | None = None):
        self.root = Path(root)
        self.files = score_mod.source_files(self.root, exclude)
        self.lines = {k: p.read_text(errors="replace").splitlines() for k, p in self.files.items()}

    def numbered(self, max_chars: int) -> tuple[str, bool]:
        full = score_mod.number_source(self.files, 10 ** 12)
        text = score_mod.number_source(self.files, max_chars)
        return text, len(full) > max_chars

    def resolve(self, name: str | None) -> str | None:
        return score_mod.resolve_file(name, self.files) if name else None

    def snippet(self, file: str, a: int, b: int, pad: int = 0, max_lines: int = 40) -> str | None:
        r = self.resolve(file)
        if not r:
            return None
        ls = self.lines[r]
        a2, b2 = max(1, a - pad), min(len(ls), b + pad, a - pad + max_lines)
        return "\n".join(f"{i:4d}  {ls[i - 1]}" for i in range(a2, b2 + 1))


CIT_STR_RE = re.compile(r"^\s*(?P<file>.+?)\s*(?::|#|,|\s)\s*\(?\s*(?:lines?\s+)?L?(?P<a>\d+)"
                        r"(?:\s*(?:-|–|—|to|\.\.)\s*L?(?P<b>\d+))?\)?\s*$", re.I)


def normalize_citation(c) -> dict | None:
    """Accept {file,start_line,end_line} | {file,lines:"10-20"} | {file,line} | "file:L10-L20"."""
    if isinstance(c, dict):
        f = c.get("file") or c.get("path")
        a = c.get("start_line", c.get("start", c.get("line")))
        b = c.get("end_line", c.get("end", a))
        if a is None and isinstance(c.get("lines"), str):
            m = re.match(r"L?(\d+)(?:\s*-\s*L?(\d+))?", c["lines"].strip())
            if m:
                a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not f:
            return None
        try:
            a = int(a) if a is not None else None
            b = int(b) if b is not None else a
        except (TypeError, ValueError):
            a = b = None
        return {"file": str(f), "start_line": a, "end_line": b}
    if isinstance(c, str):
        m = CIT_STR_RE.match(c)
        if m:
            a = int(m.group("a"))
            return {"file": m.group("file").strip("`'\" "), "start_line": a, "end_line": int(m.group("b") or a)}
        return {"file": c.strip(), "start_line": None, "end_line": None}
    return None


def validate_citation(c: dict, src: Source) -> dict:
    r = src.resolve(c.get("file"))
    out = dict(c, resolved=r)
    if r is None:
        out.update(valid=False, reason="file not found")
    elif c.get("start_line") is None:
        out.update(valid=True, reason="file only (no lines)")
    elif c["start_line"] < 1 or (c["end_line"] or 0) < c["start_line"]:
        out.update(valid=False, reason="bad range")
    elif c["end_line"] > len(src.lines[r]):
        out.update(valid=False, reason=f"out of range (file has {len(src.lines[r])} lines)")
    else:
        out.update(valid=True, reason="ok")
    return out


def loc_overlap(pred: dict, exp: dict, src: Source) -> bool:
    """Same file (resolved / basename) and overlapping line ranges. File-only predictions do not overlap."""
    pf, ef = src.resolve(pred.get("file")), src.resolve(exp.get("file"))
    if not pf or not ef or pf.split("/")[-1].lower() != ef.split("/")[-1].lower():
        return False
    if pred.get("start_line") is None:
        return False
    pa, pb = pred["start_line"], pred.get("end_line") or pred["start_line"]
    ea, eb = exp["start_line"], exp.get("end_line") or exp["start_line"]
    return pa <= eb and ea <= pb


def same_file(pred: dict, exp: dict, src: Source) -> bool:
    pf, ef = src.resolve(pred.get("file")), src.resolve(exp.get("file"))
    return bool(pf and ef and pf.split("/")[-1].lower() == ef.split("/")[-1].lower())


# ----------------------------------------------------------------------------- adapters
TOOL_SYSTEM = ("You are a senior engineer analysing a legacy business application. Answer only from the "
               "material you are given, cite exact file and line ranges, and answer only with JSON.")


class Adapter:
    """ask(task, payload) -> {"data": dict, "tokens_in", "tokens_out", "cost_usd", "latency_s", "error",
    "context"}.

    task: "qa" | "impact" | "fieldapp" | "insights". payload: dict with the question / change.
    instructions: task-specific prompt text describing the JSON to return (used by LLM adapters).
    ``context`` records what material the answerer was shown (e.g. which spec chunks).
    """
    name = "base"
    truncated = None

    def ask(self, task: str, payload: dict, instructions: str, expected_out: int = 1500,
            reserve: float = 0.0) -> dict:
        raise NotImplementedError

    def prompt_chars(self, task: str, payload: dict, instructions: str) -> int | None:
        """Size of the prompt ``ask`` would send, for --dry-run; None if unknowable."""
        return None

    def context_summary(self) -> dict:
        return {"mode": "external" if self.name == "command" else "full"}


def task_body(payload: dict) -> str:
    return "\n".join(f"{k.upper()}: {v}" for k, v in payload.items() if k != "id")


class LLMAdapter(Adapter):
    def __init__(self, budget: Budget, context_label: str, context: str, max_tokens: int = 6000):
        self.budget, self.label, self.context, self.max_tokens = budget, context_label, context, max_tokens

    def build_prompt(self, task, payload, instructions) -> tuple[str, dict]:
        return (f"{self.label}:\n{self.context}\n\n=== TASK ===\n{task_body(payload)}\n\n{instructions}",
                {"mode": "full", "chars": len(self.context)})

    def prompt_chars(self, task, payload, instructions):
        return len(self.build_prompt(task, payload, instructions)[0]) + len(TOOL_SYSTEM)

    def ask(self, task, payload, instructions, expected_out=1500, reserve=0.0):
        prompt, ctx = self.build_prompt(task, payload, instructions)
        data, u = call_json(self.budget, prompt, system=TOOL_SYSTEM, max_tokens=self.max_tokens,
                            expected_out=expected_out, tag=f"benefits:{self.name}:{task}", reserve=reserve)
        return {"data": data if isinstance(data, dict) else ({"items": data} if isinstance(data, list) else {}),
                "raw_text": u["text"] if data is None else None,
                "tokens_in": u["input_tokens"], "tokens_out": u["output_tokens"],
                "cost_usd": round(u["cost_usd"], 6), "latency_s": round(u["latency_s"], 2),
                "error": None if data is not None else "unparsable answer", "context": ctx}


class RawCodeAdapter(LLMAdapter):
    """'No tool' baseline: one call with the full line-numbered source (truncated) + the task."""
    name = "raw-code"

    def __init__(self, budget: Budget, src: Source, max_source_chars: int):
        text, truncated = src.numbered(max_source_chars)
        self.truncated = truncated
        super().__init__(budget, "SOURCE CODE (line-numbered)" + (" [TRUNCATED to fit]" if truncated else ""), text)


    def context_summary(self) -> dict:
        return {"mode": "raw-code", "truncated": self.truncated, "chars": len(self.context)}


class SpecAdapter(LLMAdapter):
    """Answers from a given spec.md only (no source).

    The spec is NOT truncated. It is split into heading / requirement chunks and, per
    question (or change, or request), the most relevant chunks by BM25 are packed into
    ``max_spec_chars`` (see retrieval.py). A spec that fits the budget is sent whole.
    Every answer records which chunks it was shown (``context.chunks``).
    """
    name = "spec"

    def __init__(self, budget: Budget, spec: Path, max_spec_chars: int):
        from retrieval import SpecRetriever
        text = Path(spec).read_text(errors="replace")
        self.retriever = SpecRetriever(text)
        self.max_spec_chars = max_spec_chars
        # True means: an answer is written from a SELECTION of the spec, not all of it
        self.truncated = len(text) > max_spec_chars
        super().__init__(budget, "SPECIFICATION DOCUMENT", text)

    def build_prompt(self, task, payload, instructions):
        query = task_body(payload)
        ctx_text, info = self.retriever.select(query, self.max_spec_chars)
        label = self.label if info["mode"] == "full" else (
            f"{self.label} (EXCERPTS: the {info['chunks_used']} sections most relevant to the task, "
            f"in document order; other sections exist but are not shown)")
        return f"{label}:\n{ctx_text}\n\n=== TASK ===\n{query}\n\n{instructions}", info

    def context_summary(self) -> dict:
        return {"mode": "retrieval" if self.truncated else "full", "method": "bm25",
                "spec_chars": len(self.context), "budget_chars": self.max_spec_chars,
                "chunks_total": len(self.retriever.chunks)}


class CommandAdapter(Adapter):
    """External tool. The command template may use {question}, {change}, {task}, {id}, {payload_file}.

    The template is split with shlex first and placeholders are substituted per argument, so values
    with spaces/quotes are passed safely. stdout must contain one JSON object, e.g.
    {answer, citations[], evidence_class} for qa, or the impact/fieldapp shapes. Optional usage keys
    (input_tokens, output_tokens, cost_usd) are recorded if present (also under "usage").
    """
    name = "command"

    def __init__(self, template: str, budget: Budget, timeout: int = 900, cwd: Path = REPO_DIR):
        self.argv = shlex.split(template)
        self.budget, self.timeout, self.cwd = budget, timeout, cwd

    def ask(self, task, payload, instructions, expected_out=1500, reserve=0.0):
        import tempfile
        if self.budget.max_usd - self.budget.spent - reserve <= 0:
            self.budget.stopped = "budget guard: cap reached before external command"
            raise BudgetExceeded(self.budget.stopped)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump(dict(payload, task=task, instructions=instructions), tf)
            pfile = tf.name
        subs = {"question": "", "change": "", "request": "", "audience": "", "id": ""}
        subs.update({k: str(v) for k, v in payload.items()})
        subs.update(task=task, payload_file=pfile)
        argv = [re.sub(r"\{(\w+)\}", lambda m: subs.get(m.group(1), m.group(0)), a) for a in self.argv]
        t0 = time.time()
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, cwd=self.cwd)
            out, err, rc = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired:
            out, err, rc = "", "timeout", -1
        dt = time.time() - t0
        data, error = {}, None
        try:
            data = bedrock_util.extract_json(out)
            if isinstance(data, list):
                data = {"items": data}
        except ValueError:
            error = f"rc={rc}; no JSON on stdout; stderr tail: {err[-300:]}"
        u = data.get("usage", data) if isinstance(data, dict) else {}
        ti, to = int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
        cost = u.get("cost_usd")
        cost = float(cost) if cost is not None else bedrock_util.cost_usd(ti, to)
        if ti or to or cost:
            self.budget.add(ti, to, cost)
        return {"data": data, "raw_text": out[-2000:] if error else None, "tokens_in": ti, "tokens_out": to,
                "cost_usd": round(cost, 6), "latency_s": round(dt, 2), "error": error,
                "context": {"mode": "external"}}


#: --adapter spec: per-question character budget for retrieved spec chunks (~17K tokens)
DEFAULT_SPEC_BUDGET_CHARS = 60000


def add_adapter_args(ap, need_spec_ok=True):
    ap.add_argument("--adapter", choices=["raw-code", "spec", "command"], default="raw-code")
    ap.add_argument("--source-root", type=Path, required=True,
                    help="legacy source root (raw-code context, citation checks, judge evidence)")
    ap.add_argument("--exclude", action="append", default=[], help="glob of source files to ignore; repeatable")
    ap.add_argument("--spec", type=Path, help="spec.md for --adapter spec")
    ap.add_argument("--command", help="command template for --adapter command, e.g. "
                    "\"python3 eval/lean/ask.py --run-dir X --question {question}\"")
    ap.add_argument("--command-timeout", type=int, default=900)
    ap.add_argument("--tool-name", help="label for this tool in reports (default: adapter name)")
    ap.add_argument("--max-source-chars", type=int, default=150000,
                    help="cap on numbered source sent to raw-code adapter and judge (default 150000)")
    ap.add_argument("--max-spec-chars", type=int, default=DEFAULT_SPEC_BUDGET_CHARS,
                    help="--adapter spec: character budget for the spec excerpts sent per question; the "
                         "most relevant heading/requirement chunks (BM25) are packed into it. A spec "
                         f"smaller than this is sent whole (default {DEFAULT_SPEC_BUDGET_CHARS})")
    ap.add_argument("--max-usd", type=float, default=1.0, help="stop the run if estimated spend exceeds this")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the real prompts, print and write the cost plan (plan.est_cost_usd), "
                         "and exit without calling Bedrock")
    ap.add_argument("--out", type=Path, required=True, help="output directory")


def make_adapter(a, budget: Budget, src: Source) -> Adapter:
    if a.adapter == "raw-code":
        return RawCodeAdapter(budget, src, a.max_source_chars)
    if a.adapter == "spec":
        if not a.spec:
            raise SystemExit("--adapter spec needs --spec")
        return SpecAdapter(budget, a.spec, a.max_spec_chars)
    if not a.command:
        raise SystemExit("--adapter command needs --command")
    return CommandAdapter(a.command, budget, a.command_timeout)


def citations_from(data: dict, key: str = "citations") -> list[dict]:
    out = []
    for c in data.get(key) or []:
        n = normalize_citation(c)
        if n:
            out.append(n)
    return out


def md_table(rows: list[tuple], header: tuple) -> list[str]:
    L = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        L.append("| " + " | ".join(fmt(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return L
