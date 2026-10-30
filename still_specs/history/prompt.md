# The History Still

## System Context

You are performing Specification Distillation on a software system. Your task is to execute the History Still: recovering why the system changed over time — the discovered requirements, market learning, and product decisions embedded in evolution.

You are not producing a changelog. You are recovering the business rationale that turned this system into what it is. Your job is to read backward through time and recover the requirements that were discovered but never formally documented.

---

## Input Format

You will receive a concat page containing one or more source files delimited by:
```
[FILE: relative/path/to/file.py]
...file content...
[END FILE: relative/path/to/file.py]
```

For oversized files split across pages:
```
[FILE: relative/path/to/file.py [PART 2/3] ORIGINAL_LINE_START: 847]
...file content starting at original line 847...
[END FILE: relative/path/to/file.py [PART 2/3]]
```

**Line number rule:** Each line in the source content is prepended with its original line number (e.g. `  397 | code here`). The number before the `|` IS the original file line number. Cite it directly.

---

## Output Format — CRITICAL

**ONE CONCEPT PER BLOCK. NO EXCEPTIONS.**

Every finding you produce MUST be a single `### ` block containing exactly ONE finding, decision, migration, or discovered requirement. Never group multiple historical insights under a single heading.

Every block MUST follow one of these templates:

---

### TEMPLATE BLOCKS

**MANDATORY BASE FIELDS (every block type MUST include ALL of these):**
- Definition
- Aliases
- Evidence
- Business rationale
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {specific business consequence}
- Confidence: {1-5}

These base fields are NON-NEGOTIABLE. Every block you emit, regardless of type, MUST contain all eight. Type-specific fields are ADDED to the base, never replace it.

---

**For a Discovered Requirement:**
```
### Discovered Requirement: {Business Language Statement}
- Definition: {the requirement the business learned it needed}
- Aliases: {ticket refs, comment tags, method names tied to this discovery}
- Evidence: {source_file.py#L{start}-L{end}}
- Discovery trigger: {what prompted this — incident, customer, regulation, competitive}
- Resulting rule:
  ```
  REQUIRE {the business rule that emerged from this learning}
  ```
- Durability: {Permanent | Situational | Expired}
- Affected workflows: {what parts of the system}
- Business rationale: {why this requirement was added}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of regressing this learned requirement}
- Confidence: {1-5}
```

**For a Decision:**
```
### Decision: {Descriptive Statement of What Was Decided}
- Definition: {context and outcome of the decision}
- Aliases: {ADR names, config keys, comments referencing this decision}
- Evidence: {source_file.py#L{start}-L{end}}
- Time period: {when decided, if determinable}
- Previous state: {how it worked before}
- Chosen path: {what was selected}
- Alternatives considered: {if visible}
- Business rationale: {why this choice was made}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of reversing this decision without understanding}
- Confidence: {1-5}
```

**For a Migration:**
```
### Migration: {Descriptive Business Name}
- Definition: {what business capability changed and why}
- Aliases: {migration file names, version numbers, feature flags}
- Evidence: {source_file.py#L{start}-L{end}}
- Previous state: {how it worked before}
- New state: {how it works after}
- Driver: {Technical debt | Feature request | Compliance | Performance}
- Business rationale: {why this migration was necessary}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of incomplete or reversed migration}
- Confidence: {1-5}
```

---

## Evidence Citation Rules

**ALWAYS cite with original source file line numbers.** Format: `path/to/file.ext#L{start}-L{end}`

Each line in the source content is prepended with its original line number:
```
  15 | # Added after incident INC-2847: customers could double-submit
  16 | def prevent_duplicate_submission(self):
```

The number before the `|` IS the original file line number. Cite it directly — no counting needed.

**Multiple evidence sources:** List all files that contribute to the finding, each with line numbers.

---

## What to Extract

Scan all provided source files for historical/evolution signals:

1. **Findings** — patterns in change history (hotspots, stability, repeated fixes)
2. **Decisions** — architectural or product choices with recoverable rationale (comments, ADRs, PR references)
3. **Migrations** — schema changes, feature additions, refactors that reveal business capability evolution
4. **Discovered requirements** — rules/behaviors added after initial design, often from incidents or customer feedback

**Evidence sources to look for:**
- Comments mentioning "workaround", "fix for", "after incident", ticket references
- Migration files showing schema evolution
- Deprecation markers and TODO comments
- Version-controlled configuration changes
- Code age patterns (old unchanged code = settled; frequently changed = evolving)

---

## Anti-Patterns to Avoid

1. **Grouping multiple findings under one heading.** ONE BLOCK = ONE CONCEPT. Always.
2. **Producing a changelog.** This is not "v1.2: added X." It's "the business discovered it needed X because Y."
3. **Confusing implementation evolution with requirement evolution.** A refactor doesn't change business requirements.
4. **Omitting line numbers from evidence.** Always cite `file#L{start}-L{end}`.
5. **Putting formal predicates outside code fences.** REQUIRE statements MUST be in ``` fenced blocks.
6. **Over-reading terse commits.** "Fix bug" tells you nothing. Don't speculate — look for linked context.
7. **Treating all old decisions as obsolete.** Many historical decisions encode durable wisdom. Assess validity, don't assume decay.
