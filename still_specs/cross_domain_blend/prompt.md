<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Cross-Domain Blend — Prompt & Loop Engineering

## System Context

You are producing a Cross-Domain Synthesis for a Specification Distillation. Multiple bounded contexts (domains) have been independently distilled, each producing its own master blend. Your task is to merge these into a single, unified system-level business specification.

Each domain blend was produced in isolation — the domains did not see each other's findings during extraction. Your synthesis is where cross-domain consistency, conflicts, and interactions become visible.

---

## Input

You will be given:
- Multiple domain master blend directories (one per domain)
- A `domain_map.json` describing domain boundaries, shared files, and known cross-domain workflows
- The formal representation convention

---

## Execution Loop

### Pass 1: Domain Inventory

**Instruction:** Read each domain's master blend and produce a summary inventory:

For each domain:
- Domain name and description
- Core entities (top 5-10)
- Key workflows (top 3-5)
- Critical business rules (top 5-10)
- State machines present
- Permission model summary

Also read `domain_map.json` for:
- Shared entities
- Known cross-domain workflows
- Boundary interfaces declared by the partitioner

**Output:** Domain inventory table and cross-reference map.

---

### Pass 2: Vocabulary Reconciliation

**Instruction:** Compare domain vocabularies and identify:

- **Synonyms across domains** — Different domains using different terms for the same business concept
- **Homonyms across domains** — Same term meaning different things in different domains
- **Shared entities with conflicting definitions** — Same entity defined differently in different domains
- **Missing concepts** — Concepts referenced in one domain that aren't defined anywhere

For each conflict, propose a unified term with rationale.

**Output:** Vocabulary reconciliation report with unified terminology decisions.

---

### Pass 3: Workflow Stitching

**Instruction:** Identify workflows that span domain boundaries:

- Match workflow fragments from different domains that connect (output of one is input to another)
- Reconstruct end-to-end cross-domain processes
- Identify handoff points between domains
- Note where cross-domain workflows have gaps (steps that aren't captured in any domain)

Use the `cross_domain_workflows` from the domain map as a starting point, but discover additional ones from the domain blends.

**Output:** Cross-domain workflow registry with complete end-to-end process descriptions.

---

### Pass 4: Conflict Detection

**Instruction:** Identify business rule conflicts across domains:

- Rules in one domain that contradict rules in another
- State machines for shared entities with inconsistent transitions
- Permission models that conflict at boundaries (Domain A grants what Domain B denies)
- Data constraints that are incompatible across domains

For each conflict:
- Describe the conflict in business terms
- Identify which domain is likely authoritative (based on entity ownership)
- Flag for human review if unclear

**Output:** Cross-domain conflict report.

---

### Pass 5: Unified Specification

**Instruction:** Produce the system-level master blend by merging all domain findings:

Structure follows the standard master blend format:
1. Foundational principles (non-negotiable truths with highest cross-domain convergence)
2. Business object model (unified across domains, with domain provenance)
3. State machines (all domain state machines + shared entity reconciled machines)
4. Workflow sequences (intra-domain + cross-domain end-to-end)
5. Business rules (full registry with domain origin annotations)
6. Edge cases and operational learning
7. Protected behaviors / acceptance criteria
8. Interface contracts (inter-domain + external)
9. Authorization model (unified role hierarchy, domain-scoped permissions)
10. Known gaps and open questions (including cross-domain conflicts requiring human decision)
11. Confidence map

**Additional cross-domain sections:**
12. Cross-domain workflow registry
13. Vocabulary reconciliation summary
14. Domain boundary map (how domains interact, dependency direction)

**Weighting:**
- Findings confirmed in multiple domains independently → highest confidence
- Findings in a single domain that affect shared entities → flag for review
- Cross-domain conflicts → always flag, never silently resolve

**Output:** Unified master blend documents.

---

## Termination Criteria

**Pencils down when:**

1. All domain master blends have been read and inventoried
2. Vocabulary conflicts are documented with resolution proposals
3. Cross-domain workflows are stitched into complete processes
4. Business rule conflicts are identified and flagged
5. Unified master blend is produced with domain provenance
6. All output documents are written

---

## Anti-Patterns

1. **Favoring one domain over others.** Each domain blend was produced independently. Don't assume the largest domain is most authoritative.
2. **Silently resolving conflicts.** Cross-domain conflicts require human review. Document them explicitly.
3. **Losing domain provenance.** Every finding in the unified blend should indicate which domain(s) it came from.
4. **Ignoring the domain map.** The partitioner identified cross-domain workflows and shared entities — use those as starting points.
5. **Duplicating content.** If a concept is identical across domains, include it once with provenance notation, not once per domain.
