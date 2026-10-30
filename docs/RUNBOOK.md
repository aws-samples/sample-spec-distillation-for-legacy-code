<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->
# Specification Distillation — Operator Runbook

The single operational guide for running a distillation on a fresh machine. This reflects the
current pipeline (page-centric map + async reduce, run-identity gate, completeness barrier,
actionable preflight). For the MCP tool API, see `runtimes/mcp-server/README.md`.

## 1. Install (once per machine)

Target: Linux or macOS with Python 3.10+, AWS credentials that can call Amazon Bedrock
(`bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` on the models you use), and
optionally the .NET 8 SDK for the parser-facts stage.

```bash
./setup.sh                 # venv + pip deps + embedding model download + optional .NET parser build
./check-dependencies.sh    # verifies every dep + an AWS/Bedrock round-trip. RUN THIS before a long run.
```

`check-dependencies.sh` exits non-zero if anything a run needs is missing: dependencies,
credentials, a live Bedrock call, the embedding model, SQLite or filesystem writes. A green
result is the go signal.

## 2. Run

```bash
AWS_PROFILE=<profile> AWS_DEFAULT_REGION=us-east-1 PYTHONUNBUFFERED=1 \
  DISTILL_TARGET=path/to/source \
  DISTILL_OUTPUT=output/<run-name> \
  DISTILL_STILLS="data rule workflow state permission" \
  bash run_distillation.sh
```

The pipeline runs: enumerate → page → **preflight (Phase 1.5)** → facts (1.2) → identifier
evidence (1.3) → **identity gate (Phase 1.6)** → **cost estimate / budget check (Phase 1.7)**
→ page-centric map + async reduce (Phase 2) → **completeness barrier (Phase 2→2.5)** →
knowledge → synthesis → blend → bottle → viewer.

### Required knobs

| Var | Meaning |
|-----|---------|
| `DISTILL_TARGET` | Path to the source tree to distill. |
| `DISTILL_OUTPUT` | Output dir for this run (use a fresh name; never reuse an orphan dir). |
| `DISTILL_STILLS` | Space-separated still names to run (e.g. `data rule workflow state permission`). |
| `AWS_PROFILE` / `AWS_DEFAULT_REGION` | AWS creds + region for Amazon Bedrock. |

### Common optional knobs

| Var | Default | Meaning |
|-----|---------|---------|
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Model id. |
| `ORCHESTRATION_STRATEGY` | `page-centric` | `page-centric` (prompt caching). `still-centric` needs an external agent runtime that this sample does not include. |
| `DISTILL_TARGET_NAME` | basename of target | Display name in the deliverable. |
| `DISTILL_PAGE_TOKENS` (older name `DISTILL_TOKEN_BUDGET`) | 22000 | Estimated tokens per page (chars/2), about 600-900 numbered lines. |
| `DISTILL_FACTS` | 1 | Phase 1.2 structural facts (.NET 8 Roslyn/ScriptDom tool, built by `setup.sh`) appended to each page in the page-centric map prompt. 0 disables. Without dotnet the run logs a WARNING and continues without facts. |
| `DISTILL_FACTS_MAX_FRACTION` | 0.15 | Bound on each page's facts block, as a fraction of the page budget. |
| `DISTILL_CONCURRENCY` / `DISTILL_REDUCE_CONCURRENCY` | (see script) | Map / reduce parallelism. |
| `DISTILL_CODE_GRAPH` | `1` (on) | Code-dependency graph + arbitration (stages 2.8/2.9; SQL/C#/VB/COBOL rigs). LLM calls (`DISTILL_CODE_GRAPH_MODEL`, default Haiku 4.5); a failure fails the run. `0` disables. |
| `DISTILL_ALLOW_NO_CONTENT` | off | Continue even when every page is marker-only (default: halt before any spend, exit 3). |
| `SKIP_SYNTHESIS` / `SKIP_BOTTLING` | off | Stop before those phases (mechanics-exercise runs). |
| `DISTILL_SOURCE_REPO_URL` | (unset) | Enables evidence deep-links in the viewer. |
| `DISTILL_IDENTIFIERS` | 1 | Phase 1.3 identifier evidence: abbreviated column and variable names with their comments, labels, definitions and set/test sites, appended to each map page. Bounds: `DISTILL_IDENTIFIERS_MAX_CHARS` (4000) and `DISTILL_IDENTIFIERS_MAX_ENTRIES` (24). 0 disables. Any change re-maps every still (`identifiers_hash` in the run identity). |
| `DISTILL_MAX_USD` | (unset) | Budget cap in USD, cumulative over the output directory's `telemetry/`. See §6a. |
| `DISTILL_WARN_USD` | (unset) | Warn-only threshold. |
| `DISTILL_ALLOW_OVER_ESTIMATE` | off | Start even when the Phase 1.7 estimate is over the cap. The run still stops at the cap. |
| `DISTILL_COST_HISTORY` | (unset) | Earlier run directories whose telemetry sets the per-call averages. Default: the table in `budget.py`. |
| `DISTILL_MAX_CALL_TOKENS` | 64000 | Output-token bound applied to every Bedrock call. |
| `DISTILL_MAP_CALL_RESERVE_USD` | 0.32 | What each in-flight map call reserves against the cap (the largest map call on the reference runs; history max when `DISTILL_COST_HISTORY` is set). |

## 3. Checkpoints — when the run pauses for you (report | gate | stop)

Three checkpoints share one control vocabulary (the run-control checkpoint contract). Each reads a `DISTILL_*` knob:

| Checkpoint | Phase | Knob | Default | What it guards |
|-----------|-------|------|---------|----------------|
| Preflight go/no-go | 1.5 | `DISTILL_PREFLIGHT` | `report` | coverage / signal-at-risk before the map spend |
| Identity gate | 1.6 | `DISTILL_IDENTITY` | `report` | resume adjudication before any spend (and before clearing stale outputs) |
| Completeness barrier | 2→2.5 | `DISTILL_BARRIER` | `gate` | every requested still produced a non-empty, non-refusal `raw_concat` before the deliverable is built (refused/empty output is moved to `.backup/` so a re-run recomputes it) |

Modes (same for every checkpoint):
- **report** — run the check, print the verdict, continue regardless.
- **gate** — halt only on an adverse verdict (exit 3 preflight, exit 4 barrier).
- **stop** — always halt after the report, for inspection ("run to here, then let me look").

The barrier defaults to **gate** (not report): a missing still would silently vanish from the
deliverable, so the safe default is to halt and make you opt into a degraded partial run
(`DISTILL_BARRIER=report`).

### Preflight is actionable

The preflight report (`<output>/preflight_report.json`, also printed at Phase 1.5) tells you,
per finding: **where** (example file paths + count, extensions quantified e.g. `py(133)`, a
missing extension shown as `<no_extension>`), **what/how** (the concrete rig/step, or an
explicit "no action"), **why it matters** (a code-graph structural gap is distinguished from a
distillation-coverage loss — a HIGH verdict is usually advisory, not lost signal), and
**blocking?** (advisory in `report`, a halt only in a gating mode).

## 4. Watch progress

The dashboard is read-only and launched separately (the orchestrator prints the exact command;
it does not spawn it):

```bash
runtimes/mcp-server/.venv/bin/python runtimes/mcp-server/progress_server.py <output-dir> --port 8002
# then open http://localhost:8002
```

It shows per-still map/reduce/knowledge/synthesis/format state, tokens-per-minute + cost, the
preflight census, and a **failed** state (red) for any crashed/incomplete still. A still that
finished mapping but has not been reduced shows `queued`, not a false "running".

## 5. View the deliverable

After the run, launch the viewer server (command printed at the end of the run):

```bash
runtimes/mcp-server/.venv/bin/python runtimes/mcp-server/serve_viewer.py \
  <output-dir>/viewer <output-dir>/stills/shared_vectors.db \
  --source-root <target> --port 8080
# then open http://localhost:8080
```

### Graph artifacts (predictable paths)

Every run writes `<output-dir>/knowledge_graph/`:

| File | Content |
|------|---------|
| `index.json` | which artifacts exist, their counts and source paths |
| `knowledge_graph.json` | concept graph (NetworkX node-link), copy of `stills/knowledge_graph.json` |
| `code_graph.json` | code-dependency graph: `source_files`, `code_objects`, `code_edges`, `code_entities` |
| `concept_code_bridge.json` | concept ↔ code links (concept symbols joined to code objects/entities) |
| `code_graph_stats.json` | code-graph pass stats (files, errors, tokens, cost) |

The MCP tools work against a completed run: `build_knowledge_graph(run_dir=<output-dir>)` re-exports
these (no LLM) and returns the index; `query_spec(question, run_dir=<output-dir>)` uses
`<output-dir>/stills/shared_vectors.db`.

Every filtered file is listed with its reason in `<output-dir>/.enumeration/excluded.tsv`
(`.rpt` is excluded unless the JRC helper is configured via `RPT_JRC_JAVA`/`RPT_JRC_CLASSPATH`).

## 6. Resume

Re-running the same command against the same `DISTILL_OUTPUT` resumes, bound to content rather
than file existence. Phase 0 always re-enumerates and writes `.enumeration/source_inventory.json`
(relative path, size and sha256 per file, plus filter-rule, ingest-helper, paging-script and config
versions → `source_identity`). Each reusable stage stamps the identity it was built from plus a
build token in `.stage_identity/<stage>.id`; ids chain from the upstream token (pages ← source;
knowledge ← run identity incl. prompts + each still's reduced output; … bottling ← blend). A
mismatch, or any rebuild of an upstream stage, recomputes that stage and everything after it, and
the stale outputs are moved to `.backup/<timestamp>/`. An unchanged re-run recomputes
nothing. The ingest cache is keyed on file content + helper version, not path.

## 6a. Budget stop and resume

With `DISTILL_MAX_USD` set:

- **Phase 1.7, before any model call.** The run prints a per-stage estimate and writes it to
  `budget_estimate.json`. If the estimate is over the cap, the run refuses to start: exit 6, and
  `run_status.json` is `over_estimate`.
- **During the run.** Actual telemetry spend plus the next stage's estimate is checked before
  every LLM stage (2.6, 2.75, 2.8, 2.9, 2.95, 2.98, 4 and 5). On the page-centric path it is also
  checked before every map and reduce call; reservations cover calls already in flight. If the
  next step would pass the cap, the run stops: exit 6, no banner, and `run_status.json` is
  `budget_exhausted` with `spent_usd`, `stage_reached`, `remaining_estimate_usd`,
  `suggested_max_usd` and `resume_command`.
- **Resume.** Run `resume_command` verbatim. Completed stages are reused through stage identity
  at $0. Map extracts are only ever written complete, so only missing (page, still) pairs are
  re-mapped.

Cost reporting: `python3 runtimes/mcp-server/telemetry.py cost <output-dir>`. It also accepts
`<output-dir>/telemetry` and exits 1 when no records are found.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (or a deliberate `stop`-mode halt) |
| 1 | hard error (missing pages, invoker failure, unknown strategy) |
| 3 | preflight gate halt (CRITICAL verdict, `DISTILL_PREFLIGHT=gate`), or no page carries source content (always, unless `DISTILL_ALLOW_NO_CONTENT=1`) |
| 4 | completeness barrier halt (a still missing, empty or refused, `DISTILL_BARRIER=gate`) |
| 5 | a required stage failed (code graph, arbitration, graph export, blend, bottling, viewer) — no "Distillation complete" banner; `run_status.json` = `failed` |
| 6 | budget: Phase 1.7 estimate over `DISTILL_MAX_USD` (`over_estimate`), or actual spend reached the cap (`budget_exhausted`). Resume with the `resume_command` in `run_status.json`. |
