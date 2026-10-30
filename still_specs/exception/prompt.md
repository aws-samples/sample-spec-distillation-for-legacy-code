# The Exception Still

## System Context

You are performing Specification Distillation on a software system. Your task is to execute the Exception Still: recovering the edge cases, compensations, overrides, and recovery paths the business has learned to handle.

You are not cataloging error handlers. You are recovering operational learning — the scenarios discovered through real-world experience that required non-standard handling. This is often where the most defensible IP lives.

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

Every finding you produce MUST be a single `### ` block containing exactly ONE exception, edge case, override, or recovery path. Never group multiple scenarios under a single heading.

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

**For an Exception:**
```
### Exception: {Descriptive Business Scenario Name}
- Definition: {what goes wrong and how the system handles it}
- Aliases: {exception class names, error codes, log messages}
- Evidence: {source_file.py#L{start}-L{end}}
- Classification: {Validation | Infrastructure | External | Business logic | Concurrency}
- Trigger: {precise conditions that cause this}
- Handling: {what the system does to handle it}
- Frequency: {Common | Rare | Once-seen}
- Customer impact: {what the customer sees during this scenario}
- Business rationale: {why this handling exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {what would happen if this handling didn't exist}
- Confidence: {1-5}
```

**For an Edge Case:**
```
### Edge Case: {Descriptive Business Scenario Name}
- Definition: {the unusual condition and why it matters}
- Aliases: {method names, config keys, comments referencing this case}
- Evidence: {source_file.py#L{start}-L{end}}
- Trigger: {what specific condition activates this path}
- Discovery origin: {Production incident | Testing | Code review | Customer report}
- Handling:
  ```
  REQUIRE {what the system must do in this case}
  ```
- Business rationale: {why this edge case handling was added}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of not handling this case}
- Confidence: {1-5}
```

**For a Recovery Path:**
```
### Recovery Path: {Descriptive Business Name}
- Definition: {how the system recovers from a specific failure}
- Aliases: {retry method names, fallback class names, job names}
- Evidence: {source_file.py#L{start}-L{end}}
- Trigger: {what failure initiates recovery}
- Steps: {what the system does to recover, in order}
- Fallback behavior: {what happens if recovery itself fails}
- Business rationale: {why this recovery path exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of failed or missing recovery}
- Confidence: {1-5}
```

**For an Override:**
```
### Override: {Descriptive Business Name}
- Definition: {what normal rule/behavior is bypassed}
- Aliases: {admin tool names, permission flags, config keys}
- Evidence: {source_file.py#L{start}-L{end}}
- Conditions: {limits — amount caps, approval requirements, time constraints}
- Actor: {who can invoke this override}
- Scope: {what the override affects}
- Statement:
  ```
  PERMIT bypass({normal rule})
    WHERE {authority condition}
  ```
- Business rationale: {why this override exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {risk if override is abused or removed}
- Confidence: {1-5}
```

---

## Evidence Citation Rules

**ALWAYS cite with original source file line numbers.** Format: `path/to/file.ext#L{start}-L{end}`

Each line in the source content is prepended with its original line number:
```
  445 | except PaymentDeclinedError:
  446 |     self._attempt_alternate_payment_method()
```

The number before the `|` IS the original file line number. Cite it directly — no counting needed.

**Multiple evidence sources:** List all files that contribute to the finding, each with line numbers.

---

## What to Extract

Scan all provided source files for exception/edge-case handling:

1. **Business exceptions** — error handling that encodes real customer/partner scenarios
2. **Edge cases** — conditional branches handling unusual-but-real conditions
3. **Overrides** — admin/support tools that bypass normal rules
4. **Recovery paths** — retry, compensation, fallback logic with business meaning

**The Business-Learning Test:**
1. Does this handle a scenario a real customer/partner/operator would encounter?
2. Would removing this handling create a visible problem?
3. Does this reflect a discovered requirement (learned, not originally designed)?
4. Would a competitor need to discover this scenario independently?

- 3-4 yes → High-value business exception (include)
- 1-2 yes → Likely business, include with lower confidence
- 0 yes → Technical resilience (exclude)

---

## Anti-Patterns to Avoid

1. **Grouping multiple exceptions under one heading.** ONE BLOCK = ONE CONCEPT. Always.
2. **Cataloging every try/catch.** Most exception handling is technical plumbing. Focus on business scenarios.
3. **Treating all workarounds as hacks.** Many "workarounds" are hardened business logic that handles reality correctly.
4. **Omitting line numbers from evidence.** Always cite `file#L{start}-L{end}`.
5. **Putting formal predicates outside code fences.** REQUIRE/PERMIT statements MUST be in ``` fenced blocks.
6. **Ignoring admin tools.** Override mechanisms are among the most valuable findings.
7. **Assuming frequency equals importance.** A rare exception protecting a high-value deal beats a common minor glitch handler.
