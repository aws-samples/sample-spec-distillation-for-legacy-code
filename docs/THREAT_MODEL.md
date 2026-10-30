<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->
# Threat model

This covers the sample as shipped: a pipeline you run on one machine with your own AWS credentials.
It deploys no AWS infrastructure. If you deploy it as a shared service, you need a new threat model;
[PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md) sketches what that would involve.

## System

| Component | What it does | Interface |
|---|---|---|
| `run_distillation.sh` and pipeline stages | Pages the source tree, calls Amazon Bedrock, and writes the spec, graphs and issues to `output/` | CLI |
| `launcher_server.py` | Local web UI. Starts runs, clones git repositories, triggers evaluation | HTTP on `127.0.0.1:8090` |
| `serve_viewer.py` | Serves the generated viewer, plus the chat, validate, translate and dependencies APIs (these call Bedrock) | HTTP on `127.0.0.1:8080` |
| `progress_server.py` | Read-only run progress dashboard | HTTP on `127.0.0.1` |
| `spec_server.py` (spec-query) | MCP server with read-only queries over a finished run; `ask_spec` calls Bedrock | MCP stdio |
| `server.py` (bedrock-distill) | MCP server exposing the pipeline's map, reduce and graph steps; calls Bedrock | MCP stdio |
| `runtimes/facts/` | .NET parser that reads the source tree; optionally runs in a build-only container | CLI / Docker |
| Amazon Bedrock | Claude models through the Converse / ConverseStream API | AWS SDK over TLS, SigV4 |
| Local embedding model | `all-mpnet-base-v2`, run locally; vectors are never sent off the machine | in-process |

## Assets

1. The user's AWS credentials and the Bedrock spend they authorize.
2. The source code being analysed. It may be proprietary to the user.
3. The generated outputs in `output/`, which contain source excerpts.
4. Other files on the user's machine.

## Trust boundaries

1. **Analysed source code → pipeline.** The code is untrusted input and can contain prompt-injection text.
2. **Model output → viewer and files.** Model output is untrusted and is rendered as HTML.
3. **Browser → local HTTP servers.** Other web pages open in the same browser can try to call localhost.
4. **MCP client (an LLM agent) → MCP servers.** Tool arguments are untrusted, since the agent may itself be steered by injected text.
5. **Machine → Amazon Bedrock.** Source excerpts leave the machine and are processed in the user's AWS account and region.
6. **Machine → git remotes and the CDN.** The launcher clones repositories; the viewer loads one pinned script from jsDelivr.

## Threats and mitigations (STRIDE)

| # | Threat | Boundary | Mitigation in the sample | Residual risk / guidance |
|---|---|---|---|---|
| T1 | **Spoofing.** Another host or user reaches the local servers and starts paid runs | 3 | Servers bind to `127.0.0.1`; the launcher refuses a non-loopback `--host` without `--allow-non-loopback`. The optional `DISTILL_ACCESS_CODE` is checked with `hmac.compare_digest`, and the session cookie is `HttpOnly`, `Secure`, `SameSite=Lax` | The access code is a single shared secret. Real multi-user use needs per-user auth (README, Hardening) |
| T2 | **Cross-site request forgery** from a malicious web page to localhost | 3 | The launcher requires `Content-Type: application/json` and rejects cross-origin `Origin` headers unless explicitly allowed with `--cors-origin` | Do not add broad CORS origins |
| T3 | **Tampering / XSS.** Model output or source text containing markup runs as script in the viewer or dashboard | 2 | Model and source text is HTML-escaped before it reaches `innerHTML`. Ids are passed through escaped `data-` attributes. `postMessage` listeners accept only the viewer iframe. The `marked` script is pinned with Subresource Integrity | Add a Content Security Policy before any shared deployment |
| T4 | **Information disclosure.** Path traversal in the static viewer server | 3 | `serve_viewer.py` resolves each request path and serves only files inside the viewer directory; anything else returns 404 | — |
| T5 | **Information disclosure.** A prompt-injected agent uses MCP tools to read arbitrary files and send them to Bedrock | 4 | `bedrock-distill` accepts file and directory arguments only inside this repository, `DISTILL_TARGET`, `DISTILL_OUTPUT` and `DISTILL_MCP_ALLOWED_ROOTS`. Paths are fully resolved (`..` and symlinks) before the check. The example MCP config registers only the read-only `spec-query` server | Register `bedrock-distill` only for automation, never for an interactive agent |
| T6 | **Prompt injection** in the analysed code alters the extracted spec | 1 | Every requirement must cite real source lines, and citations are checked against the source. The evaluation harness scores output against answer keys | Treat the spec as a draft for SME review, not as ground truth |
| T7 | **Elevation of privilege / command injection** through subprocess calls (`git`, `dotnet`, pipeline scripts) | 1, 6 | Subprocesses use argument lists with no shell. Git URLs carrying credentials are refused, as are plain `http://` and `git://` URLs | — |
| T8 | **Denial of wallet.** Runaway Bedrock spend | 3, 4, 5 | Each run prints a cost estimate first. `DISTILL_MAX_USD` refuses runs over budget and stops cleanly at the cap. The launcher has a `--max-usd-ceiling` (default USD 500) | Set an AWS Budgets alert on the account |
| T9 | **Information disclosure.** Source code sent to Bedrock | 5 | Calls use the user's own account and region over TLS. Embeddings run locally | Analyse only code you are permitted to send to Bedrock in that account and region |
| T10 | **Credential exposure** | 5, 6 | The standard AWS credential chain is used, and no credentials are stored by the sample. Credential-bearing git URLs are refused and never logged | Use temporary credentials and least-privilege `bedrock:InvokeModel*` permissions |
| T11 | **Tampering with the supply chain** (dependencies, model, CDN) | 6 | The CDN script is pinned with SRI. `pip-audit` reports no known vulnerabilities. Container images are build-only and run as a non-root user | Pin dependencies with hashes for production use |
| T12 | **Information disclosure.** `output/` contains source excerpts | — | Outputs are written only to the local output directory | Encrypt, restrict and delete outputs when no longer needed |
| T13 | **Repudiation.** No audit trail of who started a run | 3 | Each run records telemetry and a per-run status file | A shared deployment needs per-user identity and audit logging |

## Out of scope

- Hosting the servers on a network or as a multi-user service.
- The optional Crystal Reports extractor (`RptExtract.java`), which needs a separately licensed runtime.
