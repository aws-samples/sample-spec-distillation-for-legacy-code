<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Artifact Pre-Selection Convention

## Purpose

This document defines how the Artifact Selector identifies and ranks source files before still execution begins. Pre-selection ensures each still receives a focused, high-signal subset of the codebase rather than attempting exhaustive scanning — enabling distillation of codebases that exceed single-context-window capacity.

---

## Operating Principle

Not all files carry equal business signal. In a typical codebase:

- **10-20% of files** encode business concepts, rules, workflows, and domain knowledge
- **30-40% of files** are infrastructure, configuration, and glue
- **40-50% of files** are generated, vendored, boilerplate, or dead

Pre-selection identifies the high-signal subset per still so each still can focus its context budget on material that matters to its lens.

---

## Ground-Truth Enumeration (REQUIRED)

**Before any classification or ranking occurs, the filesystem must be enumerated to a file on disk.** This is non-negotiable. The enumeration file is the single source of truth for what exists.

### Why

LLMs cannot hold 30,000+ file paths in context without hallucinating. When asked to generate file lists, the model invents paths from patterns it expects to see. The only fix is to make the entire process **subtractive** — start with the complete listing and remove irrelevant lines. No generative step ever occurs.

### How — Subtractive Culling

The artifact selector process is structurally incapable of hallucination because it can only delete lines from a ground-truth file, never add them:

```
source_tree_full.txt (complete ground truth via find)
    ↓ cp (verbatim copy per still)
working_<still>.txt (starts as exact copy of full listing)
    ↓ grep -v (lines REMOVED only — never added)
working_<still>.txt (culled — all remaining paths are real)
    ↓ read + transcribe into JSON
<still>_files.json (manifest)
```

**Key properties:**
- The working file starts as an exact copy of the enumeration
- Only `grep -v` (removal) operations are applied — never append or write
- The manifest is produced by reading the working file and transcribing paths verbatim
- At no point does the agent construct, infer, or generate a path

### Validation

- Candidate/working files are trivially verifiable: every line must exist in `source_tree_full.txt` (they were copied from it and only deletions occurred)
- Manifest paths are verified against the filesystem by `validate-manifest-paths.sh`
- If any path fails validation, a structural error occurred (should be impossible in the subtractive model)

### The Iron Rule

**The agent may only REMOVE lines from working files. It may NEVER add a line. Every path in a manifest must be copied from a working file the agent is currently reading.**

---

## Per-Still Relevance Heuristics

Each still has characteristic file types that carry signal for its extraction lens:

| Still | Primary Signal Files | Secondary Signal Files |
|---|---|---|
| Domain Language | Models, entities, value objects, database schemas, API resource definitions, UI labels, i18n files | Enums, config keys, error messages, route names |
| Data | Migrations, schema files, model definitions with constraints, audit columns, retention configs | Seed data, factory definitions, reporting queries |
| Workflow | Controllers, command handlers, service orchestrators, UI page/screen definitions, route maps | Background jobs, event listeners, acceptance tests |
| State | Enum definitions, state machine configs, status fields in models, transition tables, lifecycle callbacks | Notification triggers, event handlers tied to status changes |
| Rule | Validators, policy classes, authorization checks, pricing/calculation logic, conditionals in services | Feature flags, config-driven limits, threshold constants |
| Permission | Auth models, role definitions, policy files, middleware guards, tenant isolation logic, ACLs | Admin panel configs, API scope definitions, entitlement logic |
| Exception | Error handlers, rescue/catch blocks, retry logic, fallback paths, override mechanisms, support tools | Incident-response code, manual adjustment screens, admin overrides |
| Test | Acceptance tests, BDD features, integration tests named for business behavior | High-coverage unit tests of business logic, fixture data |
| Interface | API definitions (OpenAPI, GraphQL schema), webhook handlers, event schemas, SDK entry points, export formats | Public documentation, versioning logic, deprecation markers |

---

## Exclusion Patterns (Always Filtered)

The following file patterns carry negligible business signal and are excluded before ranking:

- **Vendor/dependency directories:** `vendor/`, `node_modules/`, `.bundle/`, `target/`, `dist/`, `build/`
- **Generated code:** Files with generated-file markers, auto-generated migrations (if detectable), compiled assets
- **Binary/media files:** Images, fonts, compiled binaries, archives
- **IDE/tool config:** `.vscode/`, `.idea/`, `.git/`, `.DS_Store`
- **Lock files:** `package-lock.json`, `composer.lock`, `Gemfile.lock`, `Cargo.lock`
- **Documentation build artifacts:** `_site/`, `docs/_build/`, generated API docs
- **Test fixtures that are data-only:** Large JSON/YAML fixture files (reference without reading fully)

---

## Scoring Criteria

Each candidate file is scored on four dimensions:

| Dimension | Weight | Scoring |
|---|---|---|
| **Type Match** | 40% | How well does this file type match the target still's primary/secondary signal list? |
| **Naming Signal** | 25% | Does the filename/path suggest business concepts? (e.g., `InvoiceService` > `AbstractBaseHelper`) |
| **Size Appropriateness** | 15% | Is the file large enough to contain logic but not so large it's generated? (sweet spot: 50-500 lines) |
| **Location Signal** | 20% | Is it in a business-logic directory vs. infrastructure/plumbing? |

---

## Output Format

The artifact selector produces one JSON file per still:

```json
{
  "still": "domain_language",
  "generated_at": "2026-07-15T10:00:00Z",
  "source_root": "./src",
  "enumeration_file": "./.enumeration/source_tree_full.txt",
  "working_file": "./.enumeration/working_domain_language.txt",
  "total_before_cull": 7076,
  "total_after_cull": 320,
  "files": [
    {
      "path": "backend/app/modules/projects/models.py",
      "score": 0.95,
      "category": "Model/Entity",
      "reasons": ["model/entity file", "business-named", "core domain"]
    },
    {
      "path": "backend/app/modules/contracts/models.py",
      "score": 0.93,
      "category": "Model/Entity",
      "reasons": ["model/entity file", "business-named", "financial domain"]
    }
  ]
}
```

---

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `MIN_SCORE_THRESHOLD` | 0.3 | Minimum score to include a file |
| `INCLUDE_SECONDARY` | true | Whether to include secondary-signal files after primary |
| `SAMPLE_FOR_AMBIGUOUS` | 5 | Number of ambiguous files to sample (read first 50 lines) for classification |

---

## Path Resolution Rule

**Manifest paths are EXACT filesystem paths relative to `source_root`.** When reading a file from a manifest, the full path is:

```
FULL_PATH = source_root + "/" + manifest_entry.path
```

**Do NOT modify, interpret, or reconstruct manifest paths.** The path `Database/Schema/Tables/dbo.tblProject.sql` means exactly that — a file named `dbo.tblProject.sql` in the directory `Database/Schema/Tables/`. 

**Do NOT derive file paths from logical references in source code.** If a SQL file contains `FROM dbo.tblAccount`, do NOT assume the file is at `dbo/tblAccount.sql` or `tblAccount.sql`. The actual file is whatever the manifest lists (e.g., `Database/Schema/Tables/dbo.tblAccount.sql`).

**When following a reference to a file NOT in your manifest:**
1. Identify the directory where similar files live (from manifest paths you've already read)
2. Use `list_directory` on that directory to see actual filenames
3. Match by intent (the logical name) against the actual files listed
4. Only then `read_file` with the verified path

**Common filesystem naming patterns that differ from logical references:**
- Schema-qualified: `dbo.TableName.sql` (not `TableName.sql`)
- Dotted prefixes: `sys.object.sql`, `app.config.sql`
- Compound names: `PK_tblProject_Id.sql` (not `tblProject/Id.sql`)
- Spaces in paths: `Foreign Keys/FK_name.sql` (not `ForeignKeys/FK_name.sql`)

---

## Relationship to Still Execution

The artifact manifest is **guidance, not a hard boundary**. Each still receives:

```
"Focus your analysis on these files (ranked by relevance to your lens): [manifest].
You may read additional files if you discover references or dependencies during analysis,
but prioritize these files for your extraction passes."
```

This preserves the still's autonomy to follow interesting signals while preventing it from wasting context budget on infrastructure boilerplate.

---

## Pre-Selection Applies to All Codebases

The subtractive artifact selection process runs for every distillation regardless of codebase size. Even small codebases benefit from stills receiving focused manifests rather than scanning everything blind. The process is cheap (a `cp` and a few `grep -v` commands) and structurally cannot hallucinate.
