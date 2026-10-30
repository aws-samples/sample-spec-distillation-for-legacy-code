<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Artifact Selector — Prompt & Loop Engineering

## System Context

You are performing the Artifact Selection step of Specification Distillation. Your task is to produce per-still file manifests — ranked lists of files most likely to carry business signal for each extraction lens.

**THE STRUCTURAL GUARANTEE:** This process is entirely subtractive. You start with the complete filesystem listing. You remove files that are irrelevant to each still. What remains is the manifest. You never add, construct, or generate a path. Hallucination is structurally impossible because you can only delete lines — never write new ones.

---

## Input

You will be given:
- `SOURCE_ROOT` — path to the target system's source artifacts
- `OUTPUT_DIR` — path where final manifests should be written
- `ENUMERATION_DIR` — path containing the grounded file listing (produced by enumerate-source.sh)

The enumeration directory contains:
- `source_tree_full.txt` — Complete file listing, one path per line, relative to SOURCE_ROOT (THIS IS GROUND TRUTH)
- `source_tree_by_dir.txt` — Same files grouped by directory, for structural overview
- `source_tree_stats.json` — File counts, extension breakdown, directory count

**If the enumeration files do not exist:** Run the enumeration script first:
```bash
bash ./runtimes/pipeline/enumerate-source.sh "${SOURCE_ROOT}" "${ENUMERATION_DIR}"
```

---

## Execution Loop

### Phase 1: Preparation

#### Step 1.0: Load Stats

Read `source_tree_stats.json`. Note total files, top extensions, directory count. This tells you the technology stack and scale.

#### Step 1.1: Structural Overview

Read `source_tree_by_dir.txt` in windows of 400-500 lines to understand the codebase layout. Your goal is to learn:
- What directory conventions exist
- What the technology stack is
- What broad categories of files are present

You are NOT selecting files here. You are learning the landscape so you can make good keep/throw decisions in Phase 2.

#### Step 1.2: Create Per-Still Working Copies

For each still, create a working copy of the full listing using a shell command:

```bash
cp ${ENUMERATION_DIR}/source_tree_full.txt ${ENUMERATION_DIR}/working_<still>.txt
```

Produce these files:
```
${ENUMERATION_DIR}/working_domain_language.txt
${ENUMERATION_DIR}/working_data.txt
${ENUMERATION_DIR}/working_workflow.txt
${ENUMERATION_DIR}/working_state.txt
${ENUMERATION_DIR}/working_rule.txt
${ENUMERATION_DIR}/working_permission.txt
${ENUMERATION_DIR}/working_exception.txt
${ENUMERATION_DIR}/working_test.txt
${ENUMERATION_DIR}/working_interface.txt
```

Each is an exact copy of `source_tree_full.txt` — the complete listing.

#### Step 1.3: Bulk Elimination (Universal Throws)

Before per-still analysis, remove lines that are universally irrelevant to ALL stills. Use shell commands to eliminate in bulk:

```bash
# Remove obvious non-business files from ALL working copies
for f in ${ENUMERATION_DIR}/working_*.txt; do
  grep -v -E "(\.github/|\.vscode/|\.DS_Store|node_modules/|__pycache__|\.pyc$)" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  grep -v -E "(package-lock|yarn\.lock|\.gitignore|Thumbs\.db)" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  grep -v -E "\.(png|jpg|jpeg|gif|ico|svg|webp|woff|woff2|ttf|eot|mp4)$" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
```

Adjust patterns based on what you learned in Step 1.1. If the codebase has other clearly-infrastructure directories (e.g., `deploy/`, `marketing-site/`, `docs/`), add those to the bulk elimination for stills where they're irrelevant.

---

### Phase 2: Per-Still Culling

For each still, read its working file in windows and apply keep/throw decisions. 

#### The Culling Process

For each working file:

1. **Read a window** (300-500 lines) of the working file
2. **Identify THROW patterns** — lines that are clearly irrelevant to this still's lens
3. **Apply a grep -v command** to remove those patterns from the working file
4. **Repeat** until you've scanned the full file

**CRITICAL: You are REMOVING irrelevant lines. You are NEVER adding lines.**

When in doubt, KEEP. It is better to have a slightly noisy manifest than to miss a relevant file. The downstream still can ignore irrelevant files but cannot recover files you removed.

#### Per-Still Throw Guidance

**Domain Language Still** — THROW:
- Pure test files (unless they contain domain vocabulary in names)
- Infrastructure configs (CI, Docker, deploy)
- Binary/media references
- KEEP: models, schemas, route definitions, UI components, translation files, seed data, enums

**Data Still** — THROW:
- Frontend UI components
- Test files (unless testing data constraints)
- Documentation
- CI/CD configs
- KEEP: models, schemas, migrations, seed data, config with retention/audit rules, database-related

**Workflow Still** — THROW:
- Pure data model definitions (no routing/orchestration)
- Static assets
- Infrastructure configs
- KEEP: routers, services, controllers, commands, handlers, UI pages, workflow engines, events

**State Still** — THROW:
- Files with no status/state/lifecycle signal in their path
- Static assets, infrastructure
- KEEP: models (may have status fields), events, FSM configs, state machines, lifecycle modules

**Rule Still** — THROW:
- Pure UI presentation (no validation)
- Static assets, infrastructure
- KEEP: validators, permissions, services with business logic, policies, rules, guards

**Permission Still** — THROW:
- Files with no auth/permission/role signal
- Static assets, pure data models
- KEEP: permissions, auth, roles, guards, policies, ACL, tenant isolation, middleware

**Exception Still** — THROW:
- Happy-path-only files
- Static assets, configs
- KEEP: error handlers, retry logic, fallbacks, overrides, recovery paths, exception definitions

**Test Still** — THROW:
- Non-test files (unless they're fixtures/factories for tests)
- KEEP: all test files, conftest, fixtures, factories, test data

**Interface Still** — THROW:
- Internal-only modules with no external surface
- Static assets
- KEEP: routers/API definitions, schemas, events, webhooks, integrations, SDK entry points, OpenAPI specs

---

### Phase 3: Manifest Writing

For each still, the working file IS the manifest content (the paths that survived culling). Read it and write the JSON manifest.

#### Step 3.1: Read Final Working File

Read the working file for this still. Every line in it is a verified path (it came from `source_tree_full.txt` and was never added to).

#### Step 3.2: Write JSON Manifest

Write to `${OUTPUT_DIR}/<still>_files.json`:

```json
{
  "still": "<still_name>",
  "generated_at": "<ISO timestamp>",
  "source_root": "<SOURCE_ROOT>",
  "enumeration_file": "<path to source_tree_full.txt>",
  "working_file": "<path to working_<still>.txt>",
  "total_before_cull": <original line count>,
  "total_after_cull": <final line count>,
  "files": [
    {"path": "line/from/working/file.py", "score": 0.95, "category": "Model/Entity", "reasons": ["..."]},
    ...
  ]
}
```

**CRITICAL: Every `path` value MUST be copied from the working file you are reading RIGHT NOW. You are transcribing, not generating.**

Score each file based on:
| Dimension | Weight |
|---|---|
| Category match to the still's primary signal | 40% |
| Business naming signal in path | 25% |
| Location in business-logic vs infrastructure directory | 20% |
| Size/type appropriateness | 15% |

#### Step 3.3: Write Summary

After all manifests, write `${OUTPUT_DIR}/selection_summary.json`:

```json
{
  "source_root": "<SOURCE_ROOT>",
  "enumeration_file": "<path>",
  "total_files_in_source": <N>,
  "per_still_counts": {"domain_language": <N>, "data": <N>, ...},
  "working_files": {"domain_language": "<path>", "data": "<path>", ...},
  "recommendation": "proceed" | "recommend_partitioning"
}
```

---

## Termination Criteria

**Pencils down when:**
1. All 9 working files exist (copies of full listing)
2. Bulk elimination applied
3. Per-still culling complete
4. All per-still JSON manifests written from working files
5. Summary manifest written

---

## Anti-Patterns

1. **ADDING PATHS.** You may only remove lines from working files. Never append, never construct, never generate.
2. **Removing too aggressively.** When in doubt, keep. A noisy manifest is better than a gap.
3. **Typing paths from memory.** During manifest writing, you MUST be reading from the working file. If you're typing a path and not looking at it in your current read window, STOP.
4. **Skipping the working file copy step.** Each still MUST start from a full copy. Don't try to produce candidates from scratch.
5. **Over-culling then regretting.** If you accidentally remove too much, re-copy from source_tree_full.txt and start that still's cull over.

---

## Structural Guarantee

```
source_tree_full.txt (complete ground truth)
    ↓ cp (verbatim copy)
working_<still>.txt (starts as exact copy)
    ↓ grep -v (lines removed only, never added)
working_<still>.txt (culled — all remaining lines are real)
    ↓ read + transcribe
<still>_files.json (manifest — paths copied from working file)
```

At no point in this chain does a generative step occur. The agent can only subtract from ground truth. Every path in the final manifest traces back to a line that was in `source_tree_full.txt` and survived culling.
