<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->
# Launcher API

`runtimes/mcp-server/launcher_server.py` is a small local JSON API to launch and monitor
`run_distillation.sh` runs. It also serves one plain HTML form at `/`, built on the same API.
The form is a thin example. A product UI or MCP connector can call the API directly and replace it.

Python standard library only (`http.server`), no build step. Sample code, demo grade: read
"Maturity" and "Hardening" in the top-level [`README.md`](../README.md) before changing how it is exposed.

```bash
cd runtimes/mcp-server
AWS_PROFILE=<your profile> AWS_DEFAULT_REGION=us-east-1 \
  .venv/bin/python launcher_server.py --port 8090
# http://127.0.0.1:8090/
```

| Flag | Default | |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. A non-loopback address is refused. |
| `--allow-non-loopback` | off | Allows a non-loopback `--host` and prints a warning. Use it only on an isolated, access-controlled network. Never expose the server publicly. |
| `--port` | `8090` | |
| `--max-concurrent` | `1` | Runs that execute at once. Further runs wait in a queue. |
| `--runs-dir` | `output/runs` | Each run gets its own output directory, `<runs-dir>/<run_id>/`. |
| `--max-usd-ceiling` | `500` | The largest `max_usd` a request may ask for. |
| `--estimate-timeout` | `1800` | Seconds allowed for the pre-run estimate (clone and paging). |
| `--cors-origin` | none | A browser origin allowed to call the API, for example `http://localhost:3000`. Repeat the flag for more origins. |
| `--import-run DIR` | none | Register an existing, completed output directory (for example `output/ap-invoices`) as a read-only **imported run**, so the UI has data without a new paid run. Repeatable. See "Imported runs". |
| `--eval-python` | this Python | The interpreter that runs `eval/ci/run_suite.py` for `POST /api/runs/<id>/eval`. It needs PyYAML, like the rest of the eval harness. |

## Safety rules

- **Credentials are never accepted.** Runs inherit the launcher process's environment: its
  `AWS_PROFILE`, instance role or SSO session. A request is rejected with 400 when any field name,
  at any depth, looks like a credential (`aws_*`, `secret`, `token`, `password`, `credential`,
  `access_key`, `session`, `auth`, ...). Unknown fields are rejected too.
- **Git URLs with credentials are refused**, for example `https://user:token@host/...` or
  `https://token@host/...`. Use an SSH key in ssh-agent, or a git credential helper. Plain
  `http://` and `git://` are refused as well.
- **Loopback only by default.** The Host header must be a loopback name, which guards against DNS
  rebinding. A `POST` must send `Content-Type: application/json`. If it sends an `Origin`, that
  origin must be the server's own or one passed with `--cors-origin`.
- **Auth model (shared access code, demo grade).** When `DISTILL_ACCESS_CODE` is **unset**, the
  launcher behaves exactly as documented here (no gate). When it is set, every route **except
  `GET /healthz` and the login path** requires a session: a browser is served a themed access-code
  page (**HTTP 200**); an API caller without a session gets **401 JSON**
  `{"error":"authentication required","login":"/api/auth"}`. The session cookie is an opaque,
  server-minted token (never the code); it is `HttpOnly; Secure; SameSite=Lax` and in-memory, so a
  service restart logs everyone out. A single shared code is **not** user authentication: there is
  no per-user identity, no authorization, no audit trail and no lockout. Do not rely on it to
  expose the launcher beyond your own machine (see "Hardening" in the top-level README).
- **Allowed-hosts env.** `DISTILL_ALLOWED_HOSTS` (comma-separated) **extends** — it does not disable
  — the Host (`_host_ok`) and Origin (`_origin_ok`) guards to accept an additional host name (for
  example a reverse proxy's) and the `https://` same-origin for it. An unknown Host or Origin is still
  refused **403**. Unset ⇒ no change to the loopback-only behavior above.
- **Data.** Use synthetic or approved non-production code only. The form shows this banner.
- **Per-run viewer routes.** `chat`, `validate` and `translate` call Bedrock with the launcher
  process's own AWS identity — no credential input anywhere. Each per-run `POST` is screened by a
  per-route field allowlist (any unknown key is rejected with 400) plus the same credential-key
  rejection as `POST /api/runs`. `validate` reads only evidence paths contained under the run's
  analysed source root (resolved from `source_provenance.json`'s `target` for a git run, or
  `run_manifest.json`'s `source.path` for a local-path run) and enforces an `is_relative_to`
  containment check, so a traversal reference such as `../../etc/passwd` is skipped, never read.
  Evidence links in a `validate` response appear only when a GitHub blob base was recorded for the
  run (the launcher sets `DISTILL_SOURCE_REPO_URL` and the shell writes it to `run_manifest.json`'s
  `source.repo_url`); otherwise the links are omitted, never broken. The embedded viewer reaches
  these routes through an injected `API_BASE` of `/api/runs/<id>`; viewers generated before this
  change use bare `/api/*`, so their chat/validate/dependencies/translate are unavailable through
  the launcher (progress and tpm still work for any run — see below).
- **Evaluation routes.** `POST /api/runs/<id>/eval` is screened like `POST /api/runs`: JSON only,
  a field allowlist (unknown key → 400) and the same credential-key rejection. `answer_key` and
  `suite` are **ids** from `GET /api/eval/options` (the server's own list, read from
  `eval/ci/suites/*.yaml`); a path, or anything containing `/`, `\` or `..`, is refused with 400,
  so no client string reaches the `run_suite.py` command line as a path. Every evaluation is
  planned first (`run_suite.py --plan`, no model call) and refused with 409 when the plan is over
  `budget_usd`; the cap and `judge_runs` are then passed down to `run_suite.py`, which passes
  the remaining budget to each LLM stage as its own hard cap. One evaluation at a time per run.
  The eval routes and the viewer's Evaluation page go through the same Host, Origin and gate
  checks as every other route (no new exemption). Bedrock calls, when a judged evaluation is
  started, use the launcher process's own AWS identity.
- **Imported runs are read-only.** The launcher never writes into a `--import-run` directory: no
  `launcher.json`, `start`/`cancel` → 409, and evaluations of it are written under
  `<runs-dir>/_evals/<run_id>/`. The viewer's Evaluation page and the extra tab are produced at
  serve time, not written to disk.

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /healthz` | Unauthenticated liveness: `200 {"status":"ok"}`. Exempt from the gate and the Host guard (unconditionally for `GET /healthz`); discloses nothing else. A plain unauthenticated liveness probe (also handy as a CloudFront origin-reachability check); there is no load balancer health check. |
| `POST /api/auth` | Log in with the shared access code. Body `{"code":"<string>"}`, same-origin + JSON required. Correct code → `200 {"ok":true}` plus an `HttpOnly; Secure; SameSite=Lax` `__Host-distill_session` cookie; wrong code → `401 {"error":"incorrect access code"}` with no cookie. The code is compared in constant time (`hmac.compare_digest`) and never logged. An API caller authenticates by POSTing here, then sends the returned cookie on subsequent requests. |
| `POST /api/runs` | Validate the request and estimate the cost (no model call), then queue the run. With `"estimate_only": true`, only estimate. |
| `POST /api/runs/<id>/start` | Start a run that was created with `estimate_only`. |
| `GET /api/runs` | All runs, newest first: `{"runs": [<run>, ...]}` |
| `GET /api/runs/<id>` | One run (see "Run object"). |
| `GET /api/runs/<id>/log?tail=N` | The last N lines of `run.log`. Default 200, maximum 5000. |
| `POST /api/runs/<id>/cancel` | Cancel a queued, estimated or running run. A running run gets SIGTERM to its process group, then SIGKILL after 15 s. |
| `GET /api/runs/<id>/resume` | The exact resume command of a `budget_exhausted` run. Returns 409 for any other status. |
| `POST /api/runs/<id>/chat` | RAG question against the run's vector store (the viewer's chat panel). |
| `POST /api/runs/<id>/validate` | Validate a spec block against its source evidence, or sibling blocks when no source is available. |
| `POST /api/runs/<id>/dependencies` | Code-dependency prerequisites for a concept, from the run's code graph. |
| `POST /api/runs/<id>/translate` | Translate a formal predicate into a target language (the viewer's per-still translate button). |
| `GET /api/runs/<id>/progress` | Live, filesystem-derived progress: phases, per-still map/reduce/knowledge/synthesis/format state, source counts. |
| `GET /api/runs/<id>/tpm` | Tokens-per-minute timeseries and totals for the run. |
| `GET /api/config` | Available stills, limits, the default model and the AWS profile name (never a credential). |
| `GET /api/runs/<id>/eval` | The run's evaluation, aggregated from whatever eval artifacts exist, with **exact** and **estimated** kept apart (see "Evaluation"). Read-only, no model call. |
| `POST /api/runs/<id>/eval` | Plan (`run_suite.py --plan`), then start a budget-capped evaluation in the background; `{"smoke": true}` is the free deterministic check. |
| `GET /api/runs/<id>/eval/status?tail=N` | The run's latest evaluation job: state, planned and live spend, verdict, log tail (default 40 lines, maximum 1000). |
| `GET /api/eval/options` | Suites and answer keys the server knows (ids and labels, never a client-usable path), judge-pass limits, the budget ceiling. |
| `POST /api/runs/<id>/dataset`, `GET .../dataset[/status]`, `GET .../dataset/items`, `POST .../dataset/review` | Build, inspect and review a run's **test data set** (see "Test data sets"). |
| `GET /api/eval/compare?runs=<id1>,<id2>` | Two runs' evaluations side by side, each difference flagged as inside or outside the measured noise. |
| `GET /runs/<id>/files/<path>` | Run outputs: `issues/`, `bottled_spec/`, `master_blend/`, `knowledge_graph/`, `viewer/`, plus `run_status.json`, `budget_estimate.json`, `source_provenance.json`, `run_manifest.json`, `run.log`. The cloned source is not served. `viewer/pages/evaluation.html` is always the current Evaluation page (rendered for this run, for any run); `viewer/index.html` gets an **Evaluation** tab added when the shell predates it. |

Errors always come back as JSON: `{"error": "<message>", ...}`. The status codes are 400
(validation), 404, 409 (state conflict or estimate over the cap), 413, 415, 422 (the estimate
failed, for example an unknown git ref) and 403 (Host or Origin refused).

### `POST /api/runs`

Request body:

| Field | Type | |
|---|---|---|
| `source` | object, required | `{"type": "git", "url": "...", "ref": "...", "subdir": "..."}` (`ref` and `subdir` are optional) or `{"type": "path", "path": "/abs/dir"}` |
| `name` | string, optional | 1 to 64 characters: letters, digits, `.`, `_`, `-`. Becomes `DISTILL_TARGET_NAME` and the slug segment of the `run_id`. When omitted or blank, the server derives it from the source (git repo name with `.git` stripped, or path basename), so the id is `YYYYMMDD-HHMMSS-<slug>-<hex4>`. |
| `max_usd` | number, optional | Budget cap (`DISTILL_MAX_USD`). Omit or leave blank for **no cap**: the estimate still runs but cannot trip `over_estimate`, and the run is not stopped by a ceiling. A number enforces the cap, cumulative for the run directory. |
| `model_id` | string | `BEDROCK_MODEL_ID`. Default: the launcher environment's value, otherwise Sonnet 4.6. |
| `stills` | string list | Subset of `GET /api/config` `stills`. Default: all. |
| `facts` | bool | `DISTILL_FACTS`. Default on. |
| `issues` | bool | `DISTILL_ISSUES`. Default on. |
| `allow_over_estimate` | bool | Start even when the estimate is over `max_usd`. The run still stops at the cap. |
| `estimate_only` | bool | Estimate and stop. Start the run later with `POST /api/runs/<id>/start`. |

What happens:

1. The request is validated. The git URL, ref and subfolder are checked by `source_fetch.py`.
2. `run_distillation.sh` runs with `DISTILL_ESTIMATE_ONLY=1` into `output/runs/<run_id>/`. It
   clones the git source shallowly into `.source/`, enumerates and pages it, and writes
   `budget_estimate.json`. It makes no model call and does not need AWS credentials.
3. If a `max_usd` cap was given, the estimate is over it, and `allow_over_estimate` is not set,
   the response is **409** and the run's status is `over_estimate`. Nothing was spent. With no
   `max_usd`, there is no cap and this refusal cannot occur.
4. Otherwise the run is queued (**202**). For a git source, the full run is pinned to the commit
   that was estimated. It reuses the enumeration and the pages at $0.

```bash
# name and max_usd are both omitted: the id is derived from the repo name + timestamp
# (e.g. 20261007-142501-legacy-erp-sample-3fa2) and the run has no budget cap.
curl -s -X POST http://127.0.0.1:8090/api/runs -H 'Content-Type: application/json' -d '{
  "source": {"type": "git", "url": "https://github.com/<org>/legacy-erp-sample.git",
             "ref": "main", "subdir": "samples/ap-invoices"}}'

# To enforce a cap instead, add "max_usd": 25 (an over-estimate then returns 409).
```

202 response (run object, abbreviated):

```json
{
 "run_id": "20261007-142501-ap-invoices-3fa2",
 "name": "ap-invoices",
 "status": "running",
 "phase": "Phase 1.7",
 "spent_usd": 0.0,
 "estimate_usd": 17.35,
 "max_usd": 25.0,
 "estimate": {"estimate_usd": 17.35, "pages": 3, "stills": ["data", "..."],
              "remaining_usd_by_stage": {"map": 5.61, "reduce": 1.78, "...": 0},
              "unit_cost_source": "default table (AP + JC reference runs, Sonnet 4.6, 22K-token pages)",
              "suggested_max_usd": null, "commit": "1a2b3c4d5e..."},
 "source": {"type": "git", "url": "https://github.com/<org>/legacy-erp-sample.git",
            "ref": "main", "subdir": "samples/ap-invoices",
            "commit": "1a2b3c4d5e...", "ref_resolved": "refs/heads/main"},
 "links": {"log": "/api/runs/20261007-142501-ap-invoices-3fa2/log?tail=200",
           "budget_estimate": "/runs/20261007-142501-ap-invoices-3fa2/files/budget_estimate.json"},
 "resume_available": false
}
```

409 response (estimate over the cap):

```json
{
 "error": "estimate $17.35 is over max_usd $0.01; raise max_usd (suggested $20.00) or set allow_over_estimate: true. No model call was made.",
 "run_id": "20261007-142733-ap-invoices-91c0",
 "status": "over_estimate",
 "estimate_usd": 17.35,
 "max_usd": 0.01,
 "suggested_max_usd": 20.0
}
```

400 responses:

```json
{"error": "credential fields are not accepted: aws_secret_access_key. The launcher uses its own process environment / AWS profile only; set AWS_PROFILE (or instance credentials) where the launcher runs.", "fields": ["aws_secret_access_key"]}
{"error": "the git URL contains credentials (user/token before '@'); refusing to use it. Remove the credentials from the URL and use an SSH key (ssh-agent, ssh://... or git@host:path) or a git credential helper (git config credential.helper) instead.", "field": "source"}
```

422 response (the estimate could not run):

```json
{"error": "pre-run estimate failed: source fetch: unknown ref 'nope' in file:///tmp/repo.git (no such branch or tag)", "run_id": "...", "status": "failed", "log_tail": ["..."]}
```

### Two-step flow (what the form does)

```bash
# 1. estimate only (200, status "estimated"; nothing queued). name and max_usd omitted:
#    the id is derived from the folder basename + timestamp, and there is no budget cap.
curl -s -X POST localhost:8090/api/runs -H 'Content-Type: application/json' \
  -d '{"source": {"type": "path", "path": "/tmp/ap-src"}, "estimate_only": true}'
# 2. start it (202), or discard it with POST /api/runs/<id>/cancel
curl -s -X POST localhost:8090/api/runs/<run_id>/start
```

### Run object (`GET /api/runs/<id>`)

| Field | |
|---|---|
| `run_id`, `name` | |
| `status` | `estimating`, `estimated`, `queued`, `running`, `complete`, `failed`, `budget_exhausted`, `over_estimate`, `cancelled`, or `interrupted` (the launcher restarted while the run was active) |
| `phase` | The last `Phase N` the log reached, `done` after the completion banner, or `stage_reached` from `run_status.json` for budget stops |
| `spent_usd` | Actual spend so far, from `<run>/telemetry/` (the same logic as `budget.py`) |
| `estimate_usd`, `estimate` | The pre-run estimate: projected total, per-stage amounts, pages, stills, unit-cost source, git commit |
| `max_usd` | The cap, or `null` when the run was created without one (no ceiling) |
| `created`, `started`, `finished` | UTC ISO-8601 timestamps |
| `exit_code`, `error` | The `run_distillation.sh` exit code. `error` holds the failed stages or the reason. |
| `source` | The request source with the URL stripped of userinfo, plus the resolved `commit` and `ref_resolved` for git |
| `links` | Links to outputs that exist: `issues_md`, `issues_json`, `bottled_spec`, `bottled_summary`, `master_blend`, `knowledge_graph_index`, `knowledge_graph`, `code_graph`, `viewer`, `run_status`, `budget_estimate`, `source_provenance`, `run_manifest`, `log` |
| `viewer_command` | A fallback command that starts `serve_viewer.py` standalone. The viewer is normally opened directly from the UI (**Open viewer**, `links.viewer`): the launcher serves it and answers its per-run `/api/*` calls, so no separate server is needed. |
| `resume_available` | true when `status` is `budget_exhausted` |
| `imported`, `read_only` | true for a `--import-run` directory |
| `eval_state` | State of the run's latest evaluation job (`planning`, `planned`, `running`, `complete`, `failed`, `budget_exceeded`, `over_budget`, `interrupted`), or `null` |

`links` also carries `eval` (`/api/runs/<id>/eval`), `eval_status`, `eval_page`
(`/runs/<id>/files/viewer/pages/evaluation.html`) and, when the run has a viewer, `viewer_eval`
(`/runs/<id>/files/viewer/index.html#evaluation`, the viewer opened on its Evaluation tab).

### `GET /api/runs/<id>/log?tail=50`

```json
{"run_id": "...", "tail": 50, "lines": ["[14:25:03] Phase 0: Enumerating source + building source inventory...", "..."]}
```

### `GET /api/runs/<id>/resume`

```json
{
 "run_id": "...",
 "status": "budget_exhausted",
 "stage_reached": "2 map",
 "spent_usd": 1.24,
 "suggested_max_usd": 20.5,
 "remaining_estimate_usd": 16.6,
 "resume_command": "AWS_DEFAULT_REGION=us-east-1 DISTILL_GIT_REF=1a2b3c4... DISTILL_GIT_REF_REQUESTED=main DISTILL_GIT_URL=... DISTILL_MAX_USD=20.50 DISTILL_OUTPUT=/.../output/runs/<id> ... bash /.../run_distillation.sh",
 "note": "Run it in a shell with the same AWS profile; completed stages are reused at $0."
}
```

The launcher does not run resumes itself. Run the command in a shell. For a git source it is
pinned to the analysed commit. It never contains secrets: only `DISTILL_*`, `SKIP_*`, the
profile and region names, and the model id.

### Per-run viewer routes

These power the chat-enabled viewer the launcher serves at
`/runs/<id>/files/viewer/index.html`. All resolve the run's
`stills/shared_vectors.db`, viewer directory and analysed source root from the run directory.
A `POST` returns **404** when the run (or, except for `translate`, its vector store) is missing,
and otherwise **200** — handled model/credential errors are returned in the body as
`{"error": "..."}` so the browser's `r.json()` never throws. Each body is screened by the
per-route field allowlist and credential-key rejection described in Safety.

**`POST /api/runs/<id>/chat`** — body `{"question": "...", "top_k": 15}` (`top_k` optional,
default 15). Response: `{"answer", "sources", "input_tokens", "output_tokens"}` or `{"error"}`.

**`POST /api/runs/<id>/validate`** — body `{"title", "block", "evidence"}`. Response:
`{"answer", "source_content", "title", "cross_refs", "evidence_refs"}` or `{"error"}`.
`evidence_refs[].url` is populated only when the run recorded a GitHub blob base.

**`POST /api/runs/<id>/dependencies`** — body `{"title", "still"}` (`still` optional). Response:
the code-dependency result `{requires, required_by, anchors, note, ...}` or `{"error"}` (for
example `"Code dependency graph not built yet..."` when the graph tables are empty).

**`POST /api/runs/<id>/translate`** — body `{"code", "language"}`. Response:
`{"code", "language"}` or `{"error"}`. Needs no vector store.

A viewer generated with `--api-base /api/runs/<id>` builds its calls as `API_BASE + "/api/<route>"`,
that is `/api/runs/<id>/api/<route>`. The launcher treats that form as the same route as
`/api/runs/<id>/<route>` (for `chat`, `validate`, `dependencies`, `translate`, `eval` and
`eval/status`); only the path segments are folded, after the usual Host/gate/Origin checks.

### `GET /api/runs/<id>/progress`

Live, read-only progress derived from the run directory (no model call, no viewer regeneration —
works for any run regardless of when it was produced):

```json
{"timestamp": "14:25:03", "output_dir": "...", "phases": {"pages": {"status": "complete", "count": 3}, "...": "..."},
 "stills": {"rule": {"phase": "mapping", "map_pct": 60.0, "...": "..."}}, "source": {"files": 42, "top_types": ".py(30), ..."}}
```

404 for an unknown run.

### `GET /api/runs/<id>/tpm`

Tokens-per-minute timeseries and totals from the run's `telemetry/` directory
(`{"timeseries", "peak_input_tpm", "total_input_tokens", "total_output_tokens", "total_calls", ...}`);
zeroed when `telemetry/` is absent. 404 for an unknown run.

### `POST /api/runs/<id>/cancel`

Returns 200 with the run object (`status: "cancelled"`), or 409 when the run has already finished.
Completed stages stay on disk. A later run into the same directory would reuse them, but the
launcher always creates a new directory per run.

## Evaluation

The launcher shows and starts the evaluation suite in `eval/` (see `../eval/README.md`, in
particular "What these numbers mean"). It never imports an evaluator: it reads the files they
wrote (`runtimes/mcp-server/eval_view.py`) and starts `eval/ci/run_suite.py` as a subprocess.

**Results are on synthetic samples with known answers unless a key is provided. For code without
a key only the Exact checks apply.** Every response carries that banner and the legend
*Exact: same answer every time, no AI* / *Estimated: LLM-as-a-judge (Claude on Bedrock), shown
with 95% CI*.

### Guided flow: automatic grounding check and `score_state`

The page shows one guided flow per finished run (legend: *Grounded = checked against your code,
no AI. Score = compared with the known rules, graded by AI.*). The server does the bookkeeping:

- **Automatic grounding check.** When a run reaches `complete`, and when an imported or earlier
  run is first listed (`GET /api/runs` or `GET /api/runs/<id>`), the launcher starts the free
  Quick check (`{"smoke": true}`, `trigger: "auto"` on the eval job) once per run. Never for a
  run that cannot be evaluated (`eval_refusal`), never while an evaluation is active, never when a
  smoke job already exists (also after a restart) or a smoke `suite.json` is already found for the
  run (e.g. the repo's `eval/ci/baselines/ap-smoke-*.suite.json`). Off with `--no-auto-check`
  (the `Launcher(auto_grounding=...)` default is off, so library users and tests opt in).
- **`GET /api/runs/<id>`** adds two summaries (the list endpoint does not, to stay cheap):
  - `grounding`: `state` (`none`, `running`, `passed`, `warning`, `failed`, `unavailable`),
    `summary` (the one line the page shows, e.g. `✓ Grounded — 2,336 lines read in 16 files · all
    725 citations point at real code · graph matches the code parser (100%)`, `⚠ Needs attention —
    3 of 40 citations do not point at real code · …`, or `Checking grounding… (free)`), the facts
    (`lines`, `files`, `citations_total`, `citations_invalid`, `graph_parser_match_pct`,
    `problems`, `verdict`, `exit_code`), `eval_id`, `trigger` and `details` (the viewer's
    Evaluation tab).
  - `score_state`: `key` (`readiness` `none` | `draft` | `approved` | `shipped`, `rules`,
    `label`, `building`, `provisional`, `note`, the body fields to send as `use`, and the data
    set's `total` / `corroborated` / `approved` / `reviewed`), the latest `plan` (`planned_usd`,
    `cap_usd` = estimate + 25%, at least $0.50, `current`), the latest `scoring` job (`state`,
    live `spent_usd`, `budget_usd`), `result` (`summary` e.g. `Scored: 81.2% of known rules found
    (range 75–88%) · 3 of 6 planted problems caught`, `range`, `ci_width`, `provisional`, `note`,
    `double_check` = a judged 95% CI wider than 15 points with fewer than 3 grading passes),
    `step` (`key`, `cost`, `running`, `results`) and `busy` (the mode of an active evaluation).
    Key precedence: an approved data set, then a shipped answer key (AP / JC / eShopOnWeb
    suites), then an unreviewed draft (scored results are then Provisional).
- The actions reuse the existing endpoints: `POST /dataset`, `POST /dataset/review`
  (`{"approve": "all" | "corroborated"}`), `POST /eval` with `plan_only` and then without it.

### `GET /api/runs/<id>/eval`

Looks for evaluation artifacts in three places, all read-only:

1. `<run>/validation/**` — the run's own outputs (`score.json`, `graph_eval.json`, `suite.json`,
   `qa.json`, `impact.json`, `fieldapp.json`, `insights.json`)
2. `<runs-dir>/_evals/<run_id>/**` — evaluations this launcher started
3. `eval/runs/**` and `eval/ci/baselines/` — local or committed records, used **only** when their recorded
   provenance (`spec`, `meta.output_dir`, `provenance.vars.run`) names this run directory

An artifact whose provenance names a different run is skipped and listed in `skipped_sources`,
even inside the run's own `validation/` (for example a `graph_eval.json` copied from an earlier run). For each spec artifact (`bottled_spec`, `master_blend`, `issues`) one `score.json` is
selected: LLM-judged before `--no-llm`, with a CI before without, more judge passes, then newest.
The others are listed as `alternatives`. The newest `graph_eval.json` is selected the same way.
Plus `<run>/.pages/conservation_report.json`, `telemetry/` and `run.json` / `run_manifest.json`.

```json
{
 "legend": {"exact": "Exact: same answer every time, no AI.", "estimated": "Estimated: LLM-as-a-judge (Claude on Bedrock), shown with 95% CI."},
 "banner": "Results are on synthetic samples with known answers ...",
 "has_answer_key": true,
 "exact": {
  "conservation": {"status": "measured", "ok": true, "lines": {"value": 2336, "status": "measured"}, "files": {"value": 16, "status": "measured"}},
  "spec_artifacts": [{"artifact": "bottled_spec", "citation_validity_pct": {"value": 100.0, "status": "measured"}, "citations_valid": {"value": 725, "status": "measured"}, "structural": {"...": "..."}}],
  "graph": {"parser_confirmed_edge_precision_pct": {"value": 97.56, "status": "measured", "confirmed": 40, "llm_edges": 41, "parser_edges": 60},
            "parser_edge_recall_pct": {"value": 66.67, "status": "measured"},
            "by_language": {"vbnet": {"label": "VB.NET", "recall_pct": {"value": 0.0, "status": "measured"}, "...": "..."},
                            "sql": {"...": "..."}, "csharp": {"label": "C#", "value": null, "status": "not_measured", "reason": "no C# edges in the parser graph for this corpus"}},
            "method_call": {"precision_pct": {"...": "..."}, "recall_pct": {"...": "..."}},
            "bridge": {"link_assertion_pct": {"value": 91.76, "status": "measured"}, "...": "..."}},
  "cost": {"pipeline_usd": {"value": 17.2092, "status": "measured", "calls": 221}, "evaluation": [{"source": "...", "usd": 6.09}], "evaluation_total_usd": 6.7343}
 },
 "estimated": {
  "spec_artifacts": [{"artifact": "bottled_spec", "method": "llm", "judge_runs": 3,
     "metrics": {"recall_any_match_pct": {"value": 95.8, "ci95": [89.6, 100.0], "across_runs": {"runs": [95.8, 95.8, 95.8]}, "noise_band": 1.0, "status": "measured"},
                 "recall_strict_full_pct": {"value": 70.8, "ci95": [58.3, 83.3], "...": "..."}, "precision_keyed_pct": {"...": "..."},
                 "incorrect": {"value": 1, "...": "..."}, "uncertain": {"value": 4, "...": "..."}},
     "agreement": {"fleiss_kappa_verdicts": {"value": 0.8997, "label": "almost perfect", "status": "measured"}},
     "golden": {"status": "measured", "n_labelled": 15, "agreement_pct": 86.7, "disagreements": [{"key_id": "BR-020", "judge": "full", "human": "partial", "basis": "..."}]}}],
  "traps": {"status": "measured", "key": "synthetic-corpus/ap-invoices/ANSWER_KEY.yaml", "rows": [{"id": "BR-020", "categories": ["inconsistency"], "outcomes": {"bottled_spec": "full"}, "locations": [{"ref": "ClientSide/AP/AP/modAPGlobals.vb:41-44"}], "judge_vs_human": ["..."]}], "by_category": {"...": "..."}},
  "benefits": {"qa": {"status": "not_measured", "reason": "no evaluation of this benefit was found for this run", "metrics": {}}, "...": "..."}
 },
 "suites": [{"mode": "smoke", "verdict": "pass", "spent_usd": 0.0, "cap_usd": 0.0, "stages": ["..."], "provenance": {"commit": "...", "model_id": "...", "region": "us-east-1", "corpora": {"...": "..."}}}],
 "provenance": {"run": {"model_id": "us.anthropic.claude-sonnet-4-6", "region": "us-east-1", "source_inventory_hash": "..."}, "evaluations": ["..."]},
 "sources": ["..."], "skipped_sources": ["..."], "eval_job": null
}
```

Rules the response follows:

- **Exact and estimated never share a metric.** Exact: lines conserved, citation validity and
  counts, structural checks, parser-confirmed graph precision/recall (overall, VB.NET, SQL, C#,
  model-proposed only, method calls), bridge link assertions, cost of calls made. Estimated:
  recall any/full/partial, precision, incorrect, uncertain, judge passes, kappa, golden
  agreement, per-trap outcomes, the LLM bridge sample, benefit evaluations.
- **A missing or null metric is never 0.** It is `{"value": null, "status": "not_measured",
  "reason": "..."}`. Examples: kappa with one judge pass (*needs at least 2 judge passes*), a
  ratio whose denominator is zero (*the graph proposed no method-call edges*), C# on a corpus
  without C#, a benefit the suite never reached (*suite stage benefits-fieldapp was
  budget_exceeded: ...*). A benefit report whose own `measurement.status` is `not_measured` has
  all its numbers marked not quotable (kept as `recorded_value` for audit, never displayed).
- **A judged number is never bare.** `ci95` when the scorer recorded a bootstrap CI; otherwise
  `noise_band`, the measured judge noise for that metric family from
  `eval/ci/suite_lib.py` `DEFAULT_TOLERANCES` (± 11 points on full-match recall, ± 1 on
  any-match), with `uncertainty` saying which.
- **`--no-llm` / smoke scores are not judged recall.** Their exact parts (citations, structure)
  count; their recall/precision are `not_measured` (*not a judged recall; never quote it*).
- **Per-trap rows** come from the answer key's `flags`, resolved on the server (the server's own
  key list wins over a recorded path). A `url` appears on a location only when the run recorded
  a repository URL; otherwise locations are `file:start-end` without links.

### `POST /api/runs/<id>/eval`

| Field | Type | |
|---|---|---|
| `suite` | string, optional | An id from `GET /api/eval/options` (`ap-invoices`, `jc-changeorders`). Default: the suite of `answer_key`, else the suite whose corpus matches the run (`DISTILL_TARGET_NAME`, run name, directory name). |
| `answer_key` | string, optional | A key id from `GET /api/eval/options`. Must belong to `suite` when both are given. Never a path. |
| `judge_runs` | integer, default 1 | 1 to 5. Passed as `--judge-runs`. Use 3 for a number you intend to report. |
| `budget_usd` | number, **required** (not for smoke) | Hard cap, at most `--max-usd-ceiling`. Passed as `--budget-usd`. |
| `plan_only` | bool | Plan and stop: returns the planned cost, starts nothing. |
| `smoke` | bool | The free deterministic check: `run_suite.py --smoke` (budget forced to $0, exit 4 if any model call is made). No plan, no budget needed. |

What happens:

1. The body is validated (400 on any problem, 415 without JSON, 404 for an unknown run). If an
   evaluation of this run is already `planning` or `running`, the response is **409**.
2. `smoke`: started at once in the background (**202**).
3. Otherwise `run_suite.py --plan --budget-usd B --judge-runs N --var run=<run dir>` runs
   synchronously. It runs the deterministic stages and prices each LLM stage from the
   evaluator's own dry-run plan (no model call). `planned_usd` is the full price of the LLM
   stages, including any a capped run would never reach.
4. Over budget (`run_suite.py` exit 3, or `planned_usd > budget_usd`): **409**, nothing spent:

   ```json
   {"error": "planned evaluation cost $10.46 is over budget_usd $1.00; raise the budget (suggested $12.03) or lower judge_runs. Nothing was spent.",
    "state": "over_budget", "planned_usd": 10.4554, "budget_usd": 1.0, "suggested_budget_usd": 12.03,
    "plan": {"stages": [{"id": "score-master-blend", "status": "budget_exceeded", "estimated_usd": 5.1328, "estimate_source": "evaluator_plan", "cap_passed_usd": 1.0}, "..."]}}
   ```
5. `plan_only`: **200** with `state: "planned"` and the plan.
6. Otherwise `run_suite.py --budget-usd B --judge-runs N` starts in the background (**202**). It
   re-checks every stage's estimate against what is left and hands each LLM stage the remaining
   budget as its own cap.

Outputs go to `<runs-dir>/_evals/<run_id>/<eval_id>/` (`plan/`, `suite/`, `eval.log`,
`eval_job.json`), never into the run directory, and are picked up by `GET .../eval` at once.

```bash
curl -s -X POST localhost:8090/api/runs/<id>/eval -H 'Content-Type: application/json' -d '{"smoke": true}'
curl -s -X POST localhost:8090/api/runs/<id>/eval -H 'Content-Type: application/json' \
  -d '{"suite": "ap-invoices", "judge_runs": 1, "budget_usd": 15, "plan_only": true}'
```

### `GET /api/runs/<id>/eval/status?tail=N`

```json
{"run_id": "...", "eval_id": "20261008-131905-smoke-a9ab", "mode": "smoke", "suite": "ap-invoices",
 "state": "complete", "budget_usd": 0.0, "planned_usd": 0.0, "spent_usd": 0.0, "exit_code": 0, "verdict": "pass",
 "log_tail": ["[ok              ] graph-eval   0.37 s  $0.0000", "..."]}
```

`state` is `idle` when the run has never been evaluated. `spent_usd` is exact: while running it
sums the cost of every Bedrock call the suite has logged so far; at the end it is the suite's own
`budget.spent_usd`. `run_suite.py` exit 0 or 1 (a threshold failed) → `complete` (see `verdict`),
2 or 4 → `failed`, 3 → `budget_exceeded`.

### `GET /api/eval/compare?runs=<id1>,<id2>`

```json
{"runs": ["imported-ap-invoices-demo", "imported-jc-changeorders-v2"], "comparable": false,
 "warning": "the two runs were scored against different answer keys / corpora ...",
 "rows": [{"name": "bottled_spec.recall_any_match_pct", "kind": "estimated", "a": {"value": 95.8, "ci95": [89.6, 100.0]},
           "b": {"value": 94.1, "ci95": [88.2, 100.0]}, "delta": -1.7, "within_noise": true,
           "basis": "each value lies inside the other's 95% CI"},
          {"name": "graph.recall_vbnet_pct", "kind": "exact", "a": {"value": 0.0}, "b": {"value": 100.0}, "delta": 100.0,
           "within_noise": false, "basis": "exact: |Δ| 100 is a real difference"}]}
```

`within_noise` for an estimated metric: true when each value lies inside the other's 95% CI;
without CIs, when |Δ| is at most the measured judge-noise band for that metric
(`DEFAULT_TOLERANCES`, the same table `run_suite.py --baseline` uses). For an exact metric any
difference beyond its exact tolerance is real. `null` when either side is not measured.
`comparable` is false when the runs were scored against different answer keys.

## Test data sets

A test data set is an answer key built for one run, from that run's **source code and its tests**
(`eval/keygen/build_key.py`, deterministic, no AI unless asked). A person reviews it, and Full
evaluation then scores the spec against what they approved.

**Independence rule.** The launcher never builds a data set from pipeline output. The source is
the run's own copy: `<run>/.source/<subdir>` for a git run, the path for a path run, and the
recorded `DISTILL_TARGET` for an imported run. If that directory is the run's output, sits inside
it or inside the runs directory, contains the output, or looks like a pipeline output or viewer,
the request returns `409 {"reason": "independence"}`. `build_key.py` only receives `--source`, an
`--out` inside the data set directory, `--corpus` and `--date`. It never receives a spec, viewer
or other output path.

Everything is stored under `<runs-dir>/_datasets/<run_id>/<dataset_id>/`: `dataset.json`,
`build.log`, `ANSWER_KEY.draft.yaml`, `decisions.json`, `ANSWER_KEY.reviewed.yaml`, the filtered
`ANSWER_KEY.yaml` and the generated `suite.yaml`. Nothing is written into a run directory, so
imported runs stay read-only. All of these routes go through the same Host check, access-code
gate, Origin check (on POST), JSON content type check and credential-field rejection as the other
routes.

| Method and path | Purpose |
|---|---|
| `POST /api/runs/<id>/dataset` | Build a data set for a finished run (the same rule as Evaluate). Body `{}`, or `{"ai_suggestions": true, "max_usd": N}`, where N is required, must be > 0, and is capped at $10; this uses `build_key.py --llm-draft --max-usd N`. Returns `202` with the status object. Only one build per run can run at a time; a second request gets `409`. A missing source gets `409 {"reason": "source_unavailable"}`. The build runs in the background with a 900 s timeout. A rebuild keeps earlier review decisions, because item ids are stable. |
| `GET /api/runs/<id>/dataset` and `/dataset/status` | `state` (`none`, `building`, `ready`, `failed`, `interrupted`), plus `counts` with `total`, `corroborated`, `approved`, `reviewed`, `by_status` (`unreviewed`, `approved`, `edited`, `rejected`), `by_source` (`code`, `tests`, `AI`) and `by_type`. Also returns `key`, which says what Full evaluation will use and gives its plain-language label, the `independence` sentence and `log_tail`. |
| `GET /api/runs/<id>/dataset/items?status=&source=&q=&page=` | Returns 25 items per page. Filters: `status` is `needs_review`, `approved`, `edited`, `rejected`, `corroborated`, `reviewed` or empty; `source` is `code`, `tests`, `ai` or empty; `q` searches the text. Each item has `statement` (the edited text, if edited), `type`, `sources`, `corroborated`, `evidence_class`, `status`, `reviewer`, `locations`, and `excerpts`: line-numbered lines with the cited ones marked, from at most 2 locations. Excerpts are read from the run's source with a path check that refuses `..`, absolute paths and symlinks that point outside. |
| `POST /api/runs/<id>/dataset/review` | Body `{"decisions": [{"id", "action": "approve" | "reject" | "edit", "statement"?}], "reviewer"?: "<= 80 chars", "approve"?: "corroborated" | "all"}`. `"approve": "corroborated"` approves every item that code and tests both confirm; `"approve": "all"` approves every item nobody has decided on yet (rejected and edited items keep their decision; a second call changes nothing). Bulk decisions are stored with `bulk: "all"` / `"corroborated"`. A key with items approved via `"all"` is not provisional, but `key.note` (and `GET /eval` → `key_note`, shown on the viewer's scorecard) says `Answer key approved in bulk (not individually reviewed): …`; `counts.bulk_approved` counts them. Each decision is stored with its reviewer and a timestamp. Sending the same decision again changes nothing (`changed: 0`). After every call, `review.py` `apply_decisions` and `filter_key` rewrite the reviewed and filtered keys. |

**Evaluation.** `GET /api/eval/options?run=<id>` lists the run's data set first, as answer key and
suite id `dataset`. For that run, `POST /api/runs/<id>/eval` (plan or full) with no `suite` or
`answer_key` uses the data set. The launcher writes an ad-hoc `suite.yaml` from
`eval/ci/suites/eshoponweb.yaml`. It keeps the deterministic `corpus-sanity` stage and the keyed
`score-*` stages and their thresholds, sets `judge_runs: 1`, and sets the variables `corpus`
(the run's source), `run` and `key`.

- At least one approved item: the key is the filtered `ANSWER_KEY.yaml`, labelled
  `Using this run's test data set (N approved items)`.
- No approved items yet: the key is the draft, labelled
  `Provisional — test data set not yet reviewed (0 of M items approved)`. The eval job reports
  `provisional: true` and `key_label`. `GET /api/runs/<id>/eval` then sets `provisional` on the
  result, on `estimated` and on every estimated spec artifact.

Quick check (`{"smoke": true}`) works as before. It uses the data set only when asked for by id.

### Imported runs (`--import-run`)

```bash
.venv/bin/python launcher_server.py --import-run ../../output/ap-invoices-demo --import-run ../../output/jc-changeorders-v2
```

Each directory must look like a `run_distillation.sh` output (`run_status.json`, `run.json`,
`run_manifest.json` or `viewer/`). It appears in `GET /api/runs` as `imported-<dir name>`, with
`status` from its `run_status.json` and `imported: true`. Its outputs, viewer (with the
Evaluation tab), progress and evaluation work as for any run; `start` and `cancel` return 409.

### The viewer's Evaluation tab

`runtimes/pipeline/viewer_evaluation.html` is the scorecard page: an Exact panel and an
Estimated panel side by side, the planted-problem table, knowledge-graph accuracy by language,
benefit evaluations with status badges, the evaluation runs and their provenance. It holds no
numbers: it fetches `<API_BASE>/api/eval` when opened (and `<API_BASE>/api/eval/status` while an
evaluation runs, refreshing when it finishes), so a new evaluation shows up without regenerating
the viewer. `generate-viewer-v3.py` writes it to `viewer/pages/evaluation.html` with an
**Evaluation** tab; `serve_viewer.py` answers `GET /api/eval` for a standalone viewer.

## Statuses and `run_status.json`

The launcher's `status` comes from its own process tracking plus the pipeline's
`<run>/run_status.json`:

- `complete` means exit 0 with `run_status.json` = `complete`.
- `failed` means a failed required stage (exit 5), a hard error (exit 1), a preflight or content
  gate (exit 3), or the completeness barrier (exit 4).
- `budget_exhausted` and `over_estimate` mean exit 6. See `RUN_GUIDE.md`, "Budget cap".
