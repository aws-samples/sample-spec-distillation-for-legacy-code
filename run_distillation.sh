#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Run Distillation — Complete end-to-end from source
# ─────────────────────────────────────────────────────────────────────────────
# Enumerates source, builds concat pages, then runs each still via distill-worker.
# No cross-references to prior variants. Fresh from source.
#
# Environment:
#   DISTILL_TARGET   — path to source codebase (or use DISTILL_GIT_URL; the two are exclusive)
#   DISTILL_OUTPUT   — output directory for this variant
#   DISTILL_GIT_URL  — git repository to analyse instead of DISTILL_TARGET: https://, ssh://,
#                      git@host:path, file:///path or a local repository path. Cloned shallowly
#                      into ${DISTILL_OUTPUT}/.source/ (runtimes/mcp-server/source_fetch.py) with
#                      the user's own git auth (ssh-agent / credential helper). URLs carrying
#                      credentials (https://user:token@...) are refused.
#   DISTILL_GIT_REF  — branch, tag or commit SHA (default: the remote HEAD)
#   DISTILL_GIT_SUBDIR — analyse only this subfolder of the repository
#   DISTILL_GIT_MAX_MB — abort the clone past this size (default 500)
#                      Provenance (URL without credentials, ref, resolved commit, subdir) goes to
#                      source_provenance.json, run_manifest.json "source" and source_inventory.json;
#                      the commit + subdir are part of the source identity (same SHA → stages
#                      reused; new commit → everything recomputed).
#   DISTILL_ESTIMATE_ONLY — 1 = stop after the Phase 1.7 cost estimate (enumerate, page, estimate;
#                      no facts, no identity gate, no model call). Exit 0 with run_status.json =
#                      estimated, or 6 / over_estimate. Used by the launcher (launcher_server.py).
#   DISTILL_CONCURRENCY     — map concurrency (default: 80)
#   DISTILL_REDUCE_CONCURRENCY — reduce concurrency (default: 4)
#   DISTILL_STILLS   — space-separated still list (default: auto-discover from still_specs)
#   DISTILL_PAGE_TOKENS     — estimated tokens per page (chars/2, as concat-batch.sh counts them;
#                             default: 22000, roughly 600-900 numbered source lines per page).
#                             DISTILL_TOKEN_BUDGET is the older name for the same knob.
#   DISTILL_FACTS           — Phase 1.2 structural facts (.NET Roslyn/ScriptDom tool) appended to
#                             each page in the map prompt (default: 1; 0 disables). Without dotnet
#                             the phase logs a WARNING and the run continues without facts.
#   DISTILL_FACTS_MAX_FRACTION — facts block bound as a fraction of the page budget (default 0.15)
#   DISTILL_CODE_GRAPH      — code-dependency graph + arbitration, stages 2.8/2.9 (default: 1; 0 disables)
#   DISTILL_ALLOW_NO_CONTENT — 1 = continue even if every page is marker-only (default: halt, no spend)
#   DISTILL_IDENTIFIERS     — Phase 1.3 identifier evidence for abbreviated names, appended to each
#                             page in the map prompt (default: 1; 0 disables). Bounds:
#                             DISTILL_IDENTIFIERS_MAX_CHARS (4000), DISTILL_IDENTIFIERS_MAX_ENTRIES (24)
#
# Budget (runtimes/mcp-server/budget.py; spend is read from ${DISTILL_OUTPUT}/telemetry/):
#   DISTILL_MAX_USD         — run cap in USD, cumulative over the output dir (resumes included).
#                             Phase 1.7 prints a per-stage estimate and refuses to start when it is
#                             over the cap (exit 6, run_status.json=over_estimate); every LLM stage
#                             and every page-centric map/reduce call is gated on actual spend; over
#                             the cap the run stops cleanly (exit 6, run_status.json=budget_exhausted
#                             with spend, stage reached and an exact resume command; no banner).
#   DISTILL_WARN_USD        — warn (never stop) once spend or the estimate passes this amount
#   DISTILL_ALLOW_OVER_ESTIMATE — 1 = start even when the estimate is over the cap (stops at the cap)
#   DISTILL_COST_HISTORY    — space-separated earlier run dirs whose telemetry sets the unit costs
#                             (default: the table in budget.py, measured on the AP and JC runs)
#   DISTILL_MAX_CALL_TOKENS — per-call output-token bound for every Bedrock call (default 64000)
#
# Exit codes: 0 ok | 1 hard error | 3 preflight/content gate | 4 completeness barrier |
#             5 required stage failed | 6 budget (over estimate or budget exhausted)
#
# Reuse / resume (content-bound, not existence-bound):
#   Phase 0 always re-enumerates and writes .enumeration/source_inventory.json (sorted relative path,
#   size, sha256 per file + filter-rule / helper / config versions → source_identity). Every reusable
#   stage stamps "<identity> <build token>" in .stage_identity/<stage>.id; ids are CHAINED from the
#   upstream build token (pages ← source; facts ← pages; run identity records the facts hash; knowledge ← run_identity incl. prompts + raw_concat content;
#   … bottling ← blend), so a mismatch recomputes that stage and everything downstream. Stale outputs
#   go to .backup/.
#
# Outputs (predictable paths under DISTILL_OUTPUT):
#   knowledge_graph/index.json             what the graph artifacts are + counts
#   knowledge_graph/knowledge_graph.json   concept graph (NetworkX node-link)
#   knowledge_graph/code_graph.json        code-dependency graph (source_files/objects/edges/entities)
#   knowledge_graph/concept_code_bridge.json concept ↔ code links
#   stills/shared_vectors.db               vector store (MCP query_spec: run_dir=DISTILL_OUTPUT)
#   issues/issues.json, issues/issues.md   Issues & Insights report (Phase 2.98)
#   master_blend/master_blend.md, bottled_spec/bottled_spec.md, viewer/index.html
#   run_status.json                        complete | failed (+ failed required stages) |
#                                          over_estimate | budget_exhausted (+ spend, resume_command)
#   budget_estimate.json                   Phase 1.7 per-stage cost estimate and unit costs
#   identifiers/pages/page_NNN.md          Phase 1.3 identifier-evidence block per page
#   .enumeration/excluded.tsv              every filtered file with its reason
# ─────────────────────────────────────────────────────────────────────────────
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Canonical interpreter: the packaged venv python (setup.sh builds it). Every phase MUST use
# this, not bare `python3` — on a clean box (EC2 instance role, fresh setup) system python has
# none of the deps (boto3, etc.), so bare `python3` fails at import. Falls back to `python3`
# only if the venv is genuinely absent (surfaces as a clear import error, not a silent miss).
PYTHON="${SCRIPT_DIR}/runtimes/mcp-server/.venv/bin/python"
[[ -x "${PYTHON}" ]] || PYTHON="python3"

OUTPUT="${DISTILL_OUTPUT:?Set DISTILL_OUTPUT to the output directory}"
if [[ -n "${DISTILL_GIT_URL:-}" && -n "${DISTILL_TARGET:-}" ]]; then
  echo "ERROR: DISTILL_TARGET and DISTILL_GIT_URL are mutually exclusive — set one (a source directory OR a git repository), not both." >&2
  exit 1
fi
SOURCE=""   # git input: set after the fetch below (needs log/write_status)
if [[ -z "${DISTILL_GIT_URL:-}" ]]; then
  SOURCE="${DISTILL_TARGET:?Set DISTILL_TARGET to the source codebase path (or DISTILL_GIT_URL to a git repository)}"
fi
CONCURRENCY="${DISTILL_MAX_CONCURRENCY:-0}"
REDUCE_CONCURRENCY="${DISTILL_REDUCE_CONCURRENCY:-4}"
# Page size. Small, dependency-cohesive pages (~600-900 numbered lines) keep each map call focused:
# one page holding a whole small corpus is a suspected cause of low recall (every still skims it;
# 79% vs 96% for a per-file prompt on the synthetic AP sample). Large corpora simply get more pages.
TOKEN_BUDGET="${DISTILL_PAGE_TOKENS:-${DISTILL_TOKEN_BUDGET:-22000}}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-sonnet-4-6}"

# Auto-discover stills from still_specs (exclude non-extraction stills)
STILLS=(${DISTILL_STILLS:-})
if [[ ${#STILLS[@]} -eq 0 ]]; then
  for dir in still_specs/*/; do
    still_name=$(basename "$dir")
    # Skip meta/synthesis stills
    case "${still_name}" in
      00_conventions|bottling|reconciliation|master_blend|artifact_selector|domain_partitioner|cross_domain_blend|redistillation) continue ;;
    esac
    if [[ -f "${dir}/prompt.md" ]]; then
      STILLS+=("${still_name}")
    fi
  done
fi

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# Immutable backup: move instead of delete. Everything goes to .backup/{timestamp}/
BACKUP_DIR=""
backup() {
  # Usage: backup <file_or_dir> [context_label]
  local src="$1"
  local label="${2:-}"
  if [[ ! -e "${src}" ]]; then return 0; fi
  if [[ -z "${BACKUP_DIR}" ]]; then
    BACKUP_DIR="${OUTPUT}/.backup/$(date '+%Y%m%d_%H%M%S')"
    mkdir -p "${BACKUP_DIR}"
  fi
  local dest="${BACKUP_DIR}/${label:+${label}/}$(basename "${src}")"
  mkdir -p "$(dirname "${dest}")"
  mv "${src}" "${dest}"
  log "  [backup] $(basename "${src}") → .backup/"
}

# ── Stage identity (content-bound reuse) ─────────────────────────────────────
# A stage's outputs are reused ONLY when the identity it was stamped with equals the identity
# computed now. Ids chain (each includes its upstream id), so any upstream change cascades.
MCP_DIR="${SCRIPT_DIR}/runtimes/mcp-server"
SID_PY="${MCP_DIR}/source_inventory.py"
CONTENT_GUARD_PY="${MCP_DIR}/content_guard.py"
STAGE_DIR="${OUTPUT}/.stage_identity"
# A stamp is "<stage id> <build token>". The token changes every time the stage is (re)built, and
# downstream ids chain from the upstream TOKEN (stage_token), so recomputing a stage always
# recomputes everything after it, while reusing it keeps the token (and downstream reuse) stable.
stage_id()    { "${PYTHON}" "${SID_PY}" stage-id "$@"; }
stage_fresh() { [[ -f "${STAGE_DIR}/$1.id" ]] && [[ "$(cut -d' ' -f1 "${STAGE_DIR}/$1.id")" == "$2" ]]; }
stage_stamp() { mkdir -p "${STAGE_DIR}"; printf '%s %s-%s\n' "$2" "$(date +%s)" "${RANDOM}${RANDOM}" > "${STAGE_DIR}/$1.id"; }
stage_clear() { rm -f "${STAGE_DIR}/$1.id"; }
stage_token() { if [[ -f "${STAGE_DIR}/$1.id" ]]; then tr ' ' ':' < "${STAGE_DIR}/$1.id"; else echo "unbuilt:$1"; fi; }

# Required-stage failures are collected (so later independent stages still run and report), then
# fail the run at the end: no "Distillation complete" banner, non-zero exit, run_status.json=failed.
REQUIRED_FAILURES=()
write_status() {
  local status="$1"; shift
  "${PYTHON}" - "${OUTPUT}/run_status.json" "${status}" "$@" <<'PYEOF' || true
import json, sys, datetime
path, status, *failed = sys.argv[1:]
json.dump({"status": status, "failed_stages": failed,
           "finished": datetime.datetime.now(datetime.timezone.utc).isoformat()},
          open(path, "w"), indent=2)
PYEOF
}
trap 'rc=$?; log "Distillation FAILED (rc=${rc}) at line ${LINENO}: ${BASH_COMMAND}"; write_status failed "line ${LINENO}: ${BASH_COMMAND}"' ERR

# ── Budget guard (DISTILL_MAX_USD) ───────────────────────────────────────────────────────────
# runtimes/mcp-server/budget.py: pre-run estimate (Phase 1.7) + a gate before every LLM stage that
# compares actual telemetry spend + the stage's estimate with the cap. Over the cap the run stops
# cleanly: run_status.json = budget_exhausted (spend, stage reached, exact resume command), exit 6,
# no completion banner. The page-centric map and reduce loops also guard each call in-process.
BUDGET_PY="${SCRIPT_DIR}/runtimes/mcp-server/budget.py"
EXIT_BUDGET=6
budget_gate() {  # budget_gate <stage key>  (no-op without DISTILL_MAX_USD)
  [[ -n "${DISTILL_MAX_USD:-}" ]] || return 0
  local brc=0
  "${PYTHON}" "${BUDGET_PY}" gate --output "${OUTPUT}" --stage "$1" || brc=$?
  if [[ "${brc}" -eq "${EXIT_BUDGET}" ]]; then
    log "Budget guard: stopped before stage '$1' (see ${OUTPUT}/run_status.json)."
    exit "${EXIT_BUDGET}"
  elif [[ "${brc}" -ne 0 ]]; then
    log "Budget guard: ERROR (rc=${brc}) evaluating the gate for '$1' — halting (fail closed)."
    write_status failed "budget gate error: $1"
    exit 1
  fi
}

# ── Git input (DISTILL_GIT_URL) ──────────────────────────────────────────────────────────────
# Shallow clone into ${OUTPUT}/.source/ (reused when it already holds the resolved commit), then
# analyse it (or DISTILL_GIT_SUBDIR inside it) as the source. source_fetch.py never prints the raw
# URL and refuses URLs that carry credentials. The resolved commit + subdir enter the source
# identity (Phase 0), and DISTILL_GIT_COMMIT pins the commit in any budget resume command.
GIT_PROVENANCE=""
GIT_ID_CONFIG=()
if [[ -n "${DISTILL_GIT_URL:-}" ]]; then
  mkdir -p "${OUTPUT}"
  log "Source: fetching git repository → ${OUTPUT}/.source (ref: ${DISTILL_GIT_REF:-<remote HEAD>}${DISTILL_GIT_SUBDIR:+, subdir: ${DISTILL_GIT_SUBDIR}})"
  FETCH_RC=0
  FETCH_ERR="${OUTPUT}/.source_fetch.log"
  FETCH_JSON=$("${PYTHON}" "${MCP_DIR}/source_fetch.py" fetch --url "${DISTILL_GIT_URL}" \
    --ref "${DISTILL_GIT_REF:-}" --requested-ref "${DISTILL_GIT_REF_REQUESTED:-${DISTILL_GIT_REF:-}}" \
    --subdir "${DISTILL_GIT_SUBDIR:-}" --dest "${OUTPUT}/.source" --max-mb "${DISTILL_GIT_MAX_MB:-500}" \
    --provenance-out "${OUTPUT}/source_provenance.json" 2>"${FETCH_ERR}") || FETCH_RC=$?
  sed 's/^/  /' "${FETCH_ERR}" >&2 || true
  if [[ "${FETCH_RC}" -ne 0 ]]; then
    FETCH_MSG=$(grep -m1 'source_fetch: ERROR' "${FETCH_ERR}" | sed 's/^source_fetch: ERROR //' || true)
    log "Source: ERROR the git fetch failed — ${FETCH_MSG:-see ${FETCH_ERR}}"
    write_status failed "source fetch: ${FETCH_MSG:-git fetch failed}"
    exit 1
  fi
  GIT_PROVENANCE="${OUTPUT}/source_provenance.json"
  SOURCE=$("${PYTHON}" -c 'import json,sys; print(json.loads(sys.argv[1])["target"])' "${FETCH_JSON}")
  export DISTILL_GIT_COMMIT
  DISTILL_GIT_COMMIT=$("${PYTHON}" -c 'import json,sys; print(json.loads(sys.argv[1])["commit"])' "${FETCH_JSON}")
  GIT_ID_CONFIG=("git_commit=${DISTILL_GIT_COMMIT}" "git_subdir=${DISTILL_GIT_SUBDIR:-}")
  if [[ -z "${DISTILL_TARGET_NAME:-}" ]]; then
    DISTILL_TARGET_NAME=$("${PYTHON}" -c 'import json,sys,re; d=json.loads(sys.argv[1]); n=(d.get("subdir") or "").rstrip("/").split("/")[-1] or re.sub(r"\.git$","",d["url"].rstrip("/").replace(":","/").split("/")[-1]); print(n or "source")' "${FETCH_JSON}")
  fi
  log "Source: commit ${DISTILL_GIT_COMMIT} → analysing ${SOURCE}"
fi
# Record where the source came from in run_manifest.json ("source"; merged, run_identity untouched).
"${PYTHON}" - "${OUTPUT}/run_manifest.json" "${SOURCE}" "${GIT_PROVENANCE}" <<'PYEOF' || true
import json, os, sys
manifest, source, prov = sys.argv[1:4]
data = {}
if os.path.exists(manifest):
    try:
        data = json.load(open(manifest))
    except Exception:
        sys.exit(0)  # never clobber a manifest we cannot parse (the identity gate reports it)
if prov:
    p = json.load(open(prov))
    data["source"] = {k: p.get(k) for k in ("type", "url", "ref_requested", "ref_pinned", "ref_resolved",
                                            "commit", "subdir", "fetched") if k in p}
else:
    data["source"] = {"type": "path", "path": os.path.abspath(source)}
# GitHub blob base for the launcher's /validate evidence links (set via DISTILL_SOURCE_REPO_URL);
# recorded for both git and path runs. The launcher only sets the env var — the shell is the sole
# writer of run_manifest.json.
rp = os.environ.get("DISTILL_SOURCE_REPO_URL")
if rp:
    data["source"]["repo_url"] = rp
os.makedirs(os.path.dirname(os.path.abspath(manifest)), exist_ok=True)
json.dump(data, open(manifest, "w"), indent=2)
PYEOF

ALL_STILLS_CSV=$(IFS=,; echo "${STILLS[*]}")
TARGET_NAME="${DISTILL_TARGET_NAME:-$(basename "${SOURCE}")}"

log "═══════════════════════════════════════════════════════════════"
log "  Specification Distillation (MCP)"
log "  Version: $(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo 'unknown')"
log "  Source: ${SOURCE}"
log "  Output: ${OUTPUT}"
log "  Stills: ${#STILLS[@]} (${STILLS[*]})"
log "  Concurrency: map=${CONCURRENCY:-auto(AIMD)}, reduce=${REDUCE_CONCURRENCY}"
log "  Token budget: ${TOKEN_BUDGET}"
log "  Model: ${BEDROCK_MODEL_ID}"
log "═══════════════════════════════════════════════════════════════"
echo ""
echo "  To monitor progress (run in a separate terminal):"
echo ""
# DISTILL_OUTPUT may be absolute or relative to this script's directory (we cd'd there above).
case "${OUTPUT}" in
  /*) MONITOR_OUTPUT="${OUTPUT}" ;;
  *)  MONITOR_OUTPUT="${SCRIPT_DIR}/${OUTPUT#./}" ;;
esac
echo "    cd ${SCRIPT_DIR}/runtimes/mcp-server && \\"
echo "    source .venv/bin/activate && \\"
echo "    python3 progress_server.py ${MONITOR_OUTPUT} --port 8002"
echo ""
echo "  Then open: http://localhost:8002"
echo ""

mkdir -p "${OUTPUT}/stills"
mkdir -p "${OUTPUT}/telemetry"
export DISTILL_TELEMETRY_DIR="${OUTPUT}/telemetry"

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0: Enumerate source and build manifest
# ─────────────────────────────────────────────────────────────────────────────

ENUM_DIR="${OUTPUT}/.enumeration"
mkdir -p "${ENUM_DIR}"

log "Phase 0: Enumerating source + building source inventory..."

# Always re-enumerate (cheap): the filter rules live in source_inventory.py (EXCLUDE_RULES) and are
# hashed into the identity. .rdl/.rdlc/.dtsx are XML text and are paged; .rpt is paged only when the
# JRC ingest helper is available. Every excluded file is written to excluded.tsv with its reason.
ENUM_SUMMARY=$("${PYTHON}" "${SID_PY}" enumerate "${SOURCE}" --out-dir "${ENUM_DIR}" --suffix .next \
  ${GIT_PROVENANCE:+--provenance "${GIT_PROVENANCE}"} \
  --config "token_budget=${TOKEN_BUDGET}" "${GIT_ID_CONFIG[@]+"${GIT_ID_CONFIG[@]}"}") || { log "Phase 0: ERROR enumeration failed"; exit 1; }
"${PYTHON}" - "${ENUM_SUMMARY}" "${ENUM_DIR}/excluded.tsv" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
print(f"  {d['candidates']} source files after filtering; {d['excluded']} excluded (list: {sys.argv[2]})")
for reason, n in sorted(d["excluded_by_reason"].items(), key=lambda kv: -kv[1]):
    print(f"    excluded {n:6d}  {reason}")
PYEOF
SRC_ID=$("${PYTHON}" "${SID_PY}" get "${ENUM_DIR}/source_inventory.json.next" source_identity)
OLD_SRC_ID=""
[[ -f "${ENUM_DIR}/source_inventory.json" ]] && OLD_SRC_ID=$("${PYTHON}" "${SID_PY}" get "${ENUM_DIR}/source_inventory.json" source_identity)
if [[ -f "${ENUM_DIR}/candidates.txt" && -n "${OLD_SRC_ID}" && "${OLD_SRC_ID}" == "${SRC_ID}" ]]; then
  log "  Using existing enumeration (source identity ${SRC_ID:0:12} unchanged)."
  rm -f "${ENUM_DIR}/candidates.txt.next" "${ENUM_DIR}/excluded.tsv.next" "${ENUM_DIR}/source_inventory.json.next"
else
  if [[ -f "${ENUM_DIR}/candidates.txt" ]]; then
    SRC_DIFF=$("${PYTHON}" "${SID_PY}" diff "${ENUM_DIR}/source_inventory.json" "${ENUM_DIR}/source_inventory.json.next")
    log "  Source identity changed (${OLD_SRC_ID:0:12}${OLD_SRC_ID:+ → }${SRC_ID:0:12}); prior enumeration and everything downstream are stale."
    "${PYTHON}" - "${SRC_DIFF}" <<'PYEOF'
import json, sys
d = json.loads(sys.argv[1])
for k in ("added", "changed", "deleted"):
    if d[k]:
        print(f"    {k:8s} {len(d[k]):5d}: {', '.join(d[k][:8])}{' …' if len(d[k]) > 8 else ''}")
if not any(d.values()):
    print("    file contents unchanged — filter rules, helper/script versions or config changed")
PYEOF
    for f in candidates.txt excluded.tsv source_inventory.json; do backup "${ENUM_DIR}/${f}" "stale_enumeration"; done
  fi
  mv "${ENUM_DIR}/candidates.txt.next" "${ENUM_DIR}/candidates.txt"
  mv "${ENUM_DIR}/excluded.tsv.next" "${ENUM_DIR}/excluded.tsv"
  mv "${ENUM_DIR}/source_inventory.json.next" "${ENUM_DIR}/source_inventory.json"
fi
SRC_INVENTORY_HASH=$("${PYTHON}" "${SID_PY}" get "${ENUM_DIR}/source_inventory.json" inventory_hash)

FILE_COUNT=$(wc -l < "${ENUM_DIR}/candidates.txt" | tr -d ' ')
if [[ "${FILE_COUNT}" == "0" ]]; then
  log "Phase 0: ERROR no source files left after filtering — nothing to distill (see ${ENUM_DIR}/excluded.tsv)."
  write_status failed "enumeration: 0 files"; exit 1
fi

PAGES_DIR="${OUTPUT}/.pages"

# Pages (and the materialized manifest / plate order they were built from) are reusable only when
# stamped with the current source identity + plate pass-in. Otherwise they are moved to .backup/.
PAGES_ID=$(stage_id --upstream "${SRC_ID}" --config "plate_passin=${PLATE_STRATEGY:-}")
PAGES_REUSABLE=0
if [[ -d "${PAGES_DIR}" ]] && ls "${PAGES_DIR}"/page_*/concat.txt &>/dev/null; then
  if stage_fresh pages "${PAGES_ID}"; then
    PAGES_REUSABLE=1
  else
    log "Phase 0: existing pages were not built from the current source identity — invalidating pages."
    backup "${PAGES_DIR}" "stale_pages"
    backup "${ENUM_DIR}/materialized_manifest.tsv" "stale_pages"
    backup "${ENUM_DIR}/plated_manifest.txt" "stale_pages"
    stage_clear pages
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0.5: Materialize — resolve each candidate to a content-fidelity kind (raw/inferred/
#            referential) BEFORE paging, running ingest helpers so paging knows each unit's SIZE.
# ─────────────────────────────────────────────────────────────────────────────
# Resolves the size chicken-egg: paging fills to a token budget so it needs each unit's size up
# front, but an INFERRED unit's size is the HELPER OUTPUT size (unknown until the helper runs). So
# helpers run HERE (once), lifting non-text-native sources (e.g. .rpt → JRC report definition) into
# a cache, and we emit a MATERIALIZED MANIFEST (path, kind, unit, unit_size, source_size, method,
# conf, notes) that concat-batch.sh consumes with all sizes known. Un-liftable binaries become
# `referential` (marker + size only, no body) — stopping the significant Sonnet input
# wasted paging flattened binary noise. Idempotent: cached lifts are reused; helpers whose
# runtime is absent (e.g. JVM/JRC jars not provisioned) degrade gracefully (rpt → referential).
INGEST_HELPERS_PY="${INGEST_HELPERS_PY:-${SCRIPT_DIR}/runtimes/mcp-server/ingest_helpers.py}"
INGEST_CACHE="${OUTPUT}/.ingest_cache"
MATERIALIZED_MANIFEST="${ENUM_DIR}/materialized_manifest.tsv"

if [[ -f "${INGEST_HELPERS_PY}" ]]; then
  if [[ "${PAGES_REUSABLE}" == "1" ]]; then
    log "Phase 0.5: Pages already built for this source identity — reusing materialized manifest."
  else
    log "Phase 0.5: Materializing (ingest helpers) → ${MATERIALIZED_MANIFEST}"
    "${PYTHON}" "${INGEST_HELPERS_PY}" materialize "${ENUM_DIR}/candidates.txt" "${MATERIALIZED_MANIFEST}" \
      --source-root "${SOURCE}" --cache-dir "${INGEST_CACHE}" || {
      log "Phase 0.5: WARNING materialize failed — proceeding without a materialized manifest (all raw)."
      MATERIALIZED_MANIFEST=""
    }
  fi
  # Export so Phase 1's concat-batch.sh emits KIND:/SIZE:/VIA: markers (raw/inferred/referential).
  [[ -f "${MATERIALIZED_MANIFEST}" ]] && export MATERIALIZED_MANIFEST
else
  log "Phase 0.5: ingest_helpers.py not found — skipping materialization (all files paged raw)."
  MATERIALIZED_MANIFEST=""
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0b: Plate strategy — reorder the page manifest by the resolved strategy
# ─────────────────────────────────────────────────────────────────────────────
# HOW files rack onto pages ("plates") is a NAMED, pluggable capability
# (runtimes/mcp-server/plate_strategy.py) — the SAME seam wired into map-reduce-still.sh.
# The strategy is a pure permutation of the manifest ORDER that Phase 1 walks; concat-batch.sh
# is UNCHANGED. It changes only grouping into pages, never file content.
#
# Precedence: recorded run_manifest.json plate_strategy > PLATE_STRATEGY env pass-in > default.
# single-plate = identity (offset order — the existing/baseline behavior, so an existing .pages
# stays bit-identical). A DIFFERENT strategy yields different pages → a disjoint, internally-stable
# block_id spine (L6, expected not defect): a dependency-cohesive run is a NEW run, never a re-pack
# of an existing single-plate run, so residuals keyed on the existing ids are safe by construction.

PAGE_MANIFEST="${ENUM_DIR}/candidates.txt"
PLATE_STRATEGY_PY="${PLATE_STRATEGY_PY:-${SCRIPT_DIR}/runtimes/mcp-server/plate_strategy.py}"
RUN_MANIFEST="${OUTPUT}/run_manifest.json"

if [[ -f "${PLATE_STRATEGY_PY}" ]]; then
  # Resolve pass-in > registry default. A recorded manifest value is NOT read as authority (same
  # trapdoor as orchestration): whether a recorded plate is a legitimate resume is adjudicated by
  # the identity gate (Phase 1.6), which records plate_strategy into run_identity (single writer).
  # Plate can't be deferred like orchestration — it determines page GROUPING, needed before Phase 1
  # builds pages — but its recorded-value authority is deferred to the identity gate all the same.
  RESOLVED_STRATEGY=$("${PYTHON}" "${PLATE_STRATEGY_PY}" resolve "-" --passin "${PLATE_STRATEGY:-}") || {
    log "Phase 0b: ERROR resolving plate strategy (unknown strategy?)"; exit 1;
  }
  log "Phase 0b: Plate strategy = ${RESOLVED_STRATEGY} (passin: ${PLATE_STRATEGY:-<none>}; recorded value NOT read here — identity gate decides resume)"

  # Only reorder when pages are NOT already built — an existing plate layout is immutable for this
  # run (Phase 1 reuses it below), so re-permuting the manifest would misrepresent what was paged.
  if [[ "${PAGES_REUSABLE}" == "1" ]]; then
    log "Phase 0b: Pages already built for this source identity — keeping existing plate layout."
    # The reused pages were built from the plated order, so the conservation check (Phase 1.1)
    # must compare against that order, not candidates.txt (any non-identity plate otherwise fails
    # every re-run with "ledger path mismatch").
    [[ -f "${ENUM_DIR}/plated_manifest.txt" ]] && PAGE_MANIFEST="${ENUM_DIR}/plated_manifest.txt"
  else
    PLATED_MANIFEST="${ENUM_DIR}/plated_manifest.txt"
    "${PYTHON}" "${PLATE_STRATEGY_PY}" reorder "${RESOLVED_STRATEGY}" "${PAGE_MANIFEST}" "${PLATED_MANIFEST}" --source-root "${SOURCE}" || {
      log "Phase 0b: ERROR reordering manifest"; exit 1;
    }
    PAGE_MANIFEST="${PLATED_MANIFEST}"
  fi
  # Recording deferred to Phase 1.6 identity gate (writes plate_strategy into run_identity).
else
  log "Phase 0b: WARNING plate_strategy.py not found at ${PLATE_STRATEGY_PY} — using candidates.txt order (single-plate)."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0c: Map orchestration strategy — resolve + record (X-map-reduce-extraction-9)
# ─────────────────────────────────────────────────────────────────────────────
# HOW stills sweep over pages (still-major vs page-major) is a NAMED, pluggable capability
# (runtimes/mcp-server/orchestration_strategy.py) — the sibling of plate strategy one axis over
# (plate = WHICH files rack onto a page; this = HOW stills sweep over pages). page-centric caches
# the page prefix across the stills applied to it (~cost win). Both strategies emit the identical
# page_NNN/{still}_extract.md layout, so reduce/downstream are blind to which ran.
#
# Precedence: ORCHESTRATION_STRATEGY env pass-in > registry effective-default (page-centric).
# A recorded run_manifest value is deliberately NOT read as authority here — that was a trapdoor:
# a vestigial/sediment manifest could silently hijack a fresh run. Whether a recorded strategy is
# a legitimate RESUME (and may be inherited) is decided later by the IDENTITY GATE (Phase 1.6),
# which only trusts it when the artifact PROVES the same run (run_identity palm/preamble match).
# Here we just resolve the strategy this run WANTS; the identity gate records + adjudicates.
#
# Phase 2 branches on ORCH_STRATEGY: page-centric drives the unified async MCP surface (submit_map
# page-major -> submit_reduce per-still) via a thin in-process invoker; still-centric keeps the
# per-still distill-worker loop. Both write the identical stills/{still}/page_NNN/{still}_extract.md
# + {still}_raw_concat.md layout, so everything after Phase 2 is blind to which strategy ran.
ORCH_STRATEGY_PY="${ORCH_STRATEGY_PY:-${SCRIPT_DIR}/runtimes/mcp-server/orchestration_strategy.py}"
ORCH_STRATEGY="page-centric"  # fallback only if the strategy module is missing (resolved below)
if [[ -f "${ORCH_STRATEGY_PY}" ]]; then
  # NOTE: pass "-" for the manifest arg — do NOT read a recorded strategy as authority (see above).
  RESOLVED_ORCH=$("${PYTHON}" "${ORCH_STRATEGY_PY}" resolve "-" --passin "${ORCHESTRATION_STRATEGY:-}") || {
    log "Phase 0c: ERROR resolving orchestration strategy (unknown strategy?)"; exit 1;
  }
  ORCH_STRATEGY="${RESOLVED_ORCH}"
  log "Phase 0c: Orchestration strategy = ${ORCH_STRATEGY} (passin: ${ORCHESTRATION_STRATEGY:-<none>}; recorded value NOT read here — identity gate decides resume)"
  # Recording is deferred to the Phase 1.6 identity gate, which writes orchestration_strategy as
  # part of the run_identity block (single writer; provenance + resume-adjudication in one place).
else
  log "Phase 0c: WARNING orchestration_strategy.py not found — defaulting to still-centric."
fi
export ORCH_STRATEGY

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: Build concat pages (shared across all stills)
# ─────────────────────────────────────────────────────────────────────────────

if [[ "${PAGES_REUSABLE}" == "1" ]]; then
  TOTAL_PAGES=$(ls -d "${PAGES_DIR}"/page_*/ 2>/dev/null | wc -l | tr -d ' ')
  log "Phase 1: Reusing ${TOTAL_PAGES} existing pages (source identity ${SRC_ID:0:12} matches)."
else
  log "Phase 1: Building concat pages (budget: ${TOKEN_BUDGET} tokens/page)..."
  mkdir -p "${PAGES_DIR}"
  OFFSET=0
  PAGE=0
  while true; do
    PAGE_DIR="${PAGES_DIR}/page_$(printf '%03d' ${PAGE})"
    mkdir -p "${PAGE_DIR}"
    bash runtimes/pipeline/concat-batch.sh "${SOURCE}" "${PAGE_MANIFEST}" "${PAGE_DIR}" "${OFFSET}" "${TOKEN_BUDGET}" > /dev/null
    read -r FILES_IN BYTES_IN TOKENS_IN REMAINING < "${PAGE_DIR}/stats.txt"
    if [[ "${FILES_IN}" == "0" ]]; then backup "${PAGE_DIR}" "empty_pages"; break; fi
    OFFSET=$(cat "${PAGE_DIR}/next_offset.txt")
    PAGE=$((PAGE + 1))
  done
  TOTAL_PAGES=${PAGE}
  log "  Built ${TOTAL_PAGES} pages from ${FILE_COUNT} files."
fi

TOTAL_PAGES=$(ls -d "${PAGES_DIR}"/page_*/ 2>/dev/null | wc -l | tr -d ' ')
log "  Pages: ${TOTAL_PAGES}"

# >>> Phase 1.1: source-conservation gate (P0 blocker 1) >>>
# Fail closed before ANY model call unless every manifest entry, part and line is on exactly one
# page or is listed as skipped with a reason (writes ${PAGES_DIR}/conservation_report.json and
# ${PAGES_DIR}/skipped_manifest.tsv). Pages built by the pre-fix concat-batch.sh have no ledger
# and fail here: delete ${PAGES_DIR} and re-run to rebuild them.
CONS_MAT_ARG=()
[[ -n "${MATERIALIZED_MANIFEST:-}" && -f "${MATERIALIZED_MANIFEST:-}" ]] && CONS_MAT_ARG=(--materialized "${MATERIALIZED_MANIFEST}")
if ! "${PYTHON}" "${SCRIPT_DIR}/runtimes/pipeline/check-page-conservation.py" --pages "${PAGES_DIR}" \
     --manifest "${PAGE_MANIFEST}" --source-root "${SOURCE}" "${CONS_MAT_ARG[@]+"${CONS_MAT_ARG[@]}"}"; then
  log "Phase 1.1: ERROR source conservation check FAILED — halting before any model call."
  log "  See ${PAGES_DIR}/conservation_report.json"
  exit 1
fi
# <<< Phase 1.1: source-conservation gate <<<
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.5: PREFLIGHT gate — report coverage / escalations / critical-signal-at-risk
#            BEFORE the expensive Map-Reduce spend (go/no-go). Runs everything up to
#            just-before M/R; cheap + deterministic (no M/R). "It would suck to do an
#            expensive run of M/R only to find out you were missing a huge chunk of signal."
# ─────────────────────────────────────────────────────────────────────────────
# DISTILL_PREFLIGHT modes (default: report — never surprise-block):
#   report  (default)  survey + write the report + continue to M/R regardless of verdict.
#   gate               HALT before M/R only if the verdict is CRITICAL (steering signal at risk).
#   stop               run-to-preflight: ALWAYS halt after the report (inspect, then re-run to proceed).
PREFLIGHT_PY="${PREFLIGHT_PY:-${SCRIPT_DIR}/runtimes/mcp-server/preflight.py}"
PREFLIGHT_MODE="${DISTILL_PREFLIGHT:-report}"
PREFLIGHT_REPORT="${OUTPUT}/preflight_report.json"
if [[ -f "${PREFLIGHT_PY}" ]]; then
  log "Phase 1.5: Preflight (coverage + escalations + signal-at-risk) — mode=${PREFLIGHT_MODE}"
  PF_MAT_ARG=()
  [[ -n "${MATERIALIZED_MANIFEST:-}" && -f "${MATERIALIZED_MANIFEST:-}" ]] && PF_MAT_ARG=(--materialized "${MATERIALIZED_MANIFEST}")
  PF_FAIL=()
  [[ "${PREFLIGHT_MODE}" == "gate" ]] && PF_FAIL=(--fail-on CRITICAL)
  if "${PYTHON}" "${PREFLIGHT_PY}" --pages "${PAGES_DIR}" "${PF_MAT_ARG[@]}" \
       --still-specs ./still_specs --out "${PREFLIGHT_REPORT}" "${PF_FAIL[@]}"; then
    # The stdout block above shows the verdict + findings + a one-line survey, but the FULL
    # per-modality file-type census + rig-coverage breakdown only lives in the JSON report.
    # Point the operator at it so the detail is discoverable, not buried.
    log "Phase 1.5: full file-type classification + rig-coverage detail → ${PREFLIGHT_REPORT}"
    log "  (per-modality reserves/extensions/structural-rig; read it for the complete census)"
  else
    log "Phase 1.5: PREFLIGHT verdict CRITICAL and mode=gate → HALTING before Map-Reduce."
    log "  Review ${PREFLIGHT_REPORT}; resolve the critical signal-at-risk (or set DISTILL_PREFLIGHT=report to override), then re-run."
    exit 3
  fi
  if [[ "${PREFLIGHT_MODE}" == "stop" ]]; then
    log "Phase 1.5: mode=stop (run-to-preflight) → HALTING after preflight. Review ${PREFLIGHT_REPORT}, then re-run with DISTILL_PREFLIGHT=report to proceed."
    exit 0
  fi
else
  log "Phase 1.5: preflight.py not found — skipping the go/no-go gate."
fi

# Content gate (fail closed, before any spend): if NO page carries a source body — every [FILE:]
# block is referential/marker-only — the model would be asked to distill nothing (it refuses or
# invents). Halt unless explicitly overridden.
if ! "${PYTHON}" "${CONTENT_GUARD_PY}" pages "${PAGES_DIR}"; then
  if [[ "${DISTILL_ALLOW_NO_CONTENT:-0}" == "1" ]]; then
    log "Phase 1.5: WARNING no page carries source content (all marker-only); continuing (DISTILL_ALLOW_NO_CONTENT=1)."
  else
    log "Phase 1.5: ERROR no page carries source content — every file is referential/marker-only, so Map-Reduce would see nothing."
    log "  Check ${MATERIALIZED_MANIFEST:-the materialized manifest} (kind column) and ${ENUM_DIR}/excluded.tsv; add a text-native"
    log "  extension or ingest helper, then re-run. HALTING before Map-Reduce (no spend)."
    write_status failed "content gate: all pages marker-only"
    exit 3
  fi
fi
# Pages passed the pre-spend gates: stamp them with the identity they were built from. Reused
# pages keep their stamp (and build token), so stages chained from the pages token (Phase 1.2
# facts) are reusable too; only rebuilt pages get a new token.
if [[ "${PAGES_REUSABLE}" == "1" ]] && stage_fresh pages "${PAGES_ID}"; then :; else stage_stamp pages "${PAGES_ID}"; fi
echo ""

# DISTILL_ESTIMATE_ONLY=1 (launcher pre-flight): price the run from the pages and stop. Nothing
# below runs (no facts, no identity gate, so no stale-output clearing; no model call). A full run
# into the same output dir afterwards reuses the enumeration and pages at $0.
if [[ "${DISTILL_ESTIMATE_ONLY:-0}" == "1" ]]; then
  log "Phase 1.7: Cost estimate only (DISTILL_ESTIMATE_ONLY=1; DISTILL_MAX_USD=${DISTILL_MAX_USD:-unset})"
  BUDGET_RC=0
  "${PYTHON}" "${BUDGET_PY}" estimate --output "${OUTPUT}" --pages-dir "${PAGES_DIR}" \
    --stills "${STILLS[*]}" || BUDGET_RC=$?
  if [[ "${BUDGET_RC}" -eq "${EXIT_BUDGET}" ]]; then
    log "Phase 1.7: estimate is over DISTILL_MAX_USD (no model call made). See ${OUTPUT}/run_status.json."
    exit "${EXIT_BUDGET}"
  elif [[ "${BUDGET_RC}" -ne 0 ]]; then
    log "Phase 1.7: ERROR the cost estimate failed (rc=${BUDGET_RC})."
    write_status failed "budget estimate error"
    exit 1
  fi
  write_status estimated
  log "Estimate only: done (budget_estimate.json written; no model call made)."
  exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.2: Structural facts (deterministic; no model call)
# ─────────────────────────────────────────────────────────────────────────────
# runtimes/facts: a .NET 8 tool (Roslyn for VB.NET, ScriptDom for T-SQL) parses the enumerated
# source into facts.json (methods, calls, VB->proc calls, proc->table reads/writes, constants,
# comments); facts_post.py adds cross-layer comparison and duplicate-logic candidates. One bounded,
# line-cited block per page goes to ${FACTS_DIR}/pages/page_NNN.md, and the page-centric map
# appends it to that page. Runs after the conservation + pre-spend gates (so it chains from the
# stamped pages token) and before the identity gate, which records the blocks' hash (facts_hash):
# turning facts on/off or any facts change re-maps every still.
# Facts are an enhancement: no dotnet / build failure / parse failure → WARNING, continue without.
FACTS_DIR="${OUTPUT}/facts"
FACTS_PY="${SCRIPT_DIR}/runtimes/facts/build_facts.py"
FACTS_PAGES_DIR=""
FACTS_HASH=""
if [[ "${DISTILL_FACTS:-1}" == "0" ]]; then
  log "Phase 1.2: structural facts disabled (DISTILL_FACTS=0)."
  stage_clear facts
elif [[ ! -f "${FACTS_PY}" ]]; then
  log "Phase 1.2: WARNING ${FACTS_PY} not found — continuing without structural facts."
else
  HAVE_DOTNET=0
  { [[ -n "${DISTILL_FACTS_CMD:-}" ]] || command -v dotnet &>/dev/null; } && HAVE_DOTNET=1
  FACTS_ID=$(stage_id --upstream "$(stage_token pages)" \
    --code "${FACTS_PY}" "${SCRIPT_DIR}/runtimes/facts/facts_block.py" "${SCRIPT_DIR}/runtimes/facts/facts_post.py" \
           "${SCRIPT_DIR}/runtimes/facts/structure/Program.cs" "${SCRIPT_DIR}/runtimes/facts/structure/LeanStructure.csproj" \
    --config "page_budget=${TOKEN_BUDGET}" "max_fraction=${DISTILL_FACTS_MAX_FRACTION:-0.15}" \
             "tool_available=${HAVE_DOTNET}" "tool_cmd=${DISTILL_FACTS_CMD:-}")
  if stage_fresh facts "${FACTS_ID}" && [[ -f "${FACTS_DIR}/facts_status.json" ]]; then
    log "Phase 1.2: Reusing structural facts (pages + facts code unchanged)."
  else
    [[ -d "${FACTS_DIR}" ]] && backup "${FACTS_DIR}" "stale_facts"
    log "Phase 1.2: Building structural facts → ${FACTS_DIR}"
    if [[ "${HAVE_DOTNET}" == "0" ]]; then
      log "Phase 1.2: WARNING dotnet not found (setup.sh installs dotnet-sdk-8.0) — continuing WITHOUT structural facts."
    fi
    if "${PYTHON}" "${FACTS_PY}" build --source-root "${SOURCE}" --manifest "${PAGE_MANIFEST}" \
         --pages "${PAGES_DIR}" --out "${FACTS_DIR}" --page-budget "${TOKEN_BUDGET}" \
         --max-fraction "${DISTILL_FACTS_MAX_FRACTION:-0.15}"; then
      stage_stamp facts "${FACTS_ID}"
    else
      log "Phase 1.2: WARNING facts builder failed — continuing WITHOUT structural facts."
      stage_clear facts
    fi
  fi
  FACTS_HASH=$("${PYTHON}" "${FACTS_PY}" hash --out "${FACTS_DIR}" 2>/dev/null || echo none)
  if [[ "${FACTS_HASH}" != "none" && -n "${FACTS_HASH}" ]]; then
    FACTS_PAGES_DIR="${FACTS_DIR}/pages"
    log "Phase 1.2: structural facts ready ($(ls "${FACTS_PAGES_DIR}"/page_*.md 2>/dev/null | wc -l | tr -d ' ')/${TOTAL_PAGES} pages with a facts block)."
  else
    FACTS_HASH=""
    log "Phase 1.2: no structural facts for this run (see ${FACTS_DIR}/facts_status.json)."
  fi
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.3: Identifier evidence (deterministic; no model, no dotnet)
# ─────────────────────────────────────────────────────────────────────────────
# runtimes/facts/identifier_evidence.py collects abbreviated column/variable names and the corpus
# evidence of their meaning (comments, UI labels, definitions, how and where the value is set and
# tested) and writes one bounded block per page to ${IDENT_DIR}/pages/page_NNN.md. The page-centric
# map appends it after the facts block with an instruction to expand an abbreviation only when the
# evidence supports it and otherwise write "meaning: Unknown". Chained from the pages token; its
# blocks hash goes into the run identity (identifiers_hash), so any change re-maps every still.
# DISTILL_IDENTIFIERS=0 disables; a failure is a WARNING (the run continues without the block).
IDENT_DIR="${OUTPUT}/identifiers"
IDENT_PY="${SCRIPT_DIR}/runtimes/facts/identifier_evidence.py"
IDENT_PAGES_DIR=""
IDENT_HASH=""
IDENT_MAX_CHARS="${DISTILL_IDENTIFIERS_MAX_CHARS:-4000}"
IDENT_MAX_ENTRIES="${DISTILL_IDENTIFIERS_MAX_ENTRIES:-24}"
if [[ "${DISTILL_IDENTIFIERS:-1}" == "0" ]]; then
  log "Phase 1.3: identifier evidence disabled (DISTILL_IDENTIFIERS=0)."
  stage_clear identifiers
elif [[ ! -f "${IDENT_PY}" ]]; then
  log "Phase 1.3: WARNING ${IDENT_PY} not found — continuing without identifier evidence."
else
  IDENT_ID=$(stage_id --upstream "$(stage_token pages)" --code "${IDENT_PY}" \
    --config "max_chars=${IDENT_MAX_CHARS}" "max_entries=${IDENT_MAX_ENTRIES}")
  if stage_fresh identifiers "${IDENT_ID}" && [[ -f "${IDENT_DIR}/identifiers_status.json" ]]; then
    log "Phase 1.3: Reusing identifier evidence (pages + code unchanged)."
  else
    [[ -d "${IDENT_DIR}" ]] && backup "${IDENT_DIR}" "stale_identifiers"
    log "Phase 1.3: Building identifier evidence → ${IDENT_DIR}"
    if "${PYTHON}" "${IDENT_PY}" build --source-root "${SOURCE}" --manifest "${PAGE_MANIFEST}" \
         --pages "${PAGES_DIR}" --out "${IDENT_DIR}" --max-chars "${IDENT_MAX_CHARS}" \
         --max-entries "${IDENT_MAX_ENTRIES}"; then
      stage_stamp identifiers "${IDENT_ID}"
    else
      log "Phase 1.3: WARNING identifier evidence failed — continuing without it."
      stage_clear identifiers
    fi
  fi
  IDENT_HASH=$("${PYTHON}" "${IDENT_PY}" hash --out "${IDENT_DIR}" 2>/dev/null || echo none)
  if [[ "${IDENT_HASH}" != "none" && -n "${IDENT_HASH}" ]]; then
    IDENT_PAGES_DIR="${IDENT_DIR}/pages"
    log "Phase 1.3: identifier evidence ready ($(ls "${IDENT_PAGES_DIR}"/page_*.md 2>/dev/null | wc -l | tr -d ' ')/${TOTAL_PAGES} pages with a block)."
  else
    IDENT_HASH=""
  fi
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.6: RUN IDENTITY gate — record self-verifying identity + adjudicate resume
# ─────────────────────────────────────────────────────────────────────────────
# A run's output is a function of pages + steering preamble + per-still prompts. A recorded
# strategy is a legitimate RESUME signal ONLY when the artifact proves the same run — never from
# presence alone (vestigial manifests would hijack). This gate (pages now exist, so they can be
# fingerprinted) computes the current run_identity, compares it to any recorded one, and:
#   orphan/absent      → fresh run (nothing to inherit); record identity.
#   palm mismatch      → corpus changed → invalidate ALL stills (report which pages differ).
#   facts mismatch     → Phase 1.2 facts blocks changed / toggled → invalidate ALL stills.
#   preamble mismatch  → steering changed → invalidate ALL stills.
#   match              → per-still: a still resumes iff its prompt hash matches; others re-map.
# Invalidated stills' stale outputs are CLEARED here so Phase 2's skip-if-exists stays honest.
# Detection always logs a recorded value; resolution is gated on identity match (no vestigial-trust).
RUN_IDENTITY_PY="${RUN_IDENTITY_PY:-${SCRIPT_DIR}/runtimes/mcp-server/run_identity.py}"
if [[ -f "${RUN_IDENTITY_PY}" ]]; then
  STILL_CSV_ID=$(IFS=,; echo "${STILLS[*]}")
  log "Phase 1.6: Run identity gate (pages=${TOTAL_PAGES}, stills=${#STILLS[@]}, orch=${ORCH_STRATEGY}, plate=${RESOLVED_STRATEGY:-<none>})"
  IDENTITY_VERDICT="${OUTPUT}/.identity_verdict.json"
  "${PYTHON}" - "${RUN_IDENTITY_PY}" "${RUN_MANIFEST}" "${PAGES_DIR}" "${STILL_CSV_ID}" \
           "${ORCH_STRATEGY}" "${RESOLVED_STRATEGY:-}" "${OUTPUT}/stills" "${IDENTITY_VERDICT}" \
           "${SRC_INVENTORY_HASH}" "${FACTS_HASH:-}" "${IDENT_HASH:-}" <<'PYEOF'
import importlib.util, json, os, shutil, sys
mod_path, manifest, pages_dir, still_csv, orch, plate, stills_base, verdict_out, src_hash, facts_hash, ident_hash = sys.argv[1:12]

spec = importlib.util.spec_from_file_location("run_identity", mod_path)
ri = importlib.util.module_from_spec(spec); spec.loader.exec_module(ri)

stills = [s for s in still_csv.split(",") if s]
current = ri.compute_identity(pages_dir, stills, orch or None, plate or None,
                              source_inventory_hash=src_hash or None,
                              facts_hash=facts_hash or None,
                              identifiers_hash=ident_hash or None)
recorded = ri.read_recorded(manifest)
verdict = ri.diff(recorded, current)

# Always REPORT a recorded strategy (detection != authority).
rec_strat = verdict.get("recorded_strategy")
if rec_strat:
    sys.stderr.write(f"[identity] recorded orchestration_strategy={rec_strat!r} present "
                     f"(state={verdict['state']}) — inherited only if identity matches\n")
sys.stderr.write(f"[identity] state={verdict['state']}: {verdict['detail']}\n")
if verdict.get("changed_pages"):
    sys.stderr.write(f"[identity] changed pages: {verdict['changed_pages']}\n")

# CLEAR invalidated stills' stale outputs so Phase 2 skip-if-exists is honest. An invalidated
# still's per-page extracts + raw_concat are removed; resumable stills are left untouched.
cleared = []
for still in verdict.get("invalidated", []):
    sd = os.path.join(stills_base, still)
    if not os.path.isdir(sd):
        continue
    removed_any = False
    # per-page extract dirs (page_NNN/) + the reduced raw_concat/dedup/readme
    for entry in os.listdir(sd):
        p = os.path.join(sd, entry)
        if entry.startswith("page_") and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True); removed_any = True
        elif entry.endswith("_raw_concat.md") or entry.endswith("_dedup_index.json"):
            try: os.remove(p); removed_any = True
            except OSError: pass
    if removed_any:
        cleared.append(still)
if cleared:
    sys.stderr.write(f"[identity] cleared stale outputs for invalidated stills: {cleared}\n")

# Record the full run_identity (single writer of orchestration_strategy/plate_strategy provenance).
ri.record_identity(manifest, current)
with open(verdict_out, "w") as f:
    json.dump(verdict, f, indent=2)
print(verdict["state"])
PYEOF
  IDSTATE=$(cat "${IDENTITY_VERDICT}" 2>/dev/null | "${PYTHON}" -c "import sys,json;print(json.load(sys.stdin).get('state','?'))" 2>/dev/null || echo "?")
  log "Phase 1.6: identity=${IDSTATE} (verdict: ${IDENTITY_VERDICT}, recorded run_identity in ${RUN_MANIFEST})"
  # Map/reduce identity for the downstream chain: the full recorded run_identity (source inventory,
  # page palm, steering preamble, per-still prompt hashes, strategies).
  RUN_ID_HASH=$("${PYTHON}" -c 'import json,hashlib,sys; d=json.load(open(sys.argv[1]))["run_identity"]; print(hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest())' "${RUN_MANIFEST}")

  # HITL checkpoint (first slice of the checkpoint-policy design): the identity gate is the last
  # cheap, deterministic step before the Map spend AND it just made a resume decision that can
  # CLEAR stale outputs (a mutation). DISTILL_IDENTITY=stop halts here so a human can inspect the
  # verdict (and what was invalidated/cleared) before any Bedrock cost. Default: report (continue).
  if [[ "${DISTILL_IDENTITY:-report}" == "stop" ]]; then
    log "Phase 1.6: DISTILL_IDENTITY=stop → HALTING after the identity gate (no Map spend)."
    log "  Verdict: ${IDENTITY_VERDICT}; run_identity recorded in ${RUN_MANIFEST}."
    log "  Review, then re-run with DISTILL_IDENTITY=report (default) to proceed into Map-Reduce."
    exit 0
  fi
else
  log "Phase 1.6: run_identity.py not found — skipping identity gate (no resume adjudication)."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.7: Cost estimate + budget check (before any model call)
# ─────────────────────────────────────────────────────────────────────────────
# Estimate = pages x stills x per-call averages (telemetry history from DISTILL_COST_HISTORY, else
# the documented default table in budget.py) for map, reduce, synthesis and format, plus code
# graph, issues and bottling; work already on disk from an earlier attempt into this output dir
# (after the identity gate cleared anything stale) counts $0. Written to budget_estimate.json.
# Over DISTILL_MAX_USD → refuse to start (exit 6, run_status.json = over_estimate) unless
# DISTILL_ALLOW_OVER_ESTIMATE=1. DISTILL_WARN_USD only warns.
log "Phase 1.7: Cost estimate (DISTILL_MAX_USD=${DISTILL_MAX_USD:-unset}, DISTILL_WARN_USD=${DISTILL_WARN_USD:-unset})"
BUDGET_RC=0
"${PYTHON}" "${BUDGET_PY}" estimate --output "${OUTPUT}" --pages-dir "${PAGES_DIR}" \
  --stills "${STILLS[*]}" || BUDGET_RC=$?
if [[ "${BUDGET_RC}" -eq "${EXIT_BUDGET}" ]]; then
  log "Phase 1.7: estimate is over DISTILL_MAX_USD — not starting (no model call made). See ${OUTPUT}/run_status.json."
  exit "${EXIT_BUDGET}"
elif [[ "${BUDGET_RC}" -ne 0 ]]; then
  if [[ -n "${DISTILL_MAX_USD:-}" ]]; then
    log "Phase 1.7: ERROR the cost estimate failed (rc=${BUDGET_RC}) and DISTILL_MAX_USD is set — halting (fail closed)."
    write_status failed "budget estimate error"
    exit 1
  fi
  log "Phase 1.7: WARNING the cost estimate failed (rc=${BUDGET_RC}); no cap is set, continuing."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: Run each still (map + reduce via distill-worker agent)
# ─────────────────────────────────────────────────────────────────────────────

log "Phase 2: Processing ${#STILLS[@]} stills (orchestration=${ORCH_STRATEGY})..."
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — PAGE-CENTRIC branch (orchestration_strategy=page-centric, X-9)
# ─────────────────────────────────────────────────────────────────────────────
# One page-major sweep (all pages × all stills, page cached as prefix) via the unified async
# MCP surface, then async per-still reduce — driven by a thin in-process invoker so the
# distillation logic carries NO kiro-cli dependency (submit_map -> poll -> submit_reduce -> poll).
# Writes the identical stills/{still}/page_NNN/{still}_extract.md + stills/{still}/{still}_raw_
# concat.md layout as still-centric, so normalize/synthesis/blend + the dashboard are unchanged.
if [[ "${ORCH_STRATEGY}" == "page-centric" ]]; then
  PC_INVOKE="${SCRIPT_DIR}/runtimes/mcp-server/pagecentric_invoke.py"
  PC_PYTHON="${SCRIPT_DIR}/runtimes/mcp-server/.venv/bin/python"
  [[ -x "${PC_PYTHON}" ]] || PC_PYTHON="python3"

  # Build the page list. The canonical current-epoch page file is concat.txt (concat-batch.sh
  # writes .txt; every live consumer — jobs.py, source_index.py, rectify.py, the Phase 0/1 reuse
  # checks — keys on concat.txt). concat.md exists ONLY in pre-epoch vestigial runs (old
  # map-reduce-still.sh path) which are quarantined and never fed to a live run. So: .txt only,
  # no fallback — a missing concat.txt is a real defect to surface, not silently glob around.
  PC_PAGES_FILE="${OUTPUT}/.pagecentric_pages.txt"
  : > "${PC_PAGES_FILE}"
  for pd in "${PAGES_DIR}"/page_*/; do
    [[ -f "${pd}concat.txt" ]] && echo "${pd}concat.txt" >> "${PC_PAGES_FILE}"
  done
  PC_PAGE_COUNT=$(wc -l < "${PC_PAGES_FILE}" | tr -d ' ')
  if [[ "${PC_PAGE_COUNT}" == "0" ]]; then
    log "Phase 2: ERROR page-centric found no page concats under ${PAGES_DIR}"; exit 1
  fi

  # .started markers so the dashboard shows every still running immediately.
  for still in "${STILLS[@]}"; do
    mkdir -p "${OUTPUT}/stills/${still}"
    echo "{\"still\":\"${still}\",\"started\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
      > "${OUTPUT}/stills/${still}/.started"
  done

  STILL_CSV=$(IFS=,; echo "${STILLS[*]}")
  log "Phase 2: page-centric — ${PC_PAGE_COUNT} pages × ${#STILLS[@]} stills [${STILL_CSV}]"
  PC_LOG="${OUTPUT}/stills/_pagecentric.log"
  # Stream the invoker's output to BOTH the console and the log (tee), unbuffered — so the map
  # phase (the longest + most likely to throttle) is LIVE-VISIBLE on stdout instead of the console
  # going dead until it finishes. The controller's round/backoff/TPM-recovery lines are the whole
  # point of watching a big run; they must reach the terminal, not just the file. `pipefail` +
  # PIPESTATUS preserve the invoker's real exit code across the tee pipe.
  set -o pipefail
  # Invoker poll timeout: the wall after which the invoker gives up waiting on the map/reduce
  # job. Default 6h (was a hardcoded 2h that guillotined a legitimately long serial reduce on a
  # large corpus — map+reduce of 11 stills across many pages can exceed 2h). Override with DISTILL_PC_TIMEOUT_S.
  PC_TIMEOUT="${DISTILL_PC_TIMEOUT_S:-21600}"
  set -o pipefail
  # `|| PC_RC=$?` (not PIPESTATUS after the fact): under `set -e` a failing pipeline would otherwise
  # exit through the ERR trap before the rc could be inspected. With pipefail, $? is the invoker's
  # rc (tee exits 0).
  PC_RC=0
  PYTHONUNBUFFERED=1 DISTILL_TELEMETRY_DIR="${DISTILL_TELEMETRY_DIR}" \
  "${PC_PYTHON}" -u "${PC_INVOKE}" \
    --pages-file "${PC_PAGES_FILE}" \
    --output-base "${OUTPUT}/stills" \
    --stills "${STILL_CSV}" \
    --model-id "${BEDROCK_MODEL_ID}" \
    --timeout "${PC_TIMEOUT}" \
    ${FACTS_PAGES_DIR:+--facts-dir "${FACTS_PAGES_DIR}"} \
    ${IDENT_PAGES_DIR:+--identifiers-dir "${IDENT_PAGES_DIR}"} \
    2>&1 | tee "${PC_LOG}" || PC_RC=$?
  set +o pipefail
  if [[ "${PC_RC}" -eq 6 || "${PC_RC}" -eq 7 ]]; then
    # The in-loop budget guard refused the next map (6) or reduce (7) call. Finished per-(page,
    # still) extracts and reduced stills stay on disk; a resume redoes only what is missing.
    PC_STAGE=map; [[ "${PC_RC}" -eq 7 ]] && PC_STAGE=reduce
    "${PYTHON}" "${BUDGET_PY}" stop --output "${OUTPUT}" --stage "${PC_STAGE}" \
      --reason "The ${PC_STAGE} budget guard refused the next call (actual spend + in-flight reservations would exceed the cap)." || true
    exit "${EXIT_BUDGET}"
  fi
  if [[ "${PC_RC}" -ne 0 ]]; then
    log "Phase 2: ERROR page-centric invoker failed (rc=${PC_RC}) — see ${PC_LOG}"; exit 1
  fi
  log "Phase 2: page-centric complete (log: ${PC_LOG})"
  echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — STILL-CENTRIC branch
# ─────────────────────────────────────────────────────────────────────────────
# The still-centric strategy drove one external agent worker per still. That agent runtime is
# not part of this sample, so the strategy is recognised (for run-identity compatibility) but
# refused here. Use the default page-centric strategy, which calls Amazon Bedrock directly.
else
  log "Phase 2: ERROR orchestration strategy '${ORCH_STRATEGY}' needs an external agent runtime that this sample does not include."
  log "Phase 2:       Re-run with ORCHESTRATION_STRATEGY=page-centric (the default)."
  exit 1
fi  # end Phase 2 orchestration-strategy branch (page-centric | still-centric)

log "Phase 2 complete."
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 → 2.5 COMPLETENESS BARRIER (waterfall closure)
#
# The next phases (2.5 knowledge, 2.6 synthesis, blend) each SKIP a still whose
# raw_concat is absent. Without this gate a still that failed Map/Reduce would
# silently drop out of every downstream phase and the final deliverable — a
# clean-looking spec built on a SUBSET of the requested stills. This asserts the
# factual on-disk invariant those phases depend on: every expected still produced
# its {still}_raw_concat.md. Strategy-agnostic (protects page- AND still-centric).
#
# DISTILL_BARRIER modes (default: gate — a post-spend factual invariant, unlike
# preflight's predictive heuristic, so incompleteness HALTS by default):
#   gate    (default)  HALT if any expected still is missing its raw_concat.
#   report             WARN which stills are incomplete, then continue DEGRADED
#                      (for a deliberate mechanics-exercise / partial-still run).
#   stop               ALWAYS halt after the barrier report (inspect, then re-run).
# ─────────────────────────────────────────────────────────────────────────────
BARRIER_MODE="${DISTILL_BARRIER:-gate}"
MISSING_STILLS=()
# Content-aware: a raw_concat must exist AND be non-empty AND not be a model refusal ("I don't
# have the actual file contents…", "No source material available"). content_guard.py classifies
# each still as ok | no_findings (honest empty — allowed, warned) | refusal | empty | missing.
BARRIER_REPORT=$("${PYTHON}" "${CONTENT_GUARD_PY}" barrier "${OUTPUT}/stills" --stills "${ALL_STILLS_CSV}") || true
while IFS=$'\t' read -r still status detail; do
  [[ -z "${still}" ]] && continue
  STILL_OUTPUT="${OUTPUT}/stills/${still}"
  case "${status}" in
    ok)
      rm -f "${STILL_OUTPUT}/.failed" ;;
    no_findings)
      log "  [${still}] WARNING: output has no finding blocks (${detail}) — accepted as an honest empty result."
      rm -f "${STILL_OUTPUT}/.failed" ;;
    *)
      MISSING_STILLS+=("${still}")
      log "  [${still}] FAILED barrier: ${status} — ${detail}"
      if [[ "${status}" == "refusal" || "${status}" == "empty" ]]; then
        # Move the refused/empty output (and its per-page extracts) aside so a re-run recomputes
        # it instead of resuming from it.
        backup "${STILL_OUTPUT}/${still}_raw_concat.md" "barrier_rejected/${still}"
        backup "${STILL_OUTPUT}/${still}_dedup_index.json" "barrier_rejected/${still}"
        for pd in "${STILL_OUTPUT}"/page_*; do [[ -e "${pd}" ]] && backup "${pd}" "barrier_rejected/${still}"; done
      fi
      # Authoritative failure marker so the dashboard shows this still FAILED. The still-centric
      # worker writes a richer marker (with rc) on its own crash; only write here if absent.
      if [[ -d "${STILL_OUTPUT}" && ! -f "${STILL_OUTPUT}/.failed" ]]; then
        echo "{\"still\":\"${still}\",\"phase\":\"barrier\",\"reason\":\"${status} at Phase 2->2.5 barrier\",\"failed_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > "${STILL_OUTPUT}/.failed"
      fi ;;
  esac
done <<< "${BARRIER_REPORT}"
if [[ "${#MISSING_STILLS[@]}" -gt 0 ]]; then
  log "Phase 2 barrier: INCOMPLETE — ${#MISSING_STILLS[@]}/${#STILLS[@]} expected still(s) missing, empty or refused: ${MISSING_STILLS[*]}"
  if [[ "${BARRIER_MODE}" == "report" ]]; then
    log "Phase 2 barrier: mode=report → continuing DEGRADED. The final spec will be built on a SUBSET of stills (missing: ${MISSING_STILLS[*]})."
  else
    log "Phase 2 barrier: mode=${BARRIER_MODE} → HALTING before Phase 2.5 so a failed still cannot silently vanish from the deliverable."
    log "  Inspect the worker log(s) under ${OUTPUT}/stills/, fix the failing still, then re-run (completed stills are skipped/resumed)."
    log "  To intentionally proceed on a partial-still corpus, set DISTILL_BARRIER=report."
    write_status failed "barrier: ${MISSING_STILLS[*]}"
    exit 4
  fi
elif [[ "${BARRIER_MODE}" == "stop" ]]; then
  log "Phase 2 barrier: all ${#STILLS[@]} stills complete; mode=stop → HALTING after the barrier. Re-run with DISTILL_BARRIER=gate (default) to proceed."
  exit 0
else
  log "Phase 2 barrier: all ${#STILLS[@]} expected still(s) produced non-empty, non-refusal output — waterfall closure satisfied."
fi
# Map/reduce identity for the downstream chain: run identity (source inventory, page palm,
# preamble, per-still prompt hashes, strategies) + the actual content of every still's reduced
# output, so a still that was re-mapped (e.g. after a barrier rejection) recomputes downstream.
RAW_CONCATS=()
for still in "${STILLS[@]}"; do RAW_CONCATS+=("${OUTPUT}/stills/${still}/${still}_raw_concat.md"); done
STILLS_ID=$(stage_id --upstream "${RUN_ID_HASH:-${PAGES_ID}:${FACTS_HASH:-nofacts}:${IDENT_HASH:-noident}}" --code "${RAW_CONCATS[@]}" \
  --config "stills=${ALL_STILLS_CSV}" "orch=${ORCH_STRATEGY}")
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 → 2.5 Bridge: Normalize agent output (enforce expected layout)
# ─────────────────────────────────────────────────────────────────────────────

KNOWLEDGE_VENV="${SCRIPT_DIR}/runtimes/mcp-server/.venv/bin/activate"

log "Normalizing Phase 2 output (cleaning agent artifacts)..."
(
  source "${KNOWLEDGE_VENV}" 2>/dev/null || true
  python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/normalize_output.py" "${OUTPUT}/stills"
) 2>&1
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.5: Knowledge Layer (graph + vector dedup + materialize per-manifest)
# ─────────────────────────────────────────────────────────────────────────────

# Credential check helper — detect expiry before long operations
check_creds() {
  local profile_arg=""
  if [[ -n "${AWS_PROFILE:-}" ]]; then
    profile_arg="--profile ${AWS_PROFILE}"
  fi
  if ! command -v aws &>/dev/null; then
    log "ERROR: AWS CLI v2 not found on PATH (needed for the credential check) — run ./setup.sh."
    write_status failed "check_creds: aws cli missing"
    exit 1
  fi
  if ! aws sts get-caller-identity ${profile_arg} &>/dev/null; then
    log "ERROR: AWS credentials expired or unavailable."
    write_status failed "check_creds: credentials unavailable"
    log "  The script has skip logic — it will resume from where it left off."
    exit 1
  fi
}

log "Phase 2.5: Knowledge Layer (parse → embed → graph → dedup → materialize)..."
check_creds

KNOWLEDGE_SCRIPT="${SCRIPT_DIR}/runtimes/mcp-server/knowledge.py"
UNIFIED_GRAPH="${OUTPUT}/stills/knowledge_graph.json"
SHARED_VECTORS="${OUTPUT}/stills/shared_vectors.db"
K_ID=$(stage_id --upstream "${STILLS_ID}" \
  --code "${KNOWLEDGE_SCRIPT}" "${SCRIPT_DIR}/runtimes/mcp-server/normalize_output.py" \
         "${SCRIPT_DIR}"/runtimes/pipeline/manifests/*.txt \
  --config "stills=${ALL_STILLS_CSV}")

if stage_fresh knowledge "${K_ID}"; then
  log "  Knowledge layer identity unchanged (${K_ID:0:12}) — reusing final/ outputs + shared graph/vector store."
else
  # Not built from the current stills (or never stamped): every prior knowledge output is stale.
  # The shared vector DB and graph are rebuilt from scratch (downstream stages write into the DB,
  # and their ids chain from this one, so they recompute too).
  stage_clear knowledge
  for still in "${STILLS[@]}"; do backup "${OUTPUT}/stills/${still}/final" "stale_knowledge/${still}"; done
  backup "${UNIFIED_GRAPH}" "stale_knowledge"
  backup "${SHARED_VECTORS}" "stale_knowledge"

  for still in "${STILLS[@]}"; do
    STILL_DIR="${OUTPUT}/stills/${still}"
    CONCAT_FILE="${STILL_DIR}/${still}_raw_concat.md"
    FINAL_DIR="${STILL_DIR}/final"
    MANIFEST="${SCRIPT_DIR}/runtimes/pipeline/manifests/${still}.txt"

    if [[ ! -f "${CONCAT_FILE}" ]]; then
      log "  [${still}] No raw_concat found. Skipping."
      continue
    fi

    # Back up any artifacts the distill-worker agent may have created in wrong locations
    for f in "${STILL_DIR}/knowledge_graph.json" ${STILL_DIR}/*_vectors.db; do
      backup "${f}" "${still}"
    done

    log "  [${still}] Building knowledge graph..."
    (
      source "${KNOWLEDGE_VENV}" 2>/dev/null || true
      python3 -u "${KNOWLEDGE_SCRIPT}" "${CONCAT_FILE}" "${still}" "${FINAL_DIR}" "${MANIFEST}" "${UNIFIED_GRAPH}"
    ) 2>&1

    log "  [${still}] Done."
  done
  stage_stamp knowledge "${K_ID}"
fi

log "Phase 2.5 complete."
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.6: Concept Synthesis (LLM merge of multi-contributor unified concepts)
# ─────────────────────────────────────────────────────────────────────────────

SYNTHESIZE_SCRIPT="${SCRIPT_DIR}/runtimes/mcp-server/synthesize_concepts.py"
S_ID=$(stage_id --upstream "$(stage_token knowledge)" --code "${SYNTHESIZE_SCRIPT}" --config "skip=${SKIP_SYNTHESIS:-0}")

if stage_fresh synthesis "${S_ID}"; then
  log "Phase 2.6: Synthesis identity unchanged — skipping."
elif [[ "${SKIP_SYNTHESIS:-0}" == "1" ]]; then
  log "Phase 2.6: Synthesis skipped (SKIP_SYNTHESIS=1)"
  stage_stamp synthesis "${S_ID}"
elif [[ ! -f "${SHARED_VECTORS}" ]]; then
  log "Phase 2.6: No shared_vectors.db found. Skipping."
else
  budget_gate synthesis
  log "Phase 2.6: Concept Synthesis (merging multi-contributor concepts)..."
  check_creds
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${SYNTHESIZE_SCRIPT}" "${SHARED_VECTORS}" --concurrency 5
  ) 2>&1
  stage_stamp synthesis "${S_ID}"
  log "Phase 2.6 complete."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.75: Format still manifests (LLM formatting pass for quality)
# ─────────────────────────────────────────────────────────────────────────────

FORMAT_SCRIPT="${SCRIPT_DIR}/runtimes/mcp-server/format_still.py"
F_ID=$(stage_id --upstream "$(stage_token synthesis)" --code "${FORMAT_SCRIPT}")

if stage_fresh format "${F_ID}"; then
  log "Phase 2.75: Format identity unchanged — all stills already formatted. Skipping."
else
  budget_gate format
  # format_still.py skips a still whose formatted/.complete exists, so stale formatted output
  # must be moved aside first.
  stage_clear format
  for still in "${STILLS[@]}"; do
    backup "${OUTPUT}/stills/${still}/final/formatted" "stale_format/${still}"
  done
  log "Phase 2.75: Formatting still manifests..."
  check_creds
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${FORMAT_SCRIPT}" "${OUTPUT}/stills"
  ) 2>&1
  stage_stamp format "${F_ID}"
  log "Phase 2.75 complete."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.8: Code Dependency Graph (validated LLM extraction; ON by default)
#   Reconstructs code files from the pages, extracts imports/refs/defines +
#   read/write direction + target-system module + abbreviated→business entity decoding,
#   validates every anchor against the source, and writes source_files /
#   code_objects / code_edges / code_entities into the shared vector DB.
#   Independent of the still pipeline; feeds reconciliation and the graph.
#   Disable with DISTILL_CODE_GRAPH=0 (a small fraction, ~2%, of a map run on a large corpus).
#   Uses LLM calls (DISTILL_CODE_GRAPH_MODEL, default Haiku 4.5); failures fail the run.
# ─────────────────────────────────────────────────────────────────────────────

CODE_GRAPH_ENABLED="${DISTILL_CODE_GRAPH:-1}"
CG_PAGES_DIR="${OUTPUT}/.pages"
CG_MARKER="${OUTPUT}/.code_graph_done"
CG_OK=0
GRAPH_DIR="${OUTPUT}/knowledge_graph"
CG_ID=$(stage_id --upstream "$(stage_token format)" --code "${SCRIPT_DIR}/runtimes/mcp-server/code_graph.py" \
  --config "enabled=${CODE_GRAPH_ENABLED}" "model=${DISTILL_CODE_GRAPH_MODEL:-}")

if [[ "${CODE_GRAPH_ENABLED}" == "1" ]]; then
  if stage_fresh code_graph "${CG_ID}" && [[ -f "${CG_MARKER}" ]]; then
    log "Phase 2.8: Code dependency graph identity unchanged — skipping."
    CG_OK=1
  elif [[ -d "${CG_PAGES_DIR}" ]] && [[ -f "${SHARED_VECTORS}" ]]; then
    budget_gate code_graph
    log "Phase 2.8: Code Dependency Graph (validated LLM extraction)..."
    stage_clear code_graph; rm -f "${CG_MARKER}"
    # Rebuild from empty code tables (code_edges is append-only; a stale re-run would duplicate).
    "${PYTHON}" - "${SHARED_VECTORS}" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for t in ("source_files", "code_objects", "code_edges", "code_entities", "source_coverage"):
    c.execute(f"DROP TABLE IF EXISTS {t}")
c.commit(); c.close()
PYEOF
    check_creds
    mkdir -p "${GRAPH_DIR}"
    if (
      source "${KNOWLEDGE_VENV}" 2>/dev/null
      python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/code_graph.py" \
        "${CG_PAGES_DIR}" \
        "${SHARED_VECTORS}" \
        --concurrency "${DISTILL_CODE_GRAPH_CONCURRENCY:-12}" \
        --stats-out "${GRAPH_DIR}/code_graph_stats.json"
    ) 2>&1; then
      printf '%s\n' "${CG_ID}" > "${CG_MARKER}"
      stage_stamp code_graph "${CG_ID}"
      CG_OK=1
      log "  Code dependency graph done (stats: ${GRAPH_DIR}/code_graph_stats.json)."
    else
      log "  ERROR: code dependency graph FAILED (see output above). Set DISTILL_CODE_GRAPH=0 to run without it."
      REQUIRED_FAILURES+=("2.8 code graph")
    fi
  else
    log "Phase 2.8: ERROR code graph enabled but pages or shared_vectors.db are missing."
    REQUIRED_FAILURES+=("2.8 code graph (no pages or shared_vectors.db)")
  fi
else
  log "Phase 2.8: Code dependency graph disabled (DISTILL_CODE_GRAPH=0)."
  stage_fresh code_graph "${CG_ID}" || stage_stamp code_graph "${CG_ID}"
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.9: Graph Arbitration (Tier-1 deterministic + Tier-2 Sonnet residue)
#   Resolves conflicts/inconsistencies the per-file guards left behind, using
#   corpus-wide ground truth (CREATE kinds, write-DML, confirmed entities).
#   Binding but lossless — arbitration_log preserves every original value.
#   Runs only if the code dependency graph (2.8) was built.
# ─────────────────────────────────────────────────────────────────────────────

# Downstream ids chain from the code_graph stamp TOKEN: a failed (unstamped) code graph yields
# "unbuilt", so whatever is built without it recomputes once the code graph succeeds.
ARB_MARKER="${OUTPUT}/.arbitrate_done"
A_ID=$(stage_id --upstream "$(stage_token code_graph)" --code "${SCRIPT_DIR}/runtimes/mcp-server/arbitrate.py" \
  "${SCRIPT_DIR}/runtimes/mcp-server/crosscheck.py")
if [[ "${CODE_GRAPH_ENABLED}" == "1" && "${CG_OK}" == "1" ]]; then
  if stage_fresh arbitrate "${A_ID}" && [[ -f "${ARB_MARKER}" ]]; then
    log "Phase 2.9: Graph arbitration identity unchanged — skipping."
  else
    budget_gate arbitrate
    log "Phase 2.9: Graph Arbitration (Tier-1 deterministic + Tier-2 Sonnet)..."
    stage_clear arbitrate; rm -f "${ARB_MARKER}"
    check_creds
    if (
      source "${KNOWLEDGE_VENV}" 2>/dev/null
      python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/arbitrate.py" \
        "${SHARED_VECTORS}" --tier2 &&
      # Corroborate entity decodings against the business stills (deterministic booster).
      python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/crosscheck.py" \
        "${SHARED_VECTORS}"
    ) 2>&1; then
      printf '%s\n' "${A_ID}" > "${ARB_MARKER}"
      stage_stamp arbitrate "${A_ID}"
      log "  Graph arbitration + cross-check done."
    else
      log "  ERROR: graph arbitration / cross-check FAILED (see output above)."
      REQUIRED_FAILURES+=("2.9 graph arbitration")
    fi
  fi
elif [[ "${CODE_GRAPH_ENABLED}" != "1" ]]; then
  stage_fresh arbitrate "${A_ID}" || stage_stamp arbitrate "${A_ID}"
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.95: Concept-Symbol Bridge + Source-Fidelity Back-Map
#   Builds the bridge between the business concept layer and the code-dependency
#   graph, and the source-faithful symbol ledger that underpins traceability:
#     (a) concept_symbols.json — concept<->symbol index (deterministic, no LLM)
#     (b) symbol_backmap        — synthesized symbol -> verbatim source token,
#                                 deterministic attestation (verbatim/normalized/
#                                 tail); unmatched-but-cited rows are coined_unverified
#     (c) residue pass          — Haiku semantic alignment of coined_unverified rows
#                                 that HAVE a cited source, to reach 100% with-source
#                                 fidelity (anti-overfit: map only to observed tokens,
#                                 else coined_confirmed; never invents)
#   (a)+(b) are deterministic and free; (c) costs a few $ and needs creds. The
#   residue pass is skippable via DISTILL_SKIP_BACKMAP_RESIDUE=1 (bridge still built).
#   Idempotent via .concept_symbols_done marker + stage identity.
# ─────────────────────────────────────────────────────────────────────────────

CS_PAGES_DIR="${OUTPUT}/.pages"
CS_SYMBOLS_JSON="${OUTPUT}/stills/concept_symbols.json"
CS_MARKER="${OUTPUT}/.concept_symbols_done"
CS_ID=$(stage_id --upstream "$(stage_token arbitrate)" --code "${SCRIPT_DIR}/runtimes/mcp-server/concept_symbols.py" \
  "${SCRIPT_DIR}/runtimes/mcp-server/symbol_backmap.py" "${SCRIPT_DIR}/runtimes/mcp-server/symbol_backmap_residue.py" \
  --config "skip_residue=${DISTILL_SKIP_BACKMAP_RESIDUE:-0}")

if stage_fresh concept_symbols "${CS_ID}" && [[ -f "${CS_MARKER}" ]]; then
  log "Phase 2.95: Concept-symbol bridge + back-map identity unchanged — skipping."
elif [[ -f "${SHARED_VECTORS}" ]]; then
  [[ "${DISTILL_SKIP_BACKMAP_RESIDUE:-0}" == "1" ]] || budget_gate backmap_residue
  log "Phase 2.95: Concept-Symbol Bridge + Source-Fidelity Back-Map..."
  stage_clear concept_symbols; rm -f "${CS_MARKER}"

  # (a) concept<->symbol index (deterministic, no LLM)
  log "  Building concept-symbol index..."
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/concept_symbols.py" \
      "${OUTPUT}/stills" --out "${CS_SYMBOLS_JSON}"
  ) 2>&1

  # (b) deterministic back-map ledger (verbatim/normalized/tail; no LLM)
  #     --rebuild so re-runs are clean; the residue pass (optional LLM) then refines it.
  log "  Building symbol back-map ledger (deterministic)..."
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/symbol_backmap.py" \
      "${OUTPUT}/stills" "${CS_PAGES_DIR}" "${SHARED_VECTORS}" --rebuild
  ) 2>&1

  # (c) LLM residue pass to reach 100% with-source fidelity (opt-out; costs $)
  if [[ "${DISTILL_SKIP_BACKMAP_RESIDUE:-0}" == "1" ]]; then
    log "  Back-map residue pass skipped (DISTILL_SKIP_BACKMAP_RESIDUE=1)."
  elif [[ -d "${CS_PAGES_DIR}" ]]; then
    log "  Back-map residue pass (Haiku semantic alignment)..."
    check_creds
    RESIDUE_PROFILE_ARG=""
    if [[ -n "${AWS_PROFILE:-}" ]]; then RESIDUE_PROFILE_ARG="--profile ${AWS_PROFILE}"; fi
    (
      source "${KNOWLEDGE_VENV}" 2>/dev/null
      python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/symbol_backmap_residue.py" \
        "${OUTPUT}/stills" "${CS_PAGES_DIR}" "${SHARED_VECTORS}" \
        --concurrency "${DISTILL_BACKMAP_RESIDUE_CONCURRENCY:-8}" ${RESIDUE_PROFILE_ARG}
    ) 2>&1
  else
    log "  Back-map residue pass skipped (no pages dir)."
  fi

  printf '%s\n' "${CS_ID}" > "${CS_MARKER}"
  stage_stamp concept_symbols "${CS_ID}"
  log "Phase 2.95 complete."
else
  log "Phase 2.95: Skipped (no shared_vectors.db)"
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.97: Graph artifacts → ${OUTPUT}/knowledge_graph/ (deterministic, no LLM, always runs)
#   index.json, knowledge_graph.json (concept graph), code_graph.json (code-dependency graph),
#   concept_code_bridge.json (concept ↔ code links). Required output.
# ─────────────────────────────────────────────────────────────────────────────
log "Phase 2.97: Exporting graph artifacts → ${GRAPH_DIR}/"
# --source-root lets the bridge join VERIFY each link's assertion against the real corpus (it would
# otherwise fall back to <output>/facts/source, then to the reconstructed pages). A link whose
# identifier is not in the file it cites is dropped into the bridge's `dropped[]` ledger, never
# emitted as a guess.
if "${PYTHON}" "${SCRIPT_DIR}/runtimes/mcp-server/export_graph.py" "${OUTPUT}" \
     --source-root "${SOURCE}"; then
  log "  Graph artifacts: ${GRAPH_DIR}/index.json"
else
  log "  ERROR: graph artifact export failed (no concept graph?)."
  REQUIRED_FAILURES+=("2.97 knowledge graph export")
fi
echo ""

# >>> Phase 2.98: Issues & Insights >>>
# ─────────────────────────────────────────────────────────────────────────────
# Phase 2.98: Issues & Insights → ${OUTPUT}/issues/ (issues.json + issues.md). Required output.
#   Deterministic candidate scan over the numbered source pages + code graph (repeated literals,
#   identical expressions, uncalled routines, undefined references, commented-out / obsolete code,
#   comment-stated rules, DB-side checks), then a few bounded LLM calls (DISTILL_ISSUES_MAX_CALLS,
#   default 3; DISTILL_ISSUES_MAX_COST USD guard, default 3) that confirm/classify candidates and add
#   issues seen in the stills. Every cited location is verified against the source pages.
#   Optional input: ${OUTPUT}/facts/facts.json (hashed into the identity when present).
#   Fails (required) when candidates existed but no issue survived. DISTILL_ISSUES=0 disables.
# ─────────────────────────────────────────────────────────────────────────────
ISSUES_DIR="${OUTPUT}/issues"
ISSUES_PY="${SCRIPT_DIR}/runtimes/mcp-server/issues.py"
IS_ID=$(stage_id --upstream "$(stage_token concept_symbols)" --code "${ISSUES_PY}" \
  "${SCRIPT_DIR}/still_specs/issues/system_prompt.md" "${OUTPUT}/facts/facts.json" \
  --config "model=${DISTILL_ISSUES_MODEL:-${BEDROCK_MODEL_ID}}" "max_calls=${DISTILL_ISSUES_MAX_CALLS:-3}" \
  "target=${TARGET_NAME}")
if [[ "${DISTILL_ISSUES:-1}" != "1" ]]; then
  log "Phase 2.98: Issues & Insights disabled (DISTILL_ISSUES=0)."
elif stage_fresh issues "${IS_ID}" && [[ -s "${ISSUES_DIR}/issues.json" ]] && [[ -s "${ISSUES_DIR}/issues.md" ]]; then
  log "Phase 2.98: Issues & Insights identity unchanged — skipping."
else
  budget_gate issues
  # Under a run cap, issues.py's own per-call cost guard gets min(DISTILL_ISSUES_MAX_COST, USD left).
  ISSUES_COST_ARG=()
  if [[ -n "${DISTILL_MAX_USD:-}" ]]; then
    ISSUES_LEFT=$("${PYTHON}" "${BUDGET_PY}" remaining --output "${OUTPUT}")
    ISSUES_COST_ARG=(--max-cost "$("${PYTHON}" -c 'import sys; print(f"{min(float(sys.argv[1]), float(sys.argv[2])):.4f}")' "${DISTILL_ISSUES_MAX_COST:-3}" "${ISSUES_LEFT}")")
  fi
  log "Phase 2.98: Issues & Insights (candidate scan + bounded LLM review)..."
  stage_clear issues
  backup "${ISSUES_DIR}" "stale_issues"
  mkdir -p "${ISSUES_DIR}"
  check_creds
  if "${PYTHON}" -u "${ISSUES_PY}" "${OUTPUT}" --target "${TARGET_NAME}" --out-dir "${ISSUES_DIR}" \
       "${ISSUES_COST_ARG[@]+"${ISSUES_COST_ARG[@]}"}" 2>&1 \
     && [[ -s "${ISSUES_DIR}/issues.json" ]] && [[ -s "${ISSUES_DIR}/issues.md" ]]; then
    stage_stamp issues "${IS_ID}"
    log "  Issues & Insights: ${ISSUES_DIR}/issues.md"
  else
    log "  ERROR: Issues & Insights FAILED (empty report while candidates existed, or model failure)."
    REQUIRED_FAILURES+=("2.98 issues & insights")
  fi
fi
echo ""
# <<< Phase 2.98: Issues & Insights <<<

# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: Reconciliation (cross-still synthesis)
# ─────────────────────────────────────────────────────────────────────────────

log "Phase 3: Reconciliation..."

RECON_OUTPUT="${OUTPUT}/reconciliation"
R_ID=$(stage_id --upstream "$(stage_token concept_symbols)" --code "${SCRIPT_DIR}/runtimes/mcp-server/reconcile.py")
if stage_fresh reconcile "${R_ID}" && ls "${RECON_OUTPUT}"/*.md &>/dev/null; then
  log "  Reconciliation identity unchanged — skipping."
else
  stage_clear reconcile
  backup "${RECON_OUTPUT}" "stale_reconciliation"
  mkdir -p "${RECON_OUTPUT}"
  # Graph-driven reconciliation (deterministic, no LLM)
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/reconcile.py" "${OUTPUT}/stills" "${RECON_OUTPUT}"
  ) 2>&1
  stage_stamp reconcile "${R_ID}"
  log "  Reconciliation done."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: Master Blend
# ─────────────────────────────────────────────────────────────────────────────

log "Phase 4: Master Blend..."
check_creds

BLEND_OUTPUT="${OUTPUT}/master_blend"
B_ID=$(stage_id --upstream "$(stage_token reconcile)" --code "${SCRIPT_DIR}/runtimes/mcp-server/master_blend.py" \
  --config "target=${TARGET_NAME}")
if stage_fresh blend "${B_ID}" && [[ -s "${BLEND_OUTPUT}/master_blend.md" ]]; then
  log "  Master Blend identity unchanged — skipping."
else
  budget_gate blend
  stage_clear blend
  backup "${BLEND_OUTPUT}" "stale_blend"
  mkdir -p "${BLEND_OUTPUT}"
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/master_blend.py" \
      "${OUTPUT}/stills" \
      "${RECON_OUTPUT}" \
      "${BLEND_OUTPUT}" \
      "${TARGET_NAME}"
  ) 2>&1
  if [[ -s "${BLEND_OUTPUT}/master_blend.md" ]]; then
    stage_stamp blend "${B_ID}"
    log "  Master Blend done."
  else
    log "  ERROR: master blend produced no (or an empty) ${BLEND_OUTPUT}/master_blend.md."
    REQUIRED_FAILURES+=("4 master blend")
  fi
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 5: Bottling
# ─────────────────────────────────────────────────────────────────────────────

BOTTLED_OUTPUT="${OUTPUT}/bottled_spec"
BT_ID=$(stage_id --upstream "$(stage_token blend)" --code "${SCRIPT_DIR}/runtimes/mcp-server/bottling.py" \
  "${SCRIPT_DIR}/still_specs/bottling/prompt.md" \
  --config "target=${TARGET_NAME}")
if [[ "${SKIP_BOTTLING:-0}" == "1" ]]; then
  log "Phase 5: Bottling skipped (SKIP_BOTTLING=1)"
elif [[ ! -s "${BLEND_OUTPUT}/master_blend.md" ]]; then
  log "Phase 5: Bottling NOT run — no master blend to bottle."
  REQUIRED_FAILURES+=("5 bottling (no master blend)")
else
  log "Phase 5: Bottling..."
  check_creds
  if stage_fresh bottling "${BT_ID}" && [[ -s "${BOTTLED_OUTPUT}/bottled_spec.md" ]]; then
    log "  Bottled spec identity unchanged — skipping."
  else
    budget_gate bottling
    stage_clear bottling
    backup "${BOTTLED_OUTPUT}" "stale_bottling"
    mkdir -p "${BOTTLED_OUTPUT}"
    # --source-root: every Evidence citation is checked against the real source files (falls back
    # to the run's .pages dumps). Telemetry: bottling logs each call once as still=bottling.
    (
      source "${KNOWLEDGE_VENV}" 2>/dev/null
      python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/bottling.py" \
        "${BLEND_OUTPUT}" \
        "${BOTTLED_OUTPUT}" \
        "${TARGET_NAME}" \
        --source-root "${SOURCE}"
    ) 2>&1
    if [[ -s "${BOTTLED_OUTPUT}/bottled_spec.md" ]]; then
      stage_stamp bottling "${BT_ID}"
      log "  Bottling done."
    else
      log "  ERROR: bottling produced no (or an empty) ${BOTTLED_OUTPUT}/bottled_spec.md."
      REQUIRED_FAILURES+=("5 bottling")
    fi
  fi
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 6: HTML Viewer Generation (required output; always regenerated — deterministic, no LLM)
# ─────────────────────────────────────────────────────────────────────────────

log "Phase 6: HTML Viewer..."

VIEWER_SCRIPT="runtimes/pipeline/generate-viewer-v3.py"
VIEWER_DIR_NAME="viewer"
if [[ -f "${VIEWER_SCRIPT}" ]]; then
  VIEWER_REPO_ARG=""
  if [[ -n "${DISTILL_SOURCE_REPO_URL:-}" ]]; then
    VIEWER_REPO_ARG="--source-repo-url ${DISTILL_SOURCE_REPO_URL}"
  fi
  if (source "${KNOWLEDGE_VENV}" 2>/dev/null; python3 -u "${VIEWER_SCRIPT}" "${OUTPUT}" "${TARGET_NAME}" --viewer-dir "${OUTPUT}/${VIEWER_DIR_NAME}" ${VIEWER_REPO_ARG} --api-base "${DISTILL_VIEWER_API_BASE:-}") \
     && [[ -s "${OUTPUT}/${VIEWER_DIR_NAME}/index.html" ]]; then
    log "  HTML viewer: ${OUTPUT}/${VIEWER_DIR_NAME}/index.html"
  else
    log "  ERROR: HTML viewer generation failed (required output)."
    REQUIRED_FAILURES+=("6 viewer")
  fi
else
  log "  ERROR: ${VIEWER_SCRIPT} not found (required output)."
  REQUIRED_FAILURES+=("6 viewer (script missing)")
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Phase 6.5: Source Reference Index (evidence → GitHub links)
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_REPO_URL="${DISTILL_SOURCE_REPO_URL:-}"
PAGES_DIR="${OUTPUT}/.pages"

if [[ -n "${SOURCE_REPO_URL}" ]] && [[ -d "${PAGES_DIR}" ]] && [[ -f "${SHARED_VECTORS}" ]]; then
  log "Phase 6.5: Source Reference Index..."
  (
    source "${KNOWLEDGE_VENV}" 2>/dev/null
    python3 -u "${SCRIPT_DIR}/runtimes/mcp-server/source_index.py" \
      "${PAGES_DIR}" \
      "${SHARED_VECTORS}" \
      "${SOURCE_REPO_URL}"
  ) 2>&1
  log "  Source index done."
else
  if [[ -z "${SOURCE_REPO_URL}" ]]; then
    log "Phase 6.5: Skipped (set DISTILL_SOURCE_REPO_URL to enable evidence links)"
  fi
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────

if [[ "${#REQUIRED_FAILURES[@]}" -gt 0 ]]; then
  write_status failed "${REQUIRED_FAILURES[@]}"
  log "═══════════════════════════════════════════════════════════════"
  log "  Distillation FAILED — ${#REQUIRED_FAILURES[@]} required stage(s) failed:"
  for f in "${REQUIRED_FAILURES[@]}"; do log "    - ${f}"; done
  log "  Output (partial, NOT a deliverable): ${OUTPUT}"
  log "  Fix the failure and re-run; stages whose identity still matches are reused."
  log "═══════════════════════════════════════════════════════════════"
  exit 5
fi

write_status complete
log "═══════════════════════════════════════════════════════════════"
if [[ "${#MISSING_STILLS[@]}" -gt 0 ]]; then
  log "  Distillation complete (DEGRADED — DISTILL_BARRIER=report; missing stills: ${MISSING_STILLS[*]})."
else
  log "  Distillation complete."
fi
log "  Output: ${OUTPUT}"
log "  Stills: ${#STILLS[@]}"
log "  Pages: ${TOTAL_PAGES}"
log "  Reconciliation: ${RECON_OUTPUT}"
log "  Master Blend: ${BLEND_OUTPUT}"
if [[ "${SKIP_BOTTLING:-0}" != "1" ]]; then
  log "  Bottled Spec: ${BOTTLED_OUTPUT}"
fi
log "  Knowledge graph: ${GRAPH_DIR}/ (index.json, knowledge_graph.json, code_graph.json, concept_code_bridge.json)"
log "═══════════════════════════════════════════════════════════════"
echo ""
echo "  To launch the viewer:"
echo ""
ABS_OUTPUT="$(cd "${OUTPUT}" && pwd)"
ABS_SOURCE="$(cd "${SOURCE}" && pwd)"
echo "    cd ${SCRIPT_DIR}/runtimes/mcp-server && \\"
echo "    source .venv/bin/activate && \\"
echo "    python3 serve_viewer.py \\"
echo "      ${ABS_OUTPUT}/viewer \\"
echo "      ${ABS_OUTPUT}/stills/shared_vectors.db \\"
echo "      --source-root ${ABS_SOURCE} \\"
echo "      --port 8080"
echo ""
echo "  Then open: http://localhost:8080"
echo ""
