<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Formal Representation Convention

## Purpose

This document establishes the standard for expressing functional logic recovered through Specification Distillation. All extracted rules, calculations, constraints, permissions, sequences, and guards must be expressed using this convention.

The goal is implementation-neutral, human-readable, mathematically precise specification of business logic — free from any programming language syntax.

---

## Core Principles

1. **No implementation language syntax.** No SQL, no Ruby, no Python, no JavaScript. Math and structured predicates only.
2. **Every variable defined.** A "Where" legend with business meaning accompanies every expression.
3. **Complex conditions factored.** Dense multi-condition logic decomposes into named sub-expressions, each independently readable and testable.
4. **Human-readable without specialized knowledge.** A business stakeholder with basic quantitative literacy can validate the expression.
5. **Stored as markdown.** Projectable to any programming language or LaTeX rendering on demand.
6. **Each atomic expression is independently testable.** If you can't test it in isolation, factor further.

---

## Representation Types

### 1. Calculations

Mathematical formulas that compute a business value.

**Format:**
```
result = expression

Where:
• result = [business definition of output]
• var1 = [business definition]
• var2 = [business definition]
```

**Piecewise (conditional calculations):**
```
result = { expression_a    where condition_1
         { expression_b    where condition_2

Where:
• result = [business definition]
• variables defined...
```

---

### 2. Constraints / Invariants

Conditions that must always be true. Things the system guarantees.

**Format:**
```
REQUIRE [predicate expression]

Where:
• var = [business definition]
```

**Examples of predicate operators:**
- `>`, `<`, `≥`, `≤`, `=`, `≠`
- `∈` (is a member of)
- `∉` (is not a member of)
- `∧` (and)
- `∨` (or)
- `¬` (not)

---

### 3. Eligibility / Composed Conditions

Boolean logic determining qualification or applicability.

**Format (factored):**
```
eligible = condition_a ∧ condition_b ∧ condition_c

Where:
• eligible = [what this qualification means]
• condition_a = [predicate with business meaning]
• condition_b = [predicate with business meaning]
• condition_c = [predicate with business meaning]
```

**Factorization rule:** If any sub-condition is itself compound (contains `∧` or `∨`), extract it as a named sub-expression with its own definition.

---

### 4. Permissions

Authorization predicates defining who may do what under which conditions.

**Format:**
```
PERMIT [action] WHERE [predicate]
DENY [action] WHERE [predicate]

Where:
• action = [business operation being authorized]
• variables in predicate defined...
```

**With actor/resource/context structure:**
```
PERMIT [action] 
  ON [resource]
  BY [actor conditions]
  WHERE [additional context conditions]
```

---

### 5. Sequences / Dependencies

Required ordering of operations.

**Format:**
```
A → before → B

Where:
• A = [business operation that must occur first]
• B = [business operation that depends on A]
```

**Chain notation:**
```
A → B → C

Where: each operation defined...
```

---

### 6. State Guards

Conditions that must be true for a state transition to be allowed.

**Format:**
```
ALLOW transition(S₁ → S₂) WHERE [predicate]

Where:
• S₁ = [source state business meaning]
• S₂ = [destination state business meaning]
• predicate variables defined...
```

---

### 7. Thresholds

Numeric boundaries that change behavior.

**Format:**
```
behavior = { outcome_a    where value > threshold
           { outcome_b    where value ≤ threshold

Where:
• value = [what is being measured]
• threshold = [boundary value and its business significance]
• outcome_a = [what happens above]
• outcome_b = [what happens below]
```

---

## Notation Reference

| Symbol | Meaning | Usage |
|---|---|---|
| `=` | Equals / defined as | Assignment or equality |
| `≠` | Not equal | Inequality |
| `>`, `<`, `≥`, `≤` | Comparison | Numeric comparisons |
| `∧` | Logical AND | All conditions must be true |
| `∨` | Logical OR | At least one condition must be true |
| `¬` | Logical NOT | Negation |
| `∈` | Member of | Set membership |
| `∉` | Not member of | Set exclusion |
| `→` | Leads to / implies / before | Sequence or implication |
| `×` | Multiplication | Arithmetic |
| `÷` or `/` | Division | Arithmetic |
| `∑` | Sum | Aggregation |
| `∀` | For all | Universal quantifier |
| `∃` | There exists | Existential quantifier |
| `{ ... }` | Set | Collection of values |
| `min()`, `max()` | Minimum, maximum | Bounded selection |
| `round(x, n)` | Round to n places | Precision specification |
| `count()` | Count of items | Aggregation |

---

## Factorization Rules

### When to Factor

Factor a compound expression into named sub-expressions when:
- It contains more than 3 conditions joined by `∧` or `∨`
- A sub-condition has independent business meaning
- A sub-condition is reused by other rules
- The full expression is not readable in a single pass

### How to Factor

1. Identify logically coherent sub-groups within the compound expression
2. Give each sub-group a business-meaningful name
3. Define each sub-group as its own expression
4. Compose the top-level expression using the named sub-groups
5. Provide the "Where" legend for all variables

### Factorization Quality Test

Each factored sub-expression should satisfy:
- It has a clear business name (not just "condition_1")
- It can be explained in one sentence to a domain expert
- It can be independently tested with specific inputs
- It could be referenced by other rules without repetition

---

## Variable Legend Requirements

Every formal expression must include a "Where" section that:

1. **Defines every variable** that appears in the expression
2. **Uses business language** — the definition should be understandable without domain expertise
3. **Specifies units** where applicable (currency, time, count, percentage)
4. **Notes the source** of the value where non-obvious (e.g., "calculated by [other rule]", "provided by customer", "system-determined")
5. **Specifies valid ranges** where relevant (e.g., "always positive", "0-100", "non-null")

---

## Projection Notes

The markdown formal representation is the **spec-of-record**. It can be projected to other formats as needed:

- **To programming language:** For engineers who prefer reading in their language of choice. The projection is a convenience, not the source of truth.
- **To LaTeX rendering:** For documentation that requires typeset mathematical notation. The markdown expression contains equivalent semantic content.
- **To test assertions:** Each atomic expression maps directly to one or more test cases with defined inputs and expected outputs.

An LLM can perform any of these projections on demand. The markdown format is chosen because it is:
- Version-controllable (plain text)
- Universally readable (no special tooling)
- Semantically equivalent to formal notation (LLMs parse both identically)
- Projectable without loss (any target format can be generated)

---

## Anti-Patterns

| Anti-Pattern | Why It's Wrong | Correct Approach |
|---|---|---|
| Using SQL syntax (`LEAST`, `COALESCE`, `:param`) | Binds to a specific database language | Use `min()`, null-handling expressed as conditions |
| Using language-specific idioms (`&&`, `\|\|`, `!`) | Binds to a programming language | Use `∧`, `∨`, `¬` |
| Unnamed conditions (`condition_1 ∧ condition_2`) | Not business-readable | Give each condition a meaningful business name |
| Monolithic expressions with 5+ conditions | Not readable in single pass | Factor into named sub-expressions |
| Missing variable legend | Assumes reader knows what variables mean | Always include "Where" section |
| Variables named after code artifacts | Carries implementation naming | Name variables for their business meaning |
| Expressing sequence with code (`call A; call B;`) | Implementation syntax | Use `A → before → B` notation |
| Natural language instead of formal expression | Ambiguous, not testable | Formalize: "discount applies if total high enough" → `discount_eligible = order.total ≥ threshold` |
