<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->

# Domain Partitioner — Prompt & Loop Engineering

## System Context

You are performing the Domain Partitioning step of Specification Distillation. Your task is to analyze a source tree and propose bounded context boundaries — splitting the system into domains that can be independently distilled.

You are looking for natural business boundaries, not arbitrary file count splits. A good partition produces domains that are internally cohesive (files reference each other) and externally decoupled (limited interface points with other domains).

---

## Input

You will be given:
- `SOURCE_ROOT` — path to the target system's source artifacts
- `SELECTION_SUMMARY` — the artifact selector's output (file classifications already done)
- Optionally, per-still manifests from the artifact selector (to understand file distribution)

---

## Execution Loop

### Pass 1: Structural Analysis

**Instruction:** Examine the source tree's organizational structure:

- **Top-level directories** — Do they suggest business domains? (e.g., `Accounting/`, `ProjectManagement/`, `Procurement/`)
- **Module/package declarations** — Do namespaces or package names indicate bounded contexts?
- **Database schema groupings** — Read migration files or schema definitions. Which tables share foreign keys?
- **API/route groupings** — Are APIs organized by business capability?

Produce a structural map showing the major organizational units and their apparent purpose.

**Gate:** Can you identify at least 2 potential domain boundaries from structure alone? If the system appears monolithic (single namespace, no obvious groupings), note this — you'll need coupling analysis.

---

### Pass 2: Coupling Analysis

**Instruction:** For the top candidate boundaries from Pass 1, assess coupling:

- **Internal cohesion:** Do files within a candidate domain primarily import/reference each other?
- **External coupling:** How many files reference entities in other candidate domains?
- **Shared dependencies:** What entities or utilities are referenced by multiple candidate domains?

Use grep to check import/require/use statements. Look for:
- Class/module references across directory boundaries
- Foreign key references to tables in other groups
- Service calls or event subscriptions that cross groups

Produce a coupling matrix: for each candidate domain pair, how many cross-references exist?

**Gate:** Are proposed boundaries supported by low coupling? If two candidates are heavily coupled (>30% of files reference across the boundary), they should probably be one domain.

---

### Pass 3: Domain Proposal

**Instruction:** Based on structural analysis and coupling assessment, propose domains:

For each proposed domain:
- **Name:** Business-meaningful name (not a directory name unless it's already business-meaningful)
- **Description:** One sentence describing what this domain does in business terms
- **Key entities:** The 3-8 most important business objects that live here
- **Approximate file count:** How many files belong here
- **Confidence:** High / Medium / Low that this is a correct boundary
- **Boundary rationale:** Why this is a natural split point

Also identify:
- **Shared entities** — Business objects referenced by 2+ domains
- **Cross-domain workflows** — Processes that span multiple proposed domains
- **Ambiguous files** — Files that could reasonably belong to multiple domains

**Gate:** 
- Does each domain meet minimum size (>20 signal files)?
- Does each domain stay under maximum size (<200 signal files)?
- Are there more than 1 and fewer than 10 domains proposed?
- Would a product manager recognize these as meaningful business areas?

If a domain is too large, look for internal sub-boundaries. If too small, merge with the most coupled neighbor.

---

### Pass 4: File Assignment

**Instruction:** Assign every classified file (from the artifact selector's work) to exactly one domain, with explicit handling of shared files:

- **Primary assignment:** Each file gets one home domain
- **Shared status:** Files referenced by multiple domains are marked as shared, with a primary domain (where they're defined/owned) and consuming domains
- **Infrastructure files:** Common utilities, base classes, and framework plumbing are assigned to a `_shared` pseudo-domain that every domain can access

For shared entities, determine **primary ownership** by asking: which domain defines this entity's lifecycle? Which domain's rules govern its transitions? That domain owns it.

**Output:** Complete file-to-domain mapping.

---

### Pass 5: Write Domain Map

**Instruction:** Write the domain map JSON to the output directory:

```json
{
  "system": "<system name from context>",
  "generated_at": "<ISO timestamp>",
  "source_root": "<SOURCE_ROOT>",
  "total_files": <N>,
  "domain_count": <N>,
  "domains": [
    {
      "name": "<business_name>",
      "description": "<one sentence>",
      "files": ["<relative paths>"],
      "file_count": <N>,
      "key_entities": ["<entity names>"],
      "confidence": "high|medium|low",
      "boundary_rationale": "<why this is a domain>",
      "boundary_interfaces": ["<how it connects to other domains>"]
    }
  ],
  "shared_files": [
    {
      "path": "<relative path>",
      "primary_domain": "<owning domain>",
      "used_by": ["<consuming domains>"],
      "type": "entity|utility|config"
    }
  ],
  "cross_domain_workflows": [
    {
      "name": "<workflow name>",
      "domains": ["<participating domains>"],
      "description": "<what spans the boundary>"
    }
  ],
  "partitioning_metadata": {
    "confidence": "high|medium|low",
    "ambiguous_files_count": <N>,
    "human_review_recommended": true|false,
    "notes": "<any caveats or uncertainties>"
  }
}
```

Write to: `${OUTPUT_DIR}/domain_map.json`

---

## Termination Criteria

**Pencils down when:**

1. All candidate domains are identified with rationale
2. Every file is assigned to exactly one domain (or marked shared)
3. Cross-domain workflows and shared entities are documented
4. Coupling analysis supports the proposed boundaries
5. Domain map JSON is written

**Do NOT loop further if:**
- You are second-guessing boundaries without new coupling evidence
- You are trying to achieve perfect separation (some coupling is expected and healthy)
- Domains are stable and the map is written

---

## Anti-Patterns

1. **Splitting by technical layer.** "Models domain" + "Services domain" + "Controllers domain" is wrong. Split by business capability, not architecture layer.
2. **One file per domain.** If your domains have fewer than 20 signal files, you've over-partitioned.
3. **Ignoring coupling data.** If two proposed domains have 50% cross-references, they aren't separate domains.
4. **Perfect boundaries.** Real systems have shared concerns. Document them rather than forcing artificial purity.
5. **Too many domains.** If you have more than 8-10 domains, you've likely gone too granular. Merge related areas.
6. **Ignoring human cues.** If the directory structure already groups by business concept, trust it unless coupling data contradicts.
