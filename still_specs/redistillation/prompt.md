<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# The Redistillation Still — Prompt & Loop Engineering

## System Context

You are performing Specification Distillation on a **generated or rebuilt** software system. Your task is to execute the Redistillation Still: blindly recovering business intent from implementation artifacts, then comparing the result against an original specification to measure semantic alignment.

**CRITICAL CONSTRAINT: You must perform the blind extraction FIRST, without seeing the original spec. Only after completing extraction should you receive the original spec for comparison.**

This is a two-phase process:
- **Phase A:** Blind extraction (you see only the generated system)
- **Phase B:** Semantic comparison (you see the original spec and compare)

---

## Phase A: Blind Extraction

### Input Requirements (Phase A)

You will be provided with artifacts from the generated/rebuilt system:
- Source code
- Tests
- API definitions
- Database schemas
- Documentation
- Configuration
- Any other implementation artifacts

You will NOT be provided with the original specification. You must recover intent independently.

### Phase A Execution

**Instruction:** Apply the core distillation stills to the generated system. You don't need to run all 12 stills — focus on those most likely to surface business primitives:

**Minimum extraction set:**
1. Domain Language: What business concepts does this system recognize?
2. Workflow: What processes does it support?
3. Rule: What constraints does it enforce?
4. State: What lifecycles does it model?

**Expanded extraction (if time/context allows):**
5. Permission: What authority structures exist?
6. Interface: What external promises does it make?
7. Exception: What edge cases does it handle?

For each still, apply the corresponding prompt methodology but at a condensed level. You're producing a complete-but-concise redistilled specification, not a full detailed analysis.

**Phase A Output format:**
```
## Redistilled Specification (Blind Recovery)

### Recovered Domain Concepts
- [Entity/Concept]: [Definition] — Evidence: [where found]
...

### Recovered Workflows
- [Workflow]: [Description of process] — Evidence: [where found]
...

### Recovered Rules
- [Rule]: [Business constraint statement] — Evidence: [where found]
...

### Recovered State Models
- [Entity] states: [State list and transitions] — Evidence: [where found]
...

### Recovered Permissions (if extracted)
- [Permission structure]: [Description] — Evidence: [where found]
...

### Recovered External Contracts (if extracted)
- [Interface]: [What it promises] — Evidence: [where found]
...

### Recovered Exceptions (if extracted)
- [Exception]: [Scenario and handling] — Evidence: [where found]
...
```

**Phase A Gate — Extraction Quality:**
- Have I recovered a coherent set of business primitives?
- Could this redistilled spec stand alone as a business specification?
- Have I avoided looking at or being influenced by the original spec?
- Have I noted my extraction depth and any areas where I may have missed signal?

**STOP after Phase A. Signal that blind extraction is complete before proceeding to comparison.**

---

## Phase B: Semantic Comparison

### Input Requirements (Phase B)

You now receive the **original distilled specification** for comparison.

### Phase B Execution

### Pass B1: Primitive Alignment

**Instruction:** Compare the original spec and redistilled spec at the semantic primitive level. For each primitive type:

**Domain Concepts:** Do the same business entities exist? With the same definitions?
**Workflows:** Are the same processes present? With the same steps and outcomes?
**Rules:** Are the same constraints enforced? With the same conditions?
**States:** Are the same lifecycles modeled? With the same transitions?
**Permissions:** Are the same authority structures present?
**Interfaces:** Are the same external promises made?
**Exceptions:** Are the same edge cases handled?

For each primitive in the original spec, mark:
- ✅ **Confirmed:** Same primitive independently recovered with same/equivalent meaning
- ⚠️ **Partial:** Primitive present but meaning differs or is incomplete
- ❌ **Missing:** Primitive from original not found in redistilled
- ➕ **Added:** Primitive in redistilled that wasn't in original

**Output format:**
```
## Primitive Alignment Matrix

### Domain Concepts
| Original Primitive | Redistilled Match | Alignment | Notes |
|---|---|---|---|
| [Concept A] | [Equivalent found] | ✅ Confirmed | Same meaning, different name |
| [Concept B] | [Partial match] | ⚠️ Partial | Narrower definition in implementation |
| [Concept C] | [Not found] | ❌ Missing | No evidence in generated system |

### Workflows
[Same matrix format]

### Rules
[Same matrix format]

### States
[Same matrix format]

### Added Primitives (in redistilled but not original)
| Primitive | Type | Interpretation |
|---|---|---|
| [New concept] | [Entity/Rule/Workflow] | [Emergent requirement | Implementation artifact | Over-engineering] |
```

**Gate B1 — Alignment Quality:**
- Have I compared every significant primitive from the original?
- Am I comparing meaning, not just wording?
- Have I noted primitives that exist in both but with different scope or emphasis?
- Have I captured primitives in the redistilled spec that weren't in the original?

---

### Pass B2: Divergence Analysis

**Instruction:** For each non-confirmed alignment (Partial, Missing, Added), analyze:

1. **What specifically diverges?** (Be precise about the difference)
2. **Why might it diverge?** (Classify the likely cause)
3. **What is the business impact?** (If this divergence represents a real gap)
4. **What should be done?** (Investigate, accept, fix implementation, fix spec)

Likely cause categories:
- **Implementation gap:** The builder missed or forgot this requirement
- **Translation error:** The requirement was misunderstood
- **Intentional change:** A deliberate decision was made to differ (may be valid)
- **Emergent requirement:** The implementation revealed a need not in the original
- **Implementation bias:** The builder added assumptions not in the spec
- **Abstraction mismatch:** The same intent exists but is expressed differently
- **Incomplete extraction:** My redistillation may have missed what's actually there

**Output format:**
```
## Divergence Analysis

### [Divergence description]
- Type: [Missing | Added | Changed | Narrowed | Broadened]
- Original: [What the source spec states]
- Redistilled: [What blind recovery found — or "Not recovered"]
- Precise difference: [Exactly what diverges]
- Likely cause: [From categories above]
- Confidence in cause: [High | Medium | Low]
- Business impact: [High | Medium | Low]
- Recommended action: [Investigate | Accept change | Fix implementation | Update spec | Verify extraction depth]
- Rationale: [Why this recommendation]
```

---

### Pass B3: Alignment Scoring

**Instruction:** Produce an overall alignment assessment:

```
## Alignment Score

### Quantitative
- Total original primitives: [N]
- Confirmed (✅): [N] ([X%])
- Partial (⚠️): [N] ([X%])
- Missing (❌): [N] ([X%])
- Added (➕): [N]

### By Primitive Type
| Type | Confirmed | Partial | Missing | Score |
|---|---|---|---|---|
| Domain Concepts | X/Y | X/Y | X/Y | [%] |
| Workflows | X/Y | X/Y | X/Y | [%] |
| Rules | X/Y | X/Y | X/Y | [%] |
| States | X/Y | X/Y | X/Y | [%] |
| Permissions | X/Y | X/Y | X/Y | [%] |
| Interfaces | X/Y | X/Y | X/Y | [%] |

### Weighted Assessment
[Weight high-value business primitives more heavily]
- High-value primitive alignment: [%]
- Supporting primitive alignment: [%]
- Overall weighted alignment: [%]
```

---

### Pass B4: Inspection Queue

**Instruction:** Produce a prioritized list of items requiring human investigation, ordered by (business impact × divergence confidence):

```
## Inspection Queue

### Priority 1: Investigate Immediately (High impact, clear divergence)
1. [Item]: What to check — [Brief guidance]
2. [Item]: What to check — [Brief guidance]
...

### Priority 2: Verify (Medium impact or ambiguous)
1. [Item]: What to check — [Brief guidance]
...

### Priority 3: Review When Convenient (Low impact, minor divergence)
1. [Item]: What to check — [Brief guidance]
...

### No Action Needed (Confirmed alignments of note)
- [Notable convergence]: [Why this is a positive signal]
...
```

---

### Pass B5: Meta-Assessment

**Instruction:** Assess the quality and limitations of this redistillation itself:

```
## Redistillation Quality Assessment

### Strengths of This Redistillation
- [What went well — good source variety, clear primitives emerged, etc.]

### Limitations
- [What limits confidence — shallow extraction, missing artifact types, etc.]

### Blindness Integrity
- Was the extraction truly blind? [Yes/No/Compromised — explain]

### Recommendations for Improved Redistillation
- [What additional artifacts or deeper extraction would improve confidence]
```

---

## Final Gate — Complete Validation

1. Was Phase A completed independently before Phase B began?
2. Is the comparison semantic (meaning-level) not textual (wording-level)?
3. Are both convergences and divergences reported as useful findings?
4. Is every divergence classified with likely cause and recommended action?
5. Is the inspection queue prioritized by business impact?
6. Are limitations of the redistillation honestly stated?
7. Does the output give a team clear guidance on where to focus their review?

---

## Termination Criteria

**Pencils down when:**

1. Phase A blind extraction is complete and coherent
2. Primitive-level comparison is done for all significant primitives
3. Divergences are analyzed with causes and recommendations
4. Alignment score is calculated
5. Inspection queue is prioritized
6. Meta-assessment is honest about limitations

**Do NOT loop further if:**
- You are trying to achieve 100% alignment (that's unrealistic and not the goal)
- You are debating whether wording differences constitute meaning differences (if in doubt, mark as Partial and move on)
- You are speculating about divergence causes without evidence (state "cause uncertain" and recommend investigation)
- Additional comparison passes aren't changing your alignment assessment

---

## Key Principle Reminders

**Convergence tells you where to trust.** Same primitives emerging independently = high confidence the implementation preserved intent.

**Divergence tells you where to look.** Different primitives = focused investigation, not automatic failure.

**This is not pass/fail.** Perfect alignment is impossible and unnecessary. The goal is to know WHERE the spec and implementation agree (move forward confidently) and WHERE they disagree (investigate specifically).

**Blindness is sacred.** The entire value of redistillation rests on independent recovery. If the extraction was influenced by knowledge of the original spec, the comparison is meaningless — it would only confirm that you found what you were looking for.
