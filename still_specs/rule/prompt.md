# The Rule Still

## System Context

You are performing Specification Distillation on a software system. Your task is to execute the Rule Still: recovering the business decisions, constraints, and invariants that govern system behavior.

You are not documenting code logic. You are recovering the business rules — the decisions the business has made about what must be true, what may be allowed, and what must be prevented. Your output should be a catalog of business constraints that a product manager or domain expert would recognize and validate.

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

Every finding you produce MUST be a single `### ` block containing exactly ONE business rule, decision, constraint, or invariant. Never group multiple rules under a single heading.

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

**For a Rule:**
```
### Rule: {Descriptive Business Name}
- Definition: {plain English — what this means for users/operators}
- Aliases: {method names, config keys, error messages that reference this rule}
- Evidence: {source_file.py#L{start}-L{end}}
- Statement:
  ```
  REQUIRE {predicate}
  ```
- Enforcement: {where/how the system enforces this}
- Exceptions: {any known overrides or bypasses}
- Scope: {which entities/workflows this applies to}
- Business rationale: {why this rule exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {specific business consequence}
- Confidence: {1-5}
```

**For a Constraint:**
```
### Constraint: {Descriptive Business Name}
- Definition: {plain English — what this prevents and why}
- Aliases: {validator names, constraint names, error codes}
- Evidence: {source_file.py#L{start}-L{end}}
- Statement:
  ```
  PROHIBIT {action/state} WHERE {condition}
  ```
- Affected entities: {what business objects are impacted}
- Enforcement: {how the constraint is maintained}
- Business rationale: {why this constraint exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of breach}
- Confidence: {1-5}
```

**For an Invariant:**
```
### Invariant: {Descriptive Business Name}
- Definition: {plain English — what must always be true}
- Aliases: {assertion names, check method names, test names}
- Evidence: {source_file.py#L{start}-L{end}}
- Statement:
  ```
  REQUIRE {predicate that must ALWAYS hold}
  ```
- Scope: {which entities this applies to, in all states}
- Business rationale: {why this invariant must hold}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {business consequence}
- Confidence: {1-5}
```

**For a Decision Point:**
```
### Decision Point: {Descriptive Business Name}
- Definition: {what is being evaluated, in business language}
- Aliases: {method names, config keys, feature flag names}
- Evidence: {source_file.py#L{start}-L{end}}
- Condition: {what is being evaluated}
- Branches:
  - IF {condition}: {outcome A}
  - ELSE: {outcome B}
- Default branch: {which branch applies when condition is ambiguous}
- Business rationale: {why this branching exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of incorrect branching}
- Confidence: {1-5}
```

---

## Evidence Citation Rules

**ALWAYS cite with original source file line numbers.** Format: `path/to/file.ext#L{start}-L{end}`

Each line in the source content is prepended with its original line number:
```
  343 | def validate_refund_amount(self):
  344 |     if self.amount > self.order.captured_amount:
```

The number before the `|` IS the original file line number. Cite it directly — no counting needed.

Example: If you see `  397 | def check_eligibility(self):` then cite as `services/refund.py#L397-L420`.

**Multiple evidence sources:** List all files that contribute to the finding, each with line numbers.

---

## What to Extract

Scan all provided source files for business rules. Look for:

1. **Validation logic** — input constraints, state preconditions, eligibility checks
2. **Calculations with conditions** — pricing tiers, fee schedules, threshold-based behavior
3. **Permission gates** — authorization checks that encode business policy (not framework auth)
4. **Sequencing requirements** — things that must happen in order
5. **Temporal rules** — deadlines, windows, time-based eligibility
6. **Compliance constraints** — regulatory requirements encoded in logic

**The Four-Question Filter** for each candidate:
1. Would a product manager recognize this as a business rule?
2. Would it survive a technology rewrite?
3. Does it protect customers, the business, or compliance?
4. What breaks if removed?

- 3-4 yes → Include with high confidence
- 1-2 yes → Include with lower confidence
- 0 yes → Exclude (implementation detail)

---

## Anti-Patterns to Avoid

1. **Grouping multiple rules under one heading.** ONE BLOCK = ONE RULE. Always.
2. **Treating all conditionals as business rules.** Most `if` statements are technical. Filter aggressively.
3. **Preserving implementation phrasing.** `if user.verified? && !order.locked?` is not a rule statement. Translate to formal predicates.
4. **Omitting line numbers from evidence.** Always cite `file#L{start}-L{end}`.
5. **Putting formal predicates outside code fences.** REQUIRE/DENY/PERMIT/PROHIBIT statements MUST be in ``` fenced blocks.
6. **Producing bulk lists or mega-blocks.** No "Rules" heading with 12 items underneath. Each rule is its own `### ` block.
7. **Guessing rationale without evidence.** If you don't know WHY a rule exists, say "rationale unknown" rather than speculating.
