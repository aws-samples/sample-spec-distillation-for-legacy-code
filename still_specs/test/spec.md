<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Test Still — Specification

## Identity

**Tier:** Evidence
**Primary Question:** What behavior was important enough to protect?
**Role in the Blend:** Provides independently-asserted behavioral claims that cross-validate findings from other stills, and reveals what the organization considers regression-critical.

---

## Purpose

The Test Still extracts the behaviors the organization chose to protect. Tests are a powerful source of latent specification because they often express expected behavior more directly than production code. They represent a bet: the organization decided this behavior matters enough to invest in verifying it continuously.

The goal is not to treat tests as perfect truth. Tests inherit team bias, under-represent some areas, and over-represent others. The goal is to use them as evidence of protected intent — and equally, to identify where protection is absent (coverage gaps as signal).

---

## Inputs

### Primary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| BDD/acceptance tests (Given/When/Then) | Business-language behavioral assertions | High |
| Integration tests | End-to-end workflow assertions | High |
| Unit tests on business logic | Rule and calculation assertions | Medium-High |
| Regression tests | Previously-broken behaviors | High |
| Test names and descriptions | Intent summaries | Medium-High |
| Fixture/factory data | Example domain instances | Medium |

### Secondary Sources

| Source Type | What to Extract | Signal Strength |
|---|---|---|
| Snapshot tests | UI structure assertions | Low-Medium |
| Performance tests | Operational expectations | Low |
| CI configuration | Test priority and gating decisions | Medium |
| Test coverage reports | Protected vs. unprotected areas | Medium-High |
| Failing test history | Previously-important regressions | Medium |

---

## Method

### Step 1: Test Inventory and Classification

Classify all tests by type and business relevance:
- **Behavioral tests (BDD/acceptance):** Highest business signal — written in domain language
- **Integration tests:** High signal — assert workflow-level behavior
- **Business-logic unit tests:** Medium-high — assert individual rules and calculations
- **Technical unit tests:** Lower signal — test implementation, not behavior
- **Snapshot/UI tests:** Low — preserve structure, not necessarily meaning

### Step 2: Behavioral Claim Extraction

Translate each business-relevant test into a behavioral claim:
- "When [precondition], if [action], then [expected outcome]"
- Remove all implementation details (mock setups, test infrastructure)
- Express in domain vocabulary

### Step 3: Grouping and Pattern Recognition

Group behavioral claims by:
- Domain concept (all claims about Orders, all claims about Refunds)
- Workflow (claims that trace a complete process)
- Rule (claims asserting the same constraint from different angles)
- Regression (claims added after bugs — identifiable by commit history or naming)

### Step 4: Coverage Gap Analysis

Identify areas where tests are notably absent or sparse:
- Complex workflows with no integration tests
- Business rules with no assertion
- State transitions with no coverage
- Exception paths with no tests

Coverage gaps are signal: either the area is simple (low risk) or it's under-protected (high risk).

### Step 5: Priority Inference

Infer what the organization considers most important based on:
- Test density (heavily-tested areas signal risk or importance)
- CI gating (tests that block deployment are considered critical)
- Regression additions (tests added after incidents reveal discovered-critical behaviors)
- Test stability (frequently-failing tests may signal brittle implementation or changing requirements)

### Step 6: Scoring

Score each behavioral claim:
- Business Relevance (1-5)
- Evidence Diversity (1-5)
- Purity (1-5: 1=impure, 5=pure business intent)
- Confidence (1-5)
- Convergence (1-5, assessed later)

---

## Output Format

### 1. Protected Behavior Catalog

```
Behavior: [Business-language behavioral claim]
Formal Assertion: [If the behavior maps to a rule/constraint, express formally per convention]
Where: [Variable legend if formal assertion provided]
Source Test: [Test file and name]
Domain Area: [Concept/workflow/rule covered]
Claim Type: [Happy path | Edge case | Regression | Calculation | Permission | State transition]
Expected Outcome: [What must be true]
Negative Case: [If also tested — what must NOT happen]
Confidence: [1-5]
Business Relevance: [1-5]
```

### 2. Behavioral Claim Groups

Claims organized by domain concept or workflow, showing complete behavioral profiles:

```
### [Domain Concept: e.g., "Checkout"]
- [Claim 1]
- [Claim 2]
...
Coverage assessment: [Well-covered | Partially covered | Sparse]
Notable gaps: [What isn't tested]
```

### 3. Regression-Critical Behaviors

Behaviors added specifically because they broke in the past:
```
Behavior: [What was protected]
Likely trigger: [What incident or bug prompted this test]
Criticality signal: [Why this was important enough to add protection]
```

### 4. Coverage Gap Report

```
Area: [Domain area or workflow]
Expected coverage: [What should be tested based on complexity/importance]
Actual coverage: [What exists]
Risk assessment: [High | Medium | Low — based on business impact of regression]
Recommendation: [What should be protected in a new system]
```

### 5. Testing Priority Map

What the organization considers most critical (inferred from test investment):
- Areas with highest test density
- Tests that gate deployment
- Tests with the longest history of maintenance

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Implementation-testing | Tests that assert internal method calls, not outcomes | Ask: does this test survive a refactor? If not, it's testing implementation. |
| Framework test boilerplate | Tests that verify framework behavior, not business behavior | Ask: is this testing our business logic or the framework itself? |
| Snapshot fragility | UI snapshots that break on any change regardless of business impact | Ask: does this assert business-meaningful structure or just pixel layout? |
| Fixture bias | Test data that encodes implementation assumptions | Ask: do fixtures represent realistic business scenarios? |
| Over-mocking | Tests that mock so much they don't test real behavior | Ask: if all mocks returned different values, would this test still assert something meaningful? |

---

## Validation Criteria

1. **Business language:** Behavioral claims are understandable without test code.
2. **Outcome focus:** Claims describe what must be true, not how it's implemented.
3. **Coverage insight:** Both protected and unprotected areas are identified.
4. **Priority signal:** What the organization considers most important is clear.
5. **Cross-still alignment:** Test-derived behaviors match rules, workflows, and states from other stills.

---

## Relationship to Other Stills

| Still | Relationship |
|---|---|
| Rule Still | Tests assert rules; test-derived rules should converge with code-derived rules |
| Workflow Still | Integration/acceptance tests often encode complete workflows |
| State Still | State transition tests assert lifecycle behavior |
| Exception Still | Exception path tests reveal protected edge cases |
| Domain Language | Test names and descriptions use domain vocabulary |
| History Still | Regression tests reveal discovered requirements |
| Redistillation Still | Test-derived behavior is a primary comparison surface |
