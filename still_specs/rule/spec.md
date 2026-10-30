<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Rule Still — Specification

## Identity

**Tier:** Constraints
**Primary Question:** What decisions, constraints, and invariants govern behavior?
**Role in the Blend:** Provides the constraint layer that explains why workflows branch, states are guarded, and exceptions exist.

---

## Purpose

The Rule Still extracts the business decisions and constraints embedded in the system. It recovers what must be true, what may be allowed, what must be prevented, and under what conditions.

Business IP often hides in conditionals, validations, eligibility checks, pricing logic, permissions, limits, thresholds, and exception handling. These rules are frequently discovered over time through customer interaction, regulatory pressure, operational incidents, and market learning — and may never appear in any formal requirements document.

The goal is to recover business logic in business language, without dragging forward the technical structure in which it currently lives.

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Validation logic | Business constraints on input | High |
| Conditional branches | Decision points with business meaning | High |
| Policy/rule classes | Formalized business logic | High |
| Pricing/calculation logic | Computational business rules | High |
| Eligibility/qualification checks | Access and entitlement rules | High |
| Feature flags and configuration rules | Conditional business behavior | Medium-High |
| Database constraints | Data-level business invariants | High |
| Tests (especially edge cases) | Protected rule assertions | High |
| Error messages | Rule violation explanations | Medium-High |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Documentation (business rules docs) | Explicitly stated rules | High (if current) |
| Support playbooks | Operational rule knowledge | Medium |
| Comments explaining "why" | Developer understanding of rule rationale | Medium |
| Configuration/settings | Parameterized rules | Medium |
| Duplicate logic | Rules that outgrew their original location | Medium-High |

---

## Method

### Step 1: Decision Point Extraction

Scan code for decision points — places where behavior branches based on conditions. For each:

- What condition is evaluated?
- What happens if true vs. false?
- Is this a business decision or a technical guard?

### Step 2: Translation to Formal Representation

For each business-relevant decision point, translate to a formal expression following the Formal Representation Convention (already loaded in your context via resources — do NOT attempt to read it from the source tree):

Choose the appropriate representation type:
- **Calculations** → Mathematical formula: `result = expression`
- **Constraints** → Invariant predicate: `REQUIRE predicate`
- **Eligibility** → Factored composition: `eligible = A ∧ B ∧ C`
- **Permissions** → Authorization predicate: `PERMIT action WHERE conditions`
- **Sequences** → Dependency notation: `A → before → B`
- **State guards** → Transition predicate: `ALLOW transition WHERE guard`
- **Thresholds** → Piecewise expression: `result = { x where condition }`

Every expression must include a "Where" section defining all variables in business language.

Complex conditions (3+ conjuncts) must be factored into named sub-expressions, each independently readable and testable.

### Step 3: Rule Classification

Classify each rule by type:

| Type | Definition | Example |
|---|---|---|
| **Eligibility** | Who/what qualifies for something | "Free shipping requires order total ≥ a free-shipping threshold" |
| **Calculation** | How a value is computed | "Tax is calculated based on shipping destination, not billing address" |
| **Permission** | Who may perform an action | "Only account owners can delete payment methods" |
| **Obligation** | What must happen | "Orders must be confirmed within 24 hours or auto-cancel" |
| **Prohibition** | What must not happen | "A product cannot be added to cart if out of stock" |
| **Threshold** | Numeric boundary that changes behavior | "Orders over a fraud-review threshold require manual fraud review" |
| **Sequence** | Required ordering of operations | "Payment must be captured before shipment can be initiated" |
| **Compliance** | Regulatory or legal requirement | "Customer data must be anonymized upon account deletion request" |
| **Temporal** | Time-based constraint | "Promotional pricing expires at midnight in the customer's timezone" |
| **Operational** | System health or operational safeguard | "Maximum 3 retry attempts for failed payment capture" |

### Step 4: Evidence and Confidence Assessment

For each rule:
- Where in the code is it enforced?
- Is it enforced in more than one place? (Duplication = high business importance)
- Is it tested?
- Is it documented?
- Does it appear in error messages or user-facing copy?
- Does it appear in historical issues or incident reports?

### Step 5: Rationale Recovery

For each rule, attempt to explain *why* it exists:
- What business problem does it solve?
- What would happen if the rule were removed?
- Is it protecting the customer, the business, a partner, or an operator?
- Was it likely discovered through an incident (defensive) or designed in from the start (intentional)?

### Step 6: Conflict and Overlap Detection

Identify:
- Rules that contradict each other (possible bugs or context-dependent behavior)
- Rules that overlap (possible redundancy or evolutionary residue)
- Rules that appear to be outdated (references deprecated features, expired partnerships)
- Rules that are enforced inconsistently (applied in some paths but not others)

### Step 7: Scoring

Score each rule on:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. Business Rule Catalog

For each rule:

```
Rule: [Formal expression — math/predicate notation per convention]
Where: [Variable legend with business definitions]
Type: [Eligibility | Calculation | Permission | Obligation | Prohibition | Threshold | Sequence | Compliance | Temporal | Operational]
Trigger: [When/where this rule is evaluated]
Affected Entity/Workflow: [What it governs]
Business Rationale: [Why it exists]
Evidence: [Code location, tests, docs, error messages]
Enforcement Points: [Where in the system it's applied — note if multiple]
Exceptions: [Known overrides or bypasses]
Confidence: [1-5]
Open Questions: [Ambiguities]
```

### 2. Rule Dependency Map

Rules that interact with or depend on other rules:

```
[Rule A] must be evaluated before [Rule B] because...
[Rule C] overrides [Rule D] when [condition]...
[Rule E] and [Rule F] together form a composite business constraint...
```

### 3. Conflict Register

```
Conflict: [Description]
Rules involved: [Which rules conflict]
Context: [Under what conditions the conflict appears]
Likely explanation: [Bug, context-dependence, or evolution]
Recommendation: [How to resolve]
```

### 4. Potentially Obsolete Rules

Rules that show signs of being outdated:
- Reference deprecated entities or features
- Are tested but never triggered in runtime evidence
- Contradict current documentation or UI behavior
- Were added in response to conditions that may no longer exist

### 5. Coverage Gaps

Areas of the system where:
- Complex business logic appears to lack rule formalization
- Rules exist in code but have no test protection
- Important workflows have fewer rule constraints than expected

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Defensive programming | Null checks, type guards, format validation | Ask: does this protect business meaning or prevent crashes? |
| Framework constraints | Max upload size, request rate limits, session timeouts | Ask: is this a business SLA or a technical safeguard? |
| Over-preservation of dead rules | Logic for discontinued features still in code | Check: is this rule reachable from active workflows? |
| Implementation-specific expression | Rules tied to specific data formats or storage | Translate: express the rule in terms of what, not how |
| Duplicated rules in different forms | Same constraint in validator + DB + test | Consolidate: express once, note multiple enforcement points |

---

## Validation Criteria

1. **Business explainability:** Every rule can be explained to a product manager without code references.
2. **Rule completeness:** Major business decisions in the system are captured.
3. **Rationale coverage:** Most rules have stated or inferable business reasons.
4. **Type classification:** Rules are classified consistently by type.
5. **Evidence grounding:** Every rule cites its source evidence.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Domain Language | Rules reference concepts from the vocabulary |
| Permission Still | Permission rules are a subset; complex auth logic lives in the Permission Still |
| Workflow Still | Rules constrain and gate workflow steps |
| State Still | Rules govern state transitions (guards) |
| Exception Still | Exceptions often represent rule violation handling |
| Test Still | Tests encode rule assertions; cross-validate |
| Data Still | Data-level constraints are rules enforced at persistence layer |
| History Still | Historical changes often reveal why rules were added |
