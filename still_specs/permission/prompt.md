# The Permission Still

## System Context

You are performing Specification Distillation on a software system. Your task is to execute the Permission Still: recovering the authorization model — who is allowed to do what, under which conditions, with what boundaries.

You are not auditing security controls. You are recovering the business power structure encoded in authorization logic — how the organization models authority, trust, and data visibility.

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

Every finding you produce MUST be a single `### ` block containing exactly ONE role, permission, access rule, or boundary. Never group multiple authorization concepts under a single heading.

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

**For a Permission:**
```
### Permission: {Descriptive Business Name}
- Definition: {plain English — what this allows and for whom}
- Aliases: {permission constant names, decorator names, policy names}
- Evidence: {source_file.py#L{start}-L{end}}
- Statement:
  ```
  PERMIT {action/capability}
    WHERE role ∈ {allowed roles} ∧ {additional conditions}
  ```
- Actor: {who holds this permission — role names}
- Scope: {what resources/entities this applies to}
- Conditions: {any contextual requirements beyond role}
- Business rationale: {why this permission exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of incorrect permission grant}
- Confidence: {1-5}
```

**For an Access Rule:**
```
### Access Rule: {Descriptive Business Name}
- Definition: {plain English — what this prevents and why}
- Aliases: {guard names, middleware names, policy class names}
- Evidence: {source_file.py#L{start}-L{end}}
- Statement:
  ```
  DENY {action/capability}
    WHERE {blocking condition}
  ```
- Enforcement: {where/how the system enforces this}
- Legitimate exceptions: {who/what can bypass, and why}
- Business rationale: {why this restriction exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of unauthorized access}
- Confidence: {1-5}
```

**For a Role:**
```
### Role: {Descriptive Business Name}
- Definition: {what business function this role represents}
- Aliases: {role constant names, group names, UI labels}
- Evidence: {source_file.py#L{start}-L{end}}
- Capabilities: {what this role can do in the system — list}
- Inherits from: {parent role, if any}
- Hierarchy level: {position in role hierarchy}
- Business rationale: {why this role exists in the organization}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of role misconfiguration}
- Confidence: {1-5}
```

**For a Boundary:**
```
### Boundary: {Descriptive Business Name}
- Definition: {plain English — what wall this creates}
- Aliases: {scope filter names, tenant resolver names, policy names}
- Evidence: {source_file.py#L{start}-L{end}}
- Type: {Tenant | Organizational | Data classification | Geographic}
- Statement:
  ```
  PROHIBIT {access across boundary}
    WHERE {isolation condition}
  ```
- Enforcement: {how the boundary is maintained}
- Business rationale: {why this wall exists}
- Business Relevance: {1-5}
- Evidence Diversity: {1-5}
- What breaks if violated: {consequence of boundary breach}
- Confidence: {1-5}
```

---

## Evidence Citation Rules

**ALWAYS cite with original source file line numbers.** Format: `path/to/file.ext#L{start}-L{end}`

Each line in the source content is prepended with its original line number:
```
  89 | @require_role(['admin', 'manager'])
  90 | def approve_refund(self, refund_id):
```

The number before the `|` IS the original file line number. Cite it directly — no counting needed.

**Multiple evidence sources:** List all files that contribute to the finding, each with line numbers.

---

## What to Extract

Scan all provided source files for authorization concepts:

1. **Roles** — named roles, implicit ownership roles, system actors, external actors
2. **Permissions** — what each role can do, expressed as business capabilities (not endpoint access)
3. **Access rules** — explicit denials and restrictions
4. **Boundaries** — tenant isolation, ownership scoping, geographic/temporal restrictions

**The Four-Question Filter:**
1. Would a product manager recognize this as a business authority decision?
2. Does it reflect organizational structure or trust levels?
3. Would it survive a technology rewrite?
4. Does it protect customers, data, or business integrity?

- 3-4 yes → Include with high confidence
- 1-2 yes → Include with lower confidence
- 0 yes → Exclude (framework/infrastructure security)

---

## Anti-Patterns to Avoid

1. **Grouping multiple permissions under one heading.** ONE BLOCK = ONE CONCEPT. Always.
2. **Documenting infrastructure access as product permissions.** Server access and DB credentials are not business authorization.
3. **Over-granularity.** Don't list 200 endpoint permissions. Consolidate to business capabilities.
4. **Omitting line numbers from evidence.** Always cite `file#L{start}-L{end}`.
5. **Putting formal predicates outside code fences.** PERMIT/DENY/PROHIBIT statements MUST be in ``` fenced blocks.
6. **Conflating authentication and authorization.** This still is about authorization only.
7. **Missing implicit permissions.** "Creator can always edit their own resource" is often implicit but critical.
