<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->
# Run guide: input, steps, output

Operator reference: [`RUNBOOK.md`](RUNBOOK.md). Launcher API: [`LAUNCHER_API.md`](LAUNCHER_API.md).

## 1. Input

The pipeline takes **a directory of source files**. Nothing else is required.

Bundled sample — `samples/ap-invoices/`, a synthetic VB.NET + SQL Server accounts-payable
module for a construction contractor. 16 files, 2,320 lines, all invented:

| Part | Files |
|---|---|
| `ClientSide/AP/AP/` | 7 VB.NET: invoice entry, approval and payment-hold forms, business-logic class, data-access class, globals, one designer file |
| `Scripts/Tables/` | `AP_Tables.sql` — DDL plus seed rows |
| `Scripts/StoredProcedures/` | 5 procs: post, approve, hold release, retainage, duplicate check |
| `Scripts/Triggers/` | `trg_APInvHdr_u.sql` — status-transition guard |
| `Scripts/Reports/` | `412.sql` — open payables by job |
| `Reports/AP Accounts Payable/` | Crystal report text export |

Read natively: `.vb`, `.cs`, `.sql`, `.cob/.cbl/.cpy`, `.rdl/.rdlc`, `.dtsx`, and common text
formats. `.rpt` (Crystal binaries) are listed in `excluded.tsv` with a reason unless the Java
helper is available. Exclude the sample's own `README.md` from the input directory.

### Or: a git repository

Set `DISTILL_GIT_URL` instead of `DISTILL_TARGET`. The two are mutually exclusive, and the run
stops with an error if both are set.

| Variable | |
|---|---|
| `DISTILL_GIT_URL` | `https://host/org/repo.git`, `ssh://git@host/org/repo.git`, `git@host:org/repo.git`, `file:///path/repo.git` or a local repository path (absolute) |
| `DISTILL_GIT_REF` | Branch, tag or commit SHA. Default: the remote HEAD. |
| `DISTILL_GIT_SUBDIR` | Analyse only this subfolder of the repository. |
| `DISTILL_GIT_MAX_MB` | Size limit, default 500. The clone is aborted once it passes the limit. |

```bash
AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=us-east-1 PYTHONUNBUFFERED=1 \
  DISTILL_GIT_URL=https://github.com/<org>/<repo>.git \
  DISTILL_GIT_REF=main \
  DISTILL_GIT_SUBDIR=samples/ap-invoices \
  DISTILL_OUTPUT=output/ap-invoices-git DISTILL_MAX_USD=20 \
  bash run_distillation.sh
```

What it does:

- **Clone.** `runtimes/mcp-server/source_fetch.py` clones shallowly (`--depth 1`) into
  `<output>/.source/`. A full 40-character SHA is fetched directly. An abbreviated SHA needs a
  full fetch. A re-run that resolves to the same commit reuses the existing clean checkout without
  fetching.
- **Provenance.** The URL with any userinfo stripped, the requested ref, the resolved commit SHA
  and the subfolder are written to `source_provenance.json`, to `run_manifest.json` under
  `"source"`, and to `.enumeration/source_inventory.json` under `"source_provenance"`.
- **Reuse.** The commit and subfolder are part of the source identity. A re-run at the same SHA
  reuses every stage. A new commit re-enumerates and re-pages. If that commit changed the
  analysed files, every stage after paging is recomputed. If the analysed files are byte-identical
  (for example, the commit only touched files outside `DISTILL_GIT_SUBDIR`), the LLM stages are
  still reused at $0, because they are keyed on page content.
- **Budget resume.** For a budget stop, the resume command pins `DISTILL_GIT_REF` to the analysed
  commit, so a branch that moved in the meantime cannot change the input mid-run.
- `.git/` is never enumerated. It appears once in `excluded.tsv`.
- `DISTILL_TARGET_NAME` defaults to the subfolder name, or else the repository name.

Errors stop the run before any spend: exit 1, and `run_status.json` is `failed` with a
`source fetch: ...` reason. The cases are: unreachable URL or host, authentication failure,
unknown branch, tag or commit, missing subfolder, an empty repository, a repository over the size
limit, and a URL with embedded credentials.

**Authentication** is your existing git setup. Prompts are disabled, so a missing credential
fails fast instead of hanging:

- **SSH:** load the key into ssh-agent (`ssh-add`). If you set `core.sshCommand` or
  `GIT_SSH_COMMAND`, it is respected.
- **HTTPS:** configure a git credential helper (`git config --global credential.helper ...`).
- **Never put a token in the URL.** `https://user:token@host/...` and `https://token@host/...` are
  refused. The URL is only ever printed and recorded with the userinfo stripped.

## 2. Steps

### Prerequisites
- Linux or macOS with Python 3.10+, the AWS CLI v2, and optionally the .NET 8 SDK (parser facts).
- AWS credentials for a **non-production** account with Amazon Bedrock model access in `us-east-1`.
- `./setup.sh` once per machine (Python venv, embedding model, optional .NET parser build), then
  `./check-dependencies.sh` to verify everything, including one small live Bedrock call.

### Run

```bash
cp -r samples/ap-invoices /tmp/ap-src && rm /tmp/ap-src/README.md

AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=us-east-1 PYTHONUNBUFFERED=1 \
  DISTILL_TARGET=/tmp/ap-src \
  DISTILL_OUTPUT=output/ap-invoices \
  DISTILL_TARGET_NAME=ap-invoices \
  DISTILL_MAX_USD=20 \
  bash run_distillation.sh
```

Defaults do everything: 11 extraction passes, parser facts, code graph, knowledge graph,
issues report, spec and viewer. On this sample: **about 67 minutes and about $17** of Bedrock
(Claude Sonnet 4.6). Use a fresh output directory per run; re-running into the same directory
reuses any stage whose inputs are unchanged and costs nothing for those stages.

Useful switches: `DISTILL_PAGE_TOKENS` (page size), `DISTILL_FACTS=0`, `DISTILL_IDENTIFIERS=0`,
`DISTILL_CODE_GRAPH=0`, `DISTILL_ISSUES=0`, `BEDROCK_MODEL_ID`.

### Budget cap

Cost grows with page count: about $5.70 per 22K-token page with all 11 stills (3 pages on this
sample, about $17; the 2,551-line JC module made 4 pages and cost $22.92). Set a cap:

| Variable | Effect |
|---|---|
| `DISTILL_MAX_USD` | Cap in USD for everything recorded in `<output>/telemetry/`, so it is cumulative across resumes. |
| `DISTILL_WARN_USD` | Prints a warning once the estimate or the spend passes this amount. Never stops the run. |
| `DISTILL_ALLOW_OVER_ESTIMATE=1` | Start even when the estimate is over the cap. The run still stops at the cap. |
| `DISTILL_COST_HISTORY` | Space-separated earlier run directories. Their telemetry sets the per-call averages. Otherwise the default table in `runtimes/mcp-server/budget.py` is used. |
| `DISTILL_MAX_CALL_TOKENS` | Output-token bound on every Bedrock call. Default 64000, about $0.96 of Sonnet output. |

What the cap does:

1. **Before any model call (Phase 1.7).** The run prints a per-stage estimate: pages × stills ×
   per-call averages for map, reduce, synthesis and format, plus code graph, issues and bottling.
   It writes the estimate to `budget_estimate.json`. Work an earlier attempt already finished in
   the same output directory counts as $0. If the estimate is over the cap, the run refuses to
   start: exit 6, and `run_status.json` is `over_estimate` with a suggested cap and command.
2. **During the run.** Actual spend is read from telemetry, with cache tokens and per-model
   prices included. It is checked before every LLM stage and, on the page-centric path, before
   every map and reduce call; reservations cover calls already in flight. If the next step would
   pass the cap, the run stops cleanly: exit 6, no completion banner, and `run_status.json` is
   `budget_exhausted`. That file holds `spent_usd`, `stage_reached`, `remaining_estimate_usd`,
   `suggested_max_usd` and `resume_command`. The same command is printed.
3. **Resume.** Run `resume_command` as printed. It is the original environment with a raised
   `DISTILL_MAX_USD`. Finished stages, map extracts and reduced stills are reused through stage
   identity, so they cost $0.

Each in-flight map call reserves `DISTILL_MAP_CALL_RESERVE_USD`. The default is $0.32, the
largest single map call seen on the reference runs.

Example, measured on this sample with `DISTILL_MAX_USD=1.50`:
- **Without the override**, the run refused to start. The estimate was $17.35 and it spent $0.
- **With `DISTILL_ALLOW_OVER_ESTIMATE=1`**, the map stopped after 5 calls at $1.24. Status was
  `budget_exhausted`. The resume command suggested `DISTILL_MAX_USD=20.50`, and the re-estimate
  for that resume was $17.74.

### Or: launch from the local web form / API

`runtimes/mcp-server/launcher_server.py` launches and monitors runs. It is a JSON API with one
plain HTML form on top. The API reference, for UI and MCP integration, is
[`LAUNCHER_API.md`](LAUNCHER_API.md).

```bash
AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=us-east-1 \
  runtimes/mcp-server/.venv/bin/python runtimes/mcp-server/launcher_server.py --port 8090
# open http://127.0.0.1:8090/
```

- **Form.** Give a git URL (with an optional ref and subfolder) or a local path, a run name and a
  budget. **Estimate cost** clones, enumerates and pages the source, then shows the estimate. It
  makes no model call. **Start run** queues the run. The runs table polls status, phase and
  spend, and links to `issues.md`, the bottled spec, the knowledge graph and the viewer.
- **Isolation.** Every run gets its own output directory, `output/runs/<run_id>/`, with
  `run.log` and `launcher.json`. One run executes at a time (`--max-concurrent 1`); the rest wait
  in a queue.
- **Over the cap.** An estimate over the cap is refused with 409 / `over_estimate` and spends
  nothing, unless "start even if over" is ticked.
- **Budget stops.** A `budget_exhausted` run shows its exact resume command. Run that command in
  a shell.
- **Safety.** The server binds to `127.0.0.1` only. A non-loopback `--host` is refused unless you
  pass `--allow-non-loopback`, which prints a warning. Never expose the server publicly.
- **Credentials.** The launcher never accepts AWS credentials. Runs use the launcher process's
  environment and AWS profile. Any credential-like request field is rejected.

### What happens, in order

| Phase | Does | Notes |
|---|---|---|
| 0–1.1 | Enumerate, ingest, page, **conservation check** | Fails the run unless every source line is paged exactly once |
| 1.2 | Parser facts (Roslyn + ScriptDOM) | Calls, procs, tables, constants, duplicate and cross-layer candidates |
| 1.3 | Identifier evidence | Abbreviated column and variable names, with the comments, labels, definitions and set/test sites that show what they mean. Appended to each map page. The model must state a meaning only when the evidence supports it, and otherwise write "meaning: Unknown". |
| 1.7 | Cost estimate and budget check | Refuses to start over `DISTILL_MAX_USD`. See "Budget cap". |
| 2 | 11 extraction passes per page | The long, costly stage |
| 2.2–2.6 | Reduce, knowledge layer, synthesis, format | Dedupe and classify into concepts |
| 2.8–2.97 | Code graph, arbitration, concept↔code bridge, graph export | Every edge checked against source |
| 2.98 | Issues & Insights | Deterministic candidates confirmed by the model |
| 3–4 | Master blend, bottled spec | Citations carried through and verified |
| 6 | Viewer | Static HTML plus optional chat API |

A failed required stage ends the run non-zero, sets `run_status.json` to `failed`, and prints
no completion banner. A budget stop does the same with exit 6 and status `budget_exhausted`.

### Verify the run

```bash
cat output/ap-invoices/run_status.json          # expect "complete", failed_stages []
cat output/ap-invoices/conservation_report.json # expect ok: true
python3 runtimes/mcp-server/telemetry.py cost output/ap-invoices   # calls, tokens, cost by phase
```

`telemetry.py` accepts the run directory or its `telemetry/` folder. It exits 1 with an error
when it finds no records. It never reports a silent $0.

### Open the viewer

```bash
runtimes/mcp-server/.venv/bin/python runtimes/mcp-server/serve_viewer.py \
  output/ap-invoices/viewer output/ap-invoices/stills/shared_vectors.db \
  --source-root /tmp/ap-src --port 8080
# http://localhost:8080
```

Chat and Validate need AWS credentials in the environment; each chat question sends the whole
spec (~190K tokens, about $0.60). Pages only, no chat: `cd output/ap-invoices/viewer &&
python3 -m http.server 8080 --bind 127.0.0.1`.

## 3. Output

Written under `DISTILL_OUTPUT` (`output/` is gitignored). From the 103 KB sample:

| Path | What | Size on the sample |
|---|---|---|
| `bottled_spec/README.md` | One-page executive summary | 3.8 KB |
| `bottled_spec/bottled_spec.md` | **Business-facing spec** — 317 "SHALL" requirements, requirements index, citations | 438 KB |
| `issues/issues.md`, `issues.json` | **Issues & Insights** — 26 ranked findings with verified locations, severity, evidence class, SME question | 33 KB |
| `knowledge_graph/` | `knowledge_graph.json` (concepts), `code_graph.json` (code structure), `concept_code_bridge.json` (links), `index.json` | 1.4 MB |
| `viewer/` | Browsable HTML: Issues, Business Spec, Knowledge Graph, extraction pages | 5.6 MB |
| `master_blend/master_blend.md` | Full working spec with audit detail | 1.2 MB |
| `stills/` | Raw per-pass extractions plus `shared_vectors.db` | 11 MB |
| `facts/` | Parser facts and `graph.json` | 1.4 MB |
| `run_status.json`, `run.log`, `telemetry/` | Status, log, per-call token and cost records | |
| `budget_estimate.json` | Phase 1.7 per-stage estimate, unit costs and their source | |
| `identifiers/` | Phase 1.3 glossary (`identifiers.json`) and the per-page evidence blocks | |
| `.enumeration/source_inventory.json`, `excluded.tsv` | Exactly what went in, with hashes, and what was excluded and why | |
| `source_provenance.json`, `.source/` | Git input only: the URL (credentials stripped), the ref, the resolved commit and the subfolder; the shallow clone | |
| `conservation_report.json`, `preflight_report.json` | Source-coverage and pre-run checks | |

### Reference run and scores

One reference run on this sample (Claude Sonnet 4.6; outputs are not shipped), scored against the
hidden 48-item answer key in `synthetic-corpus/ap-invoices/ANSWER_KEY.yaml`. A new run will differ:

| Measure | Result |
|---|---|
| Known items found, any match | 95.8% (95% CI 89.6–100%), stable across judge passes |
| Known items found, full match | 70.8–89.6% depending on segmentation |
| Planted problems caught | 5 of 7 fully in the spec, 2 partially (BR-020 is flagged fully in the Issues report) |
| Incorrect findings | 1 |
| Requirements with citations | 306 of 317 (all citations verified) |

## 4. Score your own run

```bash
python3 eval/score.py \
  --spec output/ap-invoices/bottled_spec/bottled_spec.md \
  --key synthetic-corpus/ap-invoices/ANSWER_KEY.yaml \
  --source-root synthetic-corpus/ap-invoices \
  --exclude ANSWER_KEY.yaml --exclude README.md \
  --max-source-chars 200000 --out-dir output/ap-invoices/validation
```

Add `--no-llm` for a free rough check; the LLM judge costs a few dollars. Benefit-oriented
evaluations (issue detection, Q&A, change impact, new-app compatibility) are in
`eval/benefits/` — see `eval/README.md` and `eval/benefits/README.md`.

## 5. Tests

```bash
python3 -m pytest -q runtimes/pipeline/tests runtimes/mcp-server/tests runtimes/facts/tests
# no AWS calls (git input: test_source_fetch.py; launcher: test_launcher.py)
```

## Notes

- Use a non-production AWS account. Only send code to Amazon Bedrock that you are permitted to
  process there.
- This is sample code, not a product: see "Maturity" and "Hardening" in the top-level README.
