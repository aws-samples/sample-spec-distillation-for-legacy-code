#!/usr/bin/env python3
"""signal_classes.py — file-classification rectifier: the PRIME signal-class ontology + the
many-to-many factorization of file types onto it (SYSTEM_SPEC L4 reserves/recovery).

WHY. The coverage guard needs to know, for every file in the census, WHICH KINDS OF SIGNAL it
carries — so a reservoir with no extractor is booked and declared (never written off), and a
signal-bearing file that maps to NO known class is surfaced as an ontology-expansion candidate
(never force-fit — the commission guard). A flat extension→one-label dict cannot express this:
one file type carries MULTIPLE signal classes (.cs = interface + implementation), and unrelated
extensions share ONE class (.csproj / CMakeLists.txt / package.json all = dependency-topology).

DERIVE → EXPAND → VALIDATE (this module is the PROPOSE step — it emits a report for ratification;
it does not commit an ontology or wire downstream until a human ratifies).

  DERIVE  the PRIME (coarse-family) signal-class set from TWO authored corpora, triangulated:
    (a) still-side (top-down, authored intent): each still_specs/<still>/prompt.md declares the
        signal class it recovers (its "System Context" + block-type template vocabulary). This is
        the framework's own authored signal ontology — the strongest source.
    (b) file-type-side (bottom-up): the census extensions, each factored by the signal it
        structurally carries.
  EXPAND  file types factor as VECTORS over the prime set (many-to-many), a mapping WE OWN +
        version + extend — not a hardcoded worth-judgment. Unknown-but-signal-bearing → booked
        as an ontology-expansion candidate.
  VALIDATE convergence between the two derivations (L12): a class attested by BOTH the stills and
        the file-type factorization is confident; a still class with no file-type home, or a file
        type whose signal maps to no class, is a surfaced finding — never silently reconciled.

Read-only. Emits a proposal (stdout JSON / dict). No store writes, no ontology commit.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path


# ── DERIVE (a): still-side authored signal ontology ───────────────────────────────────────
# Parsed from still_specs/<still>/prompt.md at runtime (not hardcoded here) — we read the
# authored "System Context" + "ONE <x>, <y>, or <z> per block" template line. The still NAME is
# the coarse signal-class family; the block vocabulary is its fine grain (kept for validation,
# not the prime level — per the coarse-family decision).
_STILL_SIGNAL_STILLS = [
    "data", "domain_language", "exception", "history", "interface",
    "permission", "rule", "runtime", "state", "test", "workflow",
]


def derive_still_classes(still_specs_dir: Path) -> dict:
    """Return {still_name: {"definition": <System Context first sentence>, "vocab": [block types]}}
    parsed from the authored prompts. The still name is the candidate coarse prime class."""
    out = {}
    for still in _STILL_SIGNAL_STILLS:
        p = still_specs_dir / still / "prompt.md"
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # System Context: first sentence after the heading
        m = re.search(r"## System Context\s+(.+?)(?:\n\n|\n---)", text, re.S)
        definition = ""
        if m:
            definition = re.sub(r"\s+", " ", m.group(1)).strip()
            definition = definition.split(". ")[0][:200]
        # block vocabulary: the "ONE <a>, <b>, or <c>" template line
        vm = re.search(r"\bONE ([a-z][a-z, ]*?(?:or [a-z ]+)?)\b", text)
        vocab = []
        if vm:
            raw = vm.group(1).replace(" or ", ", ")
            vocab = [t.strip() for t in raw.split(",") if t.strip()]
        out[still] = {"definition": definition, "vocab": vocab}
    return out


# ── EXPAND: the PRIME signal-class set, on THREE ORTHOGONAL AXES ───────────────────────────
# AXIS 1 — ROLE (what the class IS / which graph it belongs to):
#   material  — substrate the rigs read FROM (not a bottled signal; e.g. implementation-logic
#               is the "wash" the stills distill business meaning out of, not a recovered fraction)
#   signal    — business meaning the DELIVERABLE carries (knowledge-graph payload)
#   structural — structural fact that SHAPES extraction (the tray layout / plate map); lives in a
#               (was "steering" — renamed to avoid collision with model-steering / STEERING_PREAMBLE;
#                this role is about code STRUCTURE that shapes grouping, not steering the LLM)
#               separate pre-phase graph, never bottled as fact, graduates only on corroboration
# AXIS 2 — MATURITY (extraction-recovery readiness; the reserves ledger, per SYSTEM_SPEC L4):
#   covered      — a working rig recovers it (PROVEN by recovery evidence, never asserted)
#   exploration  — a technique is in line-of-sight but unbuilt (a NAMED approach, not hope)
#   reserve      — signal is present (sonar) but no known extraction technique (honest floor)
# Maturity is assessed at the (file-type x class) CELL, not the class — same signal class can be
# covered for one container (.png) and reserve for another (.psd). See CELLS below.
# AXIS 3 — PROVENANCE (how the signal came to us — the two kinds of ground truth):
#   recovered — reverse-engineered from the IMPLEMENTATION (the stills' founding premise: the
#               spec that should have existed, inferred from what the system does)
#   declared  — authored PRIOR intent ingested as feedstock (JIRA/Confluence/PDF/DOCX specs,
#               epic/story exports): what the system was SAID/INTENDED to do
# The SAME signal classes carry both provenances (a Confluence page carries workflow +
# business-rule signal, just DECLARED). Declared signal is NOT folded into the recovered
# knowledge graph as a peer — it lands in a PARALLEL declared graph and is RECONCILED against the
# recovered graph (see RECONCILIATION contract below). Merging them would destroy the drift
# finding and risk asserting intent as realized fact (commission).
PRIME_SIGNAL_CLASSES = {
    # (name): (role, description)
    # ─ signal classes the stills directly attest (authored, top-down) ─
    "domain-vocabulary":    ("signal", "business terms/labels the system names things by"),
    "data-model":           ("signal", "persisted entities, relationships, invariants, schema"),
    "business-rule":        ("signal", "decisions, constraints, invariants governing behavior"),
    "workflow":             ("signal", "business processes / value-creating journeys"),
    "state-lifecycle":      ("signal", "object states, transitions, guards"),
    "authorization":        ("signal", "roles, permissions, access boundaries"),
    "interface-contract":   ("signal", "external promises: endpoints, contracts, capabilities"),
    "exception-recovery":   ("signal", "edge cases, overrides, compensations, recovery"),
    "evolution-rationale":  ("signal", "discovered requirements, decisions, migrations (WHY over time)"),
    "behavioral-reality":   ("signal", "what actually runs (observed behavior, operational rules)"),
    # ─ signal classes present in files but NO still targets today (frontier — need rigs) ─
    "config-wiring":        ("signal", "runtime wiring, DTO/contract shapes, feature flags"),
    "presentation-ui":      ("signal", "views/layout/markup the user sees"),
    "report-definition":    ("signal", "report layout + query bindings"),
    "visual-diagram":       ("signal", "diagrams/screens/models encoded as images"),
    # ─ MATERIAL: the substrate rigs read FROM (not a bottled signal — the wash) ─
    "implementation-logic": ("material", "imperative code that DOES the work (the HOW); the stills "
                                         "distill business meaning OUT of it — it is input, not payload"),
    # ─ STRUCTURAL: structural fact that shapes extraction (the tray layout); separate graph ─
    "dependency-topology":  ("structural", "build/project/package graph: what depends on what "
                                         "(declarative); the PLATE MAP — steers packing/grouping, "
                                         "never bottled as fact, graduates only on corroboration"),
}

# file-type → {prime signal classes it carries}. MANY-TO-MANY. This is the versioned mapping we
# own and extend. A file type absent here is UNKNOWN (booked, surfaced), never silently 'inert'.
FILETYPE_FACTORIZATION = {
    # imperative source — carry implementation, and (for header/decl langs) interface
    "cs":   {"implementation-logic", "interface-contract", "business-rule"},
    "vb":   {"implementation-logic", "interface-contract", "business-rule"},
    "c":    {"implementation-logic"},
    "cpp":  {"implementation-logic"},
    "h":    {"interface-contract"},               # declarations/contracts, not implementation
    "hpp":  {"interface-contract"},
    "java": {"implementation-logic", "interface-contract", "business-rule"},
    "js":   {"implementation-logic", "presentation-ui"},
    "ts":   {"implementation-logic", "interface-contract"},
    "py":   {"implementation-logic", "business-rule"},
    "rb":   {"implementation-logic"},
    "go":   {"implementation-logic", "interface-contract"},
    "php":  {"implementation-logic", "presentation-ui"},
    "vbs":  {"implementation-logic"},
    "ps1":  {"implementation-logic", "config-wiring"},
    "sql":  {"data-model", "business-rule", "implementation-logic"},
    # dependency-topology — DIFFERENT extensions, SAME class (the factorization payoff)
    "sln":    {"dependency-topology"}, "csproj": {"dependency-topology"},
    "vbproj": {"dependency-topology"}, "vcxproj": {"dependency-topology"},
    "proj":   {"dependency-topology"}, "props": {"dependency-topology"},
    "txt":    set(),  # only special-cased below for CMakeLists/etc via name, see classify()
    # schema / data-model (declarative)
    "xsd":  {"data-model"}, "wsdl": {"interface-contract", "data-model"},
    "edmx": {"data-model"}, "dbml": {"data-model"}, "avsc": {"data-model"},
    # config-wiring
    "config": {"config-wiring"}, "json": {"config-wiring", "dependency-topology"},
    "xml":  {"config-wiring"}, "yaml": {"config-wiring"}, "yml": {"config-wiring"},
    "toml": {"config-wiring", "dependency-topology"},
    # vocabulary
    "resx": {"domain-vocabulary"}, "resw": {"domain-vocabulary"},
    # report
    "rpt":  {"report-definition"}, "rdl": {"report-definition"},
    "rdlc": {"report-definition"}, "dtsx": {"report-definition", "workflow"},
    # presentation / markup
    "html": {"presentation-ui"}, "cshtml": {"presentation-ui"}, "aspx": {"presentation-ui"},
    "ascx": {"presentation-ui"}, "xaml": {"presentation-ui"}, "css": {"presentation-ui"},
    # visual
    "png": {"visual-diagram"}, "jpg": {"visual-diagram"}, "jpeg": {"visual-diagram"},
    "gif": {"visual-diagram"}, "bmp": {"visual-diagram"}, "svg": {"visual-diagram"},
    "ico": {"visual-diagram"}, "tif": {"visual-diagram"}, "tiff": {"visual-diagram"},
}

# PRIOR-SPEC feedstock containers (provenance=DECLARED). These carry the SAME signal classes as
# recovered code (a spec doc has workflow + business-rule + interface-contract signal) — the
# difference is the PROVENANCE tag, not the classes. Classified here so the factorization knows a
# .docx/.pdf/Confluence-export is signal-bearing (never 'inert'); at ingest they carry
# provenance=declared, route to the parallel declared graph, and are reconciled (not merged)
# against the recovered graph. Extraction is mostly EXPLORATION (doc→text/layout normalizer +
# interpretive pass); a SCANNED pdf is visual/OCR RESERVE. Signal classes are left broad
# (declared specs span many) — the interpretive pass refines per-block.
DECLARED_SPEC_FILETYPES = {
    "docx": {"workflow", "business-rule", "interface-contract", "evolution-rationale"},
    "doc":  {"workflow", "business-rule", "interface-contract", "evolution-rationale"},
    "pdf":  {"workflow", "business-rule", "interface-contract", "evolution-rationale"},
    "md":   {"workflow", "business-rule", "evolution-rationale"},  # exported wiki/README
    # Confluence/JIRA HTML/XML exports are container-ambiguous → handled by the rectifier's
    # interrogate step (content says "this is a JIRA export"), not a blind extension map.
}

# filenames (not extensions) that are dependency-topology regardless of extension — the
# cross-extension convergence you flagged (CMake / npm / etc.)
_TOPOLOGY_FILENAMES = {
    "cmakelists.txt": {"dependency-topology"},
    "package.json": {"dependency-topology"},
    "makefile": {"dependency-topology"},
    "pom.xml": {"dependency-topology"},
    "build.gradle": {"dependency-topology"},
    "requirements.txt": {"dependency-topology"},
    "cargo.toml": {"dependency-topology"},
    "go.mod": {"dependency-topology"},
    ".csproj": {"dependency-topology"},
}


# ── The (file-type × class) CELL: coverage is assessed HERE, not at the class level ──────────
# A cell has TWO GATES; net maturity = the WEAKER gate (and which gate is weak names the work):
#   normalization gate — can we get this container into a rig-compatible form?
#   extraction gate    — does the rig recover the signal from the normalized form?
# Each gate carries a maturity (covered/exploration/reserve) and its evidence/technique. A cell is
# `covered` only if BOTH gates are covered. `covered` requires PROOF (a passing functional test /
# recovered signal), never assertion; `exploration` requires a NAMED technique; `reserve` is the
# honest floor. Normalization is an ORDERED, signal-preserving, auditable transform chain governed
# by ONE principle: ORDER OPERATIONS TO MINIMIZE COMPOUNDING SIGNAL LOSS (e.g. format-lift before
# lossy resize/re-encode — convert-then-resize, never resize-then-convert). Loss is judged in the
# TARGET-SIGNAL coordinate system (recovery-vs-nothing baseline), not source-artifact fidelity:
# recompression/format/color-depth changes are permitted (orthogonal to the signal); cropping
# content or blurring labels unreadable is omission. Every transform is recorded (intermediary
# documented) so the loss is auditable, never hidden.
#
# The rig inventory below is illustrative of THIS build's rigs; it is the thing that grows as
# extractors/normalizers are built (maturity moves reserve→exploration→covered, memorialized).
_EXTRACTION_RIGS = {
    # modality-ish family : (extraction maturity in THIS build, rig/technique)
    "text-imperative":   ("covered", "code_graph LLM extractor (sql/cs); OTHER text langs = exploration (same rig, add template)"),
    # Two distinct techniques against topology, at different maturities — do not conflate:
    #   (a) topology GRAPH extraction (recover the actual dependency edges as graph payload).
    #       DESIGN (line-of-sight, unbuilt): .csproj/.sln/CMake/package.json/pom.xml/etc. are N
    #       dialects of one dependency-declaration DSL. Rather than N brittle per-dialect parsers
    #       (each silently drops edges it wasn't coded to see — the CODE_EXTS allowlist failure
    #       relocated), LLM-REASON each dialect to ONE canonical dependency-graph text, then
    #       process that. The LLM render is a signal-preserving NORMALIZATION whose intermediary
    #       is KEPT + auditable (order-of-ops law). It is LLM-lossy/fabrication-prone, so it is
    #       gated by BACK-VALIDATION, inline, before any downstream processing:
    #         raw project file
    #           → LLM interpret → canonical dep-graph text  (intermediary KEPT; provenance=declared)
    #           → BACK-VALIDATE vs source (bidirectional gate):
    #               • rendered edge grounds to a source token (<ProjectReference Include=...>,
    #                 add_subdirectory(...), "dependencies":{...}) → CONFIRMED (declared→recovered)
    #               • rendered edge with NO source token → COMMISSION flag (invented) — not kept
    #               • source dep token with NO rendered edge → OMISSION flag — reserve, not zeroed
    #           → deterministic graph processing consumes ONLY the confirmed/reconciled graph
    #       The surviving graph is ground truth; the delta (invented+missed) is its conserved
    #       distance-from-TRUTH (L4). Stays EXPLORATION until render rig AND validator are built+proven.
    #   (b) topology (structural role) for plate layout (group same-module files onto cohesive pages) —
    #       path-based module inference in plate_strategy.py's `dependency-cohesive`: BUILT/covered.
    #       Needs no graph; independent of (a).
    "dependency-topology": ("exploration", "(a) LLM-render N project-file dialects → ONE canonical dep-graph text, "
                                           "then BACK-VALIDATE each edge vs source token (declared→recovered; commission/omission flagged) = exploration, unbuilt; "
                                           "(b) path-based module inference for plate steering (plate_strategy.py dependency-cohesive) = covered"),
    "report-definition": ("exploration", "deterministic .rdl/.rpt XML → query-binding parser — line-of-sight"),
    "config-wiring":     ("exploration", "deterministic config/DTO parser — line-of-sight"),
    "presentation-ui":   ("exploration", "markup/DOM structure parser — line-of-sight"),
    "visual-diagram":    ("reserve", "multimodal-LLM vision pass — png/jpg exploration (rig exists), psd/exotic = reserve"),
    "domain-vocabulary": ("exploration", ".resx/.resw key-value → term parser — line-of-sight"),
    "data-model":        ("covered", "sql still + code_graph; .xsd/.edmx = exploration (schema parser)"),
}


# ── Learned-factorization overlay (self-extending by VERIFIED delta — the rectifier's memory) ──
# The rectification loop (rectify.py) persists VALIDATED file-type→classes deltas here so the next
# run knows them without re-interrogation. It is an ADDITIVE overlay, distinct from the
# hand-curated maps above: curated ALWAYS wins (the overlay only fills previously-UNKNOWN types),
# so an auto-learned delta can never silently override an authored classification. The file is
# owned + versioned + auditable + reversible (delete it → back to curated-only). Loaded lazily +
# cached; rectify.py invalidates the cache after a write.
_LEARNED_PATH_ENV = "SIGNAL_LEARNED_FACTORIZATION"
_DEFAULT_LEARNED_PATH = Path(__file__).with_name("learned_factorization.json")
_learned_cache: dict | None = None


def _learned_map() -> dict:
    """Load the learned overlay {key(ext or filename) -> {classes:[...], provenance, confidence,
    ...}}. Cached; call reset_learned_cache() after a write."""
    global _learned_cache
    if _learned_cache is not None:
        return _learned_cache
    import os
    p = Path(os.environ.get(_LEARNED_PATH_ENV, str(_DEFAULT_LEARNED_PATH)))
    data = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text())
            data = raw.get("entries", raw) if isinstance(raw, dict) else {}
        except Exception:
            data = {}
    _learned_cache = data
    return data


def reset_learned_cache() -> None:
    global _learned_cache
    _learned_cache = None


def classify(path: str) -> tuple[set, bool, str]:
    """Factor one file path into its (signal classes, is_known, provenance). provenance is
    'declared' for prior-spec feedstock (routes to the declared graph, reconciled not merged),
    else 'recovered' (the default — inferred from implementation). is_known=False means no
    factorization entry matched — an ontology-expansion candidate (booked + surfaced, NEVER
    force-fit into a class it doesn't belong to).

    Resolution order (curated ALWAYS wins over learned): topology-filename → declared-spec ext →
    curated factorization ext → LEARNED overlay (rectifier memory) → unknown."""
    name = path.replace("\\", "/").split("/")[-1].lower()
    if name in _TOPOLOGY_FILENAMES:
        return set(_TOPOLOGY_FILENAMES[name]), True, "recovered"
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in DECLARED_SPEC_FILETYPES:
        return set(DECLARED_SPEC_FILETYPES[ext]), True, "declared"
    if ext in FILETYPE_FACTORIZATION:
        classes = set(FILETYPE_FACTORIZATION[ext])
        return classes, bool(classes) or ext == "txt", "recovered"  # empty-but-known (txt) is known
    # LEARNED overlay: a rectifier-validated delta for a type the curated maps don't cover.
    learned = _learned_map()
    entry = learned.get(name) or (learned.get(ext) if ext else None)
    if entry and entry.get("classes"):
        return set(entry["classes"]), True, entry.get("provenance", "recovered")
    return set(), False, "recovered"  # unknown → ontology-expansion candidate


# ── RECTIFICATION loop (the never-seen-before file) — design contract ────────────────────────
# A file type the ontology has never seen must NEVER be silently dropped (omission) and NEVER be
# force-fit/asserted (commission). The loop (runs BEFORE plate-positioning — a novel type may
# carry structural/topology signal the plates are built from; and behind a --preflight gate so
# coverage/escalations are reported BEFORE any expensive Map-Reduce spend):
#
#   1. INTERROGATE an actual instance (technique OPEN + hybrid: pattern-match / grammar-parse /
#      detector / LLM reasoning / a convergent blend — deterministic-first, reason on residue, L5).
#      Produces an identity HYPOTHESIS, never a fact.
#   2. VALIDATE by the strongest available ground-truth mechanism (the judge ladder), and GRADE
#      (don't gate) — route WITH the resulting confidence logged + propagated (L4), don't refuse:
#        a. functional test  — identify→normalize→extract on a real instance yields coherent
#           signal? (behavioral proof; strongest) → high confidence
#        b. independent judge — a SECOND independent reasoning pass adjudicates the first;
#           convergence = confidence (L12), disagreement = flag (the judge pattern used across
#           this framework: near-dupe judge, arbitration, anti-overfit validation)
#        c. cross-technique corroboration — pattern+parse+detector+LLM agree
#      Higher-confidence validation, when later available, MONOTONICALLY upgrades the cell
#      (reserve→exploration→covered); confidence is reducible over time, never silently asserted.
#   3. ROUTE + LEARN — on sufficient confidence, process it AND persist the type's attributes into
#      the OWNED, VERSIONED routing/factorization map (self-extending by VERIFIED delta; next run
#      it is known, no re-interrogation).
#   4. ESCALATE — genuinely inconclusive/conflicting → human review, booked as EXCLUDED (surfaced,
#      quantified). LLM reasoning with no runnable confirmation enriches the escalation ticket
#      ("likely X carrying Y; ratify?"), it does not auto-route as certain.
# NORMALIZATION within (1)/(2a) is the ordered, signal-preserving, auditable transform chain
# (minimize-compounding-loss). The ontology grows ONLY by ratified/verified deltas — never by
# silent mid-run self-expansion.
#
# BUILT: the live loop is `rectify.py` (interrogators = deterministic sniff + LLM; judge ladder =
# structural coherence + independent second pass; route+learn = the learned_factorization.json
# overlay classify() consults above; --preflight = the `preflight` subcommand, dry-run without
# --commit). The prime set was ratified by the owner (2026-08-24) WITH the reservation that new
# types auto-bind by verified delta when doable — which this loop realizes.
_RECTIFICATION_CONTRACT = "interrogate(open/hybrid) → validate(judge-ladder, grade-not-gate) → route+learn | escalate"


# ── RECONCILIATION (declared ⟂ recovered) — the superposition contract ───────────────────────
# When PRIOR-SPEC feedstock (provenance=declared: JIRA/Confluence/PDF/DOCX epic-story exports) is
# ingested, it is a SECOND, independent derivation of the system's truth — declared intent — and
# its marquee value is the COMPARISON against recovered behavior, not more concepts. Declared
# signal lands in a PARALLEL declared graph (same signal classes, tagged declared) and is
# reconciled against the recovered knowledge graph:
#   CONVERGENCE (declared == recovered) → the wavefunction is already collapsed by nature (both
#      islands measured the same value); high-confidence spec content — intent was realized (L12).
#   DIVERGENCE (declared != recovered)  → a SUPERPOSITION, preserved intact, never collapsed:
#      both amplitudes recorded with their evidence (declared X + doc citation; recovered Y +
#      code evidence), neither privileged. Two distinct decoherence sub-types, booked separately:
#        declared-not-recovered — spec says X, no code exhibits it (unimplemented? stale spec?)
#        recovered-not-declared — code does Y, no spec mentions it (undocumented? shadow feature?)
# THE FRAMEWORK IS NOT THE OBSERVER. Collapsing an unmeasured divergence — an LLM/judge/reconciler
# picking declared or recovered as "the truth" — is COMMISSION of the gravest kind: it fabricates
# a measurement result the evidence does not license AND destroys the very information (the
# superposition) the product owner needed. Reconciliation DETECTS + PRESENTS the sealed box; only
# a human/business observer with the context to know whether Y is a bug (collapse→declared) or the
# spec is stale (collapse→recovered) — or both are wrong (→Z) — may open it. Preserve-the-grey at
# the spec-vs-reality tier (SYSTEM_SPEC L4). Design contract; the live reconciler is a build step.
_RECONCILIATION_CONTRACT = ("declared ⟂ recovered: converge→confidence(L12); "
                            "diverge→superposition(preserved, 2 sub-types, human-observed, "
                            "NEVER auto-collapsed — framework is not the observer)")


def validate(still_classes: dict) -> dict:
    """Cross-check the two derivations (L12 convergence). Which prime classes are attested by the
    stills (authored), which only by file-type structure (the frontier the stills don't cover)?"""
    still_families = set(still_classes.keys())
    # map still family -> prime class name (the generalization)
    STILL_TO_PRIME = {
        "domain_language": "domain-vocabulary", "data": "data-model", "rule": "business-rule",
        "workflow": "workflow", "state": "state-lifecycle", "permission": "authorization",
        "interface": "interface-contract", "exception": "exception-recovery",
        "history": "evolution-rationale", "runtime": "behavioral-reality",
        "test": "business-rule",  # test still attests protected behaviors = business rules as evidence
    }
    still_attested = {STILL_TO_PRIME[s] for s in still_families if s in STILL_TO_PRIME}
    signal_prime = {c for c, (role, _) in PRIME_SIGNAL_CLASSES.items() if role == "signal"}
    filetype_attested = set()
    for classes in FILETYPE_FACTORIZATION.values():
        filetype_attested |= classes
    return {
        "attested_by_both": sorted(still_attested & filetype_attested),
        "still_only": sorted(still_attested - filetype_attested),
        "filetype_only_frontier": sorted(filetype_attested - still_attested),
        "signal_classes_unattested_by_stills": sorted(signal_prime - still_attested),
        "by_role": {role: sorted(c for c, (r, _) in PRIME_SIGNAL_CLASSES.items() if r == role)
                    for role in ("signal", "material", "structural")},
    }


def propose(still_specs_dir: Path, census_exts: dict | None = None) -> dict:
    """Emit the PROPOSAL: derived prime set + factorization + validation, for human ratification.
    census_exts (ext->count) optional; if given, reports which census reservoirs map to which
    classes and flags unknown (ontology-expansion) reservoirs."""
    still_classes = derive_still_classes(still_specs_dir)
    val = validate(still_classes)
    proposal = {
        "axes": {"role": ["material", "signal", "structural"],
                 "maturity": ["covered", "exploration", "reserve"],
                 "provenance": ["recovered", "declared"]},
        "prime_signal_classes": {c: {"role": role, "desc": desc}
                                 for c, (role, desc) in PRIME_SIGNAL_CLASSES.items()},
        "extraction_rigs": {k: {"maturity": m, "technique": t}
                            for k, (m, t) in _EXTRACTION_RIGS.items()},
        "declared_spec_filetypes": {e: sorted(cs) for e, cs in DECLARED_SPEC_FILETYPES.items()},
        "reconciliation_contract": _RECONCILIATION_CONTRACT,
        "rectification_contract": _RECTIFICATION_CONTRACT,
        "still_derived": still_classes,
        "validation": val,
        "factorization_examples": {
            ".cs": sorted(FILETYPE_FACTORIZATION["cs"]),
            ".h": sorted(FILETYPE_FACTORIZATION["h"]),
            ".csproj / CMakeLists.txt / package.json": ["dependency-topology"],
            ".xsd": sorted(FILETYPE_FACTORIZATION["xsd"]),
            ".resx": sorted(FILETYPE_FACTORIZATION["resx"]),
            ".png": sorted(FILETYPE_FACTORIZATION["png"]),
        },
    }
    if census_exts:
        mapped, unknown = {}, {}
        for ext, cnt in census_exts.items():
            classes, known, provenance = classify("x." + ext if ext else "x")
            if known and classes:
                roles = sorted({PRIME_SIGNAL_CLASSES[c][0] for c in classes
                                if c in PRIME_SIGNAL_CLASSES})
                mapped[ext] = {"count": cnt, "classes": sorted(classes), "roles": roles,
                               "provenance": provenance}
            elif known:
                mapped[ext] = {"count": cnt, "classes": [], "roles": [], "provenance": provenance}
            else:
                unknown[ext] = cnt
        proposal["census_mapped"] = mapped
        # UNKNOWN reservoirs: signal likely present, no class yet → ontology-expansion candidates.
        # On a real run these go through the RECTIFICATION loop (interrogate→validate→route/learn
        # or escalate); here they are simply surfaced for ratification. Never dropped, never
        # force-fit (commission guard); booked in the survey (omission guard).
        proposal["census_unknown_ontology_expansion_candidates"] = dict(
            sorted(unknown.items(), key=lambda x: -x[1]))
    return proposal


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Propose the prime signal-class ontology + factorization")
    ap.add_argument("--still-specs", default="../../still_specs")
    ap.add_argument("--pages", default="", help="optional .pages dir to map the census against")
    args = ap.parse_args()
    census = None
    if args.pages:
        import code_graph as CG
        c = {}
        for _, ext, _ in CG.census_source_files(Path(args.pages)):
            c[ext] = c.get(ext, 0) + 1
        census = c
    print(json.dumps(propose(Path(args.still_specs), census), indent=2))
