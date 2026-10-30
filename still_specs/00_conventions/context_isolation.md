<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Context Isolation Rule

## Principle

Each still extraction must be performed in a fresh, isolated context window. The agent executing a still must not carry conversational history, prior still outputs, or editorial commentary from other sessions into the extraction phase.

This is a methodological requirement, not a convenience preference. It protects the independence of each still's findings and ensures that convergence across stills represents genuine independent discovery rather than echo-chamber confirmation.

---

## Rule

**Every still extraction begins in a clean context window containing only:**

1. `CONTEXT.md` — Framework understanding (what Specification Distillation is, how it works)
2. The specific still's `prompt.md` — Execution instructions for this still
3. The raw source artifacts to process — Code, schemas, tests, configs, docs from the target system
4. The `00_conventions/formal_representation.md` — If the still produces formal expressions

**Explicitly excluded from the extraction context:**

- Prior conversation about the target system
- Outputs from other stills (even completed ones)
- Commentary, analysis, or opinions about the system from any prior session
- The strategy memo's detailed still descriptions (the prompt.md is self-sufficient)
- Any human editorial guidance about "what to focus on" or "what's important"

---

## Rationale

### Why isolation matters

1. **Independent signal.** If the Rule Still and the Test Still both independently discover the same constraint, that convergence is genuine evidence of business truth. If the Rule Still saw the Test Still's output before extracting, the "convergence" is contaminated.

2. **Prevents cascade failure.** A misidentification in an early still doesn't propagate. If the Domain Language Still misnames a concept, subsequent stills won't inherit that error in their extraction phase.

3. **Honest confidence scoring.** Convergence scores are only meaningful when stills operate independently. Cross-pollination makes convergence trivially easy and therefore meaningless.

4. **Redistillation compatibility.** The Redistillation Still requires blind extraction. If initial distillation uses isolated contexts, the methodology is consistent — blind recovery is the standard, not the exception.

5. **Eliminates editorial bias.** Human conversation about "what seems important" or "what I think the system does" should not influence what the still actually finds in the artifacts.

---

## Two-Phase Execution

Each still has two distinct phases with different context rules:

### Phase 1: Extraction (Isolated)

- Fresh context window
- Only the still's prompt + raw artifacts
- No prior still outputs
- No conversational history
- Produces: raw extraction output

### Phase 2: Reconciliation (Informed)

- May occur in the same or different context window
- Receives: the raw extraction output from Phase 1
- Receives: outputs from previously-completed stills (per execution order)
- Produces: reconciliation annotations (convergence, divergence, vocabulary alignment, gaps)
- **Does NOT modify the Phase 1 extraction** — annotates only

---

## Context Window Composition

### For Phase 1 (Extraction)

**IMPORTANT:** The distillation tooling files (CONTEXT.md, prompt.md, formal_representation.md) live in the DISTILLATION workspace. The raw source artifacts live at SOURCE_ROOT — a completely separate path. Never construct a path that combines SOURCE_ROOT with distillation tooling paths (e.g., never look for `[SOURCE_ROOT]/still_specs/...`).

```
Context window contains:
├── CONTEXT.md (framework orientation) — FROM DISTILLATION WORKSPACE
├── still_specs/[still_name]/prompt.md (execution instructions) — FROM DISTILLATION WORKSPACE
├── still_specs/00_conventions/formal_representation.md (if still produces formal logic) — FROM DISTILLATION WORKSPACE
└── [Raw artifacts from target system] — FROM SOURCE_ROOT (separate path)
    ├── Source code files
    ├── Schema definitions
    ├── Test files
    ├── Configuration
    └── Documentation
```

### For Phase 2 (Reconciliation)

```
Context window contains:
├── CONTEXT.md (framework orientation)
├── This still's Phase 1 output (raw extraction)
├── Prior still outputs (per execution_order.md tier sequence)
└── Reconciliation instructions (annotate convergence/divergence, do not alter extraction)
```

---

## Enforcement

This rule is enforced procedurally:

- The human operator (or orchestration system) starts a new context window for each still
- The only documents loaded are those specified above
- If an agent detects that it has access to prior still outputs or conversation during Phase 1, it should note the purity risk and proceed with maximum extraction independence regardless

---

## Exceptions

The only valid exception: if a still's prompt.md explicitly instructs referencing a prior output during extraction (currently none do). Any such exception would need to be justified against the independence principle and documented as a known purity trade-off.
