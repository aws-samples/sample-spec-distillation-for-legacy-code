<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Redistillation Still — Specification

## Identity

**Tier:** Validation
**Primary Question:** Did implementation preserve the intended business truth?
**Role in the Blend:** Serves as the validation loop — applying distillation blindly to generated/rebuilt artifacts and comparing the recovered spec against the original to measure semantic alignment.

---

## Purpose

The Redistillation Still validates whether an implementation preserves business intent. It applies the other distillation techniques blindly to generated or rebuilt system artifacts — without access to the original specification — and then compares the independently-recovered spec against the source spec.

This is the "check your work" step. It does not prove correctness in a mathematical sense, but it raises or lowers confidence that the system preserved the intended business truth.

The key constraint is **blindness**: the redistillation process must not use the originating spec as input. It must recover intent independently.

---

## Prerequisites

The Redistillation Still requires:
1. An **original distilled specification** (produced by the other stills)
2. A **generated/rebuilt system** (produced by AI-DLC or manual rebuild)
3. **Separation of concerns**: the person/system performing redistillation should not have memorized the original spec

---

## Method

### Step 1: Blind Extraction

Apply a subset of the other stills to the generated system without reference to the original spec:
- Domain Language Still → Recover vocabulary
- Workflow Still → Recover processes
- Rule Still → Recover constraints
- State Still → Recover lifecycles
- Permission Still → Recover authorization model
- Interface Still → Recover external contracts

The extraction does not need to run all 11 stills. Focus on stills where the original spec had the strongest findings.

### Step 2: Independent Assembly

Produce a redistilled specification from the blind extraction. This should be a complete, standalone document — not a comparison. It should look like a first-pass distilled spec.

### Step 3: Semantic Comparison

Compare the redistilled spec against the original spec at the semantic primitive level. Where primitives include formal expressions (rules, constraints, permissions, guards), compare at the mathematical/predicate level — not at the wording level.

**For formally expressed primitives, test equivalence:**
- Do the expressions compute the same function for all valid inputs?
- Do the predicates evaluate to the same truth value for all valid states?
- Are the variable definitions semantically aligned even if named differently?

**For each primitive type, ask:**

| Primitive | Comparison Question |
|---|---|
| Domain entities | Do the same business concepts exist? |
| Actors/roles | Are the same organizational positions recognized? |
| Business rules | Are the same constraints enforced? |
| Invariants | Are the same things always-true? |
| Workflows | Are the same processes supported? |
| Decision points | Are the same branches meaningful? |
| Exception cases | Are the same edge cases handled? |
| Obligations/permissions | Are the same authority structures present? |
| Value exchanges | Are the same transactions occurring? |
| State transitions | Are the same lifecycles meaningful? |
| External promises | Are the same contracts preserved? |
| Operational constraints | Are the same limits respected? |

### Step 4: Divergence Classification

For each difference found, classify:

| Finding | Meaning | Action |
|---|---|---|
| Same primitive, same meaning | Alignment — no action needed | Note as convergence |
| Primitive present in original, absent in redistilled | Implementation may have dropped intent | Investigate: was it implemented but not visible? Or truly missing? |
| Primitive absent in original, present in redistilled | Implementation may have added unauthorized behavior | Investigate: is this emergent requirement or implementation bias? |
| Same primitive, different meaning | Translation error or intentional refinement | Investigate: which interpretation is correct? |
| Primitive narrowed (fewer cases handled) | Implementation may be incomplete | Investigate: partial implementation or deliberate scoping? |
| Primitive broadened (more cases handled) | Implementation may have over-generalized | Investigate: beneficial expansion or accidental complexity? |

### Step 5: Confidence Scoring

Score overall alignment:
- **Semantic Alignment Score:** What percentage of original primitives are confirmed present in redistilled spec?
- **Primitive-level scores:** Per-primitive confidence (1-5)
- **High-value primitive focus:** Weight business-critical primitives higher than supporting ones

### Step 6: Inspection Queue Generation

Produce a prioritized list of areas requiring human review:
- Ordered by business impact × divergence magnitude
- Each item includes: what differs, why it might differ, and what to check

---

## Output Format

### 1. Semantic Alignment Summary

```
Overall Alignment: [X% of original primitives confirmed]
High-confidence matches: [Count]
Likely matches (need verification): [Count]
Missing primitives: [Count]
Added primitives: [Count]
Changed meanings: [Count]
```

### 2. Convergence Report

Primitives that independently re-emerged:
```
Primitive: [Name]
Type: [Entity | Rule | Workflow | State | Permission | Interface]
Original expression: [How it appeared in source spec]
Redistilled expression: [How it appeared in blind recovery]
Alignment confidence: [1-5]
```

### 3. Divergence Report

Primitives that differ:
```
Primitive: [Name]
Divergence type: [Missing | Added | Changed | Narrowed | Broadened]
Original: [What the source spec says]
Redistilled: [What blind recovery found — or "Not found"]
Likely cause: [Implementation gap | Translation error | Intentional change | Emergent requirement | Implementation bias]
Business impact: [High | Medium | Low]
Recommended action: [Investigate | Accept | Fix in implementation | Fix in spec]
```

### 4. Inspection Queue

Prioritized list of items requiring human review:
```
Priority 1 (High impact, clear divergence):
- [Item]: [Brief description of what to check]
...

Priority 2 (Medium impact or ambiguous divergence):
- [Item]: [Brief description]
...

Priority 3 (Low impact, for completeness):
- [Item]: [Brief description]
...
```

### 5. Redistillation Confidence Assessment

```
Redistillation quality factors:
- Source variety: [How many artifact types were available for blind extraction]
- Extraction depth: [How many stills were applied]
- Blindness integrity: [Was the original spec truly unseen?]
- Implementation maturity: [Is the generated system complete enough to distill?]

Overall confidence in redistillation validity: [High | Medium | Low]
Factors limiting confidence: [List]
```

---

## Impurities to Watch For

| Impurity | How It Appears | How to Filter |
|---|---|---|
| Model bias | Both distillation and redistillation using the same extraction approach may share blind spots | Use different extraction emphases or orderings if possible |
| Abstraction mismatch | Comparing at wrong granularity (too abstract hides differences, too concrete flags false differences) | Compare at the business-primitive level, not at the implementation-detail level |
| Wording fixation | Focusing on identical language rather than equivalent meaning | Always ask: do these mean the same thing? Not: do they use the same words? |
| Completeness confusion | Interpreting absence as disagreement when the redistilled system simply wasn't examined deeply enough | Note extraction depth and flag where shallow extraction may miss valid implementations |
| Blindness leakage | Redistillation influenced by knowledge of the original spec | Ensure procedural separation; flag if blindness was compromised |

---

## Validation Criteria

1. **Blindness integrity:** Redistillation was performed without access to or memory of the original spec.
2. **Semantic focus:** Comparison operates at meaning level, not wording level.
3. **Balanced assessment:** Both convergence and divergence are reported as useful findings.
4. **Actionability:** Divergences are classified with likely cause and recommended action.
5. **Prioritization:** Inspection queue is ordered by business impact.
6. **Honest confidence:** Limitations of the redistillation are explicitly stated.

---

## Key Principles

**Convergence tells you where to trust.**
When the same business primitive emerges independently from both the original distillation and blind redistillation, confidence increases that the implementation preserved intent.

**Divergence tells you where to look.**
When primitives differ, the divergence creates a focused inspection path. It does not automatically mean failure — it means investigation is warranted.

**This is not a pass/fail test.**
It is a confidence and focus mechanism. Perfect alignment is unlikely and unnecessary. Meaningful alignment on high-value primitives is the goal.
