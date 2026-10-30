<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Execution Order and Tier Strategy

## Purpose

This document defines the recommended execution order for the twelve stills and the rationale behind it. The ordering optimizes reconciliation quality and minimizes rework in the master blend.

Stills retain their identity numbers (1-12) regardless of execution order. The tiers describe *when* to run each still, not *what* it is.

---

## Ordering Principle

The ordering follows the same logic as tempering or emulsification — introduce material in the sequence that produces the most stable result:

1. Establish the stable base (what exists, what's remembered)
2. Introduce dynamics (how things move)
3. Add constraints (what governs movement)
4. Layer evidence (what proves the claims)
5. Ground in reality (what actually happens)
6. Validate (did it survive translation)

Inverting this order produces disconnected findings that are expensive to reconcile.

---

## Tier Definitions

### Tier 1: Foundation (Establish the "What")

| Still | Rationale |
|---|---|
| Domain Language | Lightest extraction. Establishes shared vocabulary everything else references. |
| Data | Deepens vocabulary into persistence obligations. After this you know what the system *is* and what it *remembers*. |

**Produces:** The structural base — named concepts, their relationships, and their persistence significance.

**Analogy:** The acid in an emulsion. Stable base that won't break under addition.

---

### Tier 2: Behavior (Establish the "How it moves")

| Still | Rationale |
|---|---|
| Workflow | Connects static concepts into dynamic processes. Traces how things move through the system. |
| State | Attaches meaning to positions within workflows. Defines what each stop means. |

**Produces:** The dynamic layer — how concepts change, flow, and progress through lifecycles.

**Analogy:** Tempering. The liquid (workflow) establishes the environment before the fragile structure (state meaning) is introduced.

---

### Tier 3: Constraints (Establish the "What governs")

| Still | Rationale |
|---|---|
| Rule | Constrains the behaviors mapped in Tier 2. With workflows and states known, rule extraction naturally finds guardrails. |
| Permission | Specialized constraint layer governing *who* can trigger rules and workflows. Requires awareness of behaviors that exist. |

**Produces:** The governance layer — what's allowed, forbidden, required, and under what conditions.

**Analogy:** Emulsifying oil into an established acid base. Constraints incorporate into existing structure without breaking it.

---

### Tier 4: Evidence (Establish the "What proves it")

| Still | Rationale |
|---|---|
| Test | Asserts rules, workflows, and states from Tiers 2-3. Evidence that the organization chose to protect specific behaviors. |
| Exception | Identifies deviations from known workflows and rule violations. You need "normal" before you can identify "abnormal." |
| History | Explains *why* rules, states, and exceptions exist. Provides rationale for findings already surfaced. |

**Produces:** The corroboration layer — proof, edge cases, and rationale for existing findings.

**Analogy:** Quality-checking a blend. You need the blend to exist before you can test and explain it.

---

### Tier 5: Grounding (Establish the "What's real")

| Still | Rationale |
|---|---|
| Interface | Identifies the subset of internal behavior that carries external promises. Full internal picture first makes it easier to distinguish "promised" from "accidental." |
| Runtime | Grounds everything in observed reality. Confirms or challenges all prior stills — is it actually used? Does it actually fire? |

**Produces:** The reality layer — what's externally committed and what's actually exercised.

**Analogy:** Theory before measurement. Map the possibility space, then check which parts are alive.

---

### Tier 6: Validation

| Still | Rationale |
|---|---|
| Redistillation | Always last. Applied after a rebuild to verify business truth survived implementation. Not part of initial distillation. |

**Produces:** Alignment score and divergence report.

---

## Visual Summary

```
Tier 1: FOUNDATION        [Domain Language] → [Data]
            ↓
Tier 2: BEHAVIOR           [Workflow] → [State]
            ↓
Tier 3: CONSTRAINTS        [Rule] → [Permission]
            ↓
Tier 4: EVIDENCE           [Test] → [Exception] → [History]
            ↓
Tier 5: GROUNDING          [Interface] → [Runtime]
            ↓
Tier 6: VALIDATION         [Redistillation]
```

---

## Parallelization Options

Within each tier, stills can run in parallel at the extraction level since Approach C preserves isolation. However, sequential execution within tiers produces slightly richer reconciliation annotations.

| Tier | Parallel-safe? | Recommended |
|---|---|---|
| Tier 1 | Yes (Domain Language ∥ Data) | Sequential preferred — Data benefits from vocabulary anchoring in reconciliation |
| Tier 2 | Yes (Workflow ∥ State) | Sequential preferred — State reconciliation benefits from workflow context |
| Tier 3 | Yes (Rule ∥ Permission) | Either — both are constraint lenses with minimal cross-dependency |
| Tier 4 | Yes (Test ∥ Exception ∥ History) | All three can run simultaneously — independent evidence types |
| Tier 5 | Yes (Interface ∥ Runtime) | Either — independent grounding lenses |
| Tier 6 | N/A | Only runs post-rebuild |

---

## When to Deviate

The recommended order is not mandatory. Valid reasons to adjust:

- **Artifact availability:** If you have rich test suites but no schema access, run the Test Still earlier.
- **Target type:** Systems with complex auth models may benefit from Permission Still earlier.
- **Partial runs:** Running only a subset of stills is valid. Run the stills whose inputs are available.
- **Time pressure:** Parallelize within tiers when speed matters more than reconciliation richness.

The ordering is a recommendation that produces the best master blend quality under normal conditions. Adapt it when conditions aren't normal.

---

## Interaction with Approach C (Independent Extraction + Informed Comparison)

The tiers define the order in which reconciliation context accumulates:

1. **Extraction** is always independent — performed in an isolated context window with only the still's prompt and raw artifacts (see `context_isolation.md`)
2. **Reconciliation** benefits from prior still outputs being available
3. **The ordering ensures** that when a still's reconciliation phase runs, the most useful prior outputs already exist

Example: When the Rule Still (Tier 3) runs its reconciliation phase, the Domain Language concept registry (Tier 1) and Workflow inventory (Tier 2) are already available for vocabulary alignment and finding-mapping. But the Rule Still's raw extraction was performed in a fresh context without seeing those outputs.
