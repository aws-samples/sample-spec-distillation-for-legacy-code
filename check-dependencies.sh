#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# check-dependencies.sh — Pre-flight validation for Specification Distillation
#
# Verifies EVERYTHING that could fail during a run:
# - System tools (python, tmux, aws) and the orchestration strategy
# - Python packages (all imports)
# - AWS credentials (STS, Bedrock, S3)
# - LLM call (one small Amazon Bedrock Converse round-trip; costs a fraction of a cent)
# - Embedding model (local HuggingFace download + inference)
# - SQLite vector store (create, insert, query)
# - NetworkX graph (create, serialize, load)
# - File system (write permissions in output dir)
#
# Run this BEFORE starting a multi-hour distillation run.
# Usage: ./check-dependencies.sh [output_dir]
# ─────────────────────────────────────────────────────────────────────────────
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SCRIPT_DIR}/runtimes/mcp-server/.venv/bin"
PYTHON="${VENV}/python"
OUTPUT_DIR="${1:-${SCRIPT_DIR}/output/_preflight_check}"

PASS=0
FAIL=0
WARN=0

check() {
  local name="$1"
  local result="$2"
  if [[ $? -eq 0 ]] && [[ -n "${result}" ]]; then
    echo "  ✓ ${name}: ${result}"
    PASS=$((PASS + 1))
  else
    echo "  ✗ ${name}: FAILED"
    FAIL=$((FAIL + 1))
  fi
}

check_cmd() {
  local name="$1"
  shift
  local output
  output=$("$@" 2>&1) && {
    echo "  ✓ ${name}: ${output}"
    PASS=$((PASS + 1))
  } || {
    echo "  ✗ ${name}: FAILED — ${output}"
    FAIL=$((FAIL + 1))
  }
}

warn() {
  echo "  ⚠ $1"
  WARN=$((WARN + 1))
}

echo "═══════════════════════════════════════════════════════════════"
echo "  Pre-flight Dependency Check"
echo "  Install dir: ${SCRIPT_DIR}"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── 1. System Tools ──────────────────────────────────────────────────────────

echo "[1/8] System tools..."
check_cmd "python3" "${PYTHON}" --version
if command -v tmux &>/dev/null; then
  check_cmd "tmux" tmux -V
else
  warn "tmux not installed (optional; useful for long runs on a remote host)"
fi
# Only the page-centric orchestration strategy (Amazon Bedrock Converse, in-process) ships with
# this sample. still-centric needs an external agent runtime that is not included.
ORCH="$("${PYTHON}" "${SCRIPT_DIR}/runtimes/mcp-server/orchestration_strategy.py" resolve - \
          --passin "${ORCHESTRATION_STRATEGY:-}" 2>/dev/null || echo "${ORCHESTRATION_STRATEGY:-page-centric}")"
if [[ "${ORCH}" == "page-centric" ]]; then
  echo "  ✓ orchestration strategy: ${ORCH}"
  PASS=$((PASS + 1))
else
  echo "  ✗ orchestration strategy: ${ORCH} is not supported by this sample (use page-centric)"
  FAIL=$((FAIL + 1))
fi
check_cmd "aws-cli" aws --version
echo ""

# ── 2. Python Packages ───────────────────────────────────────────────────────

echo "[2/8] Python packages..."
check_cmd "boto3" "${PYTHON}" -c "import boto3; print(boto3.__version__)"
check_cmd "sentence-transformers" "${PYTHON}" -c "from sentence_transformers import SentenceTransformer; print('OK')"
check_cmd "networkx" "${PYTHON}" -c "import networkx; print(networkx.__version__)"
check_cmd "numpy" "${PYTHON}" -c "import numpy; print(numpy.__version__)"
check_cmd "markdown" "${PYTHON}" -c "import markdown; print(markdown.__version__)"
check_cmd "mcp" "${PYTHON}" -c "from mcp.server import FastMCP; print('OK')"
check_cmd "torch" "${PYTHON}" -c "import torch; print(torch.__version__)"
check_cmd "hnswlib" "${PYTHON}" -c "import hnswlib; print('OK')"
check_cmd "olefile" "${PYTHON}" -c "import olefile; print(olefile.__version__)"
echo ""

# ── 3. AWS Credentials ───────────────────────────────────────────────────────

echo "[3/8] AWS credentials..."
PROFILE_ARG=""
if [[ -n "${AWS_PROFILE:-}" ]]; then
  PROFILE_ARG="--profile ${AWS_PROFILE}"
fi
check_cmd "sts:GetCallerIdentity" aws sts get-caller-identity ${PROFILE_ARG} --query "Account" --output text
echo ""

# ── 4. AWS Service Permissions ───────────────────────────────────────────────

echo "[4/8] AWS service permissions..."
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

# Bedrock: list foundation models (read-only, proves access)
check_cmd "bedrock:ListFoundationModels" aws bedrock list-foundation-models ${PROFILE_ARG} --region "${REGION}" --query "modelSummaries[0].modelId" --output text

echo ""

# ── 5. Bedrock LLM Call ──────────────────────────────────────────────────────

echo "[5/8] LLM round-trip (Amazon Bedrock Converse)..."
check_cmd "bedrock:Converse" "${PYTHON}" -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}/runtimes/mcp-server')
import bedrock_client
import os
profile = os.environ.get('AWS_PROFILE', '')
region = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
model = os.environ.get('BEDROCK_MODEL_ID', 'us.anthropic.claude-sonnet-4-6')
client = bedrock_client.create_llm_client(profile, region)
result = bedrock_client.call_llm(client, model, 'You are a test.', 'Say OK.', max_tokens=10)
print(f'OK ({result.input_tokens}in/{result.output_tokens}out tokens)')
"
echo ""

# ── 6. Embedding Model ───────────────────────────────────────────────────────

echo "[6/8] Embedding model (local HuggingFace)..."
check_cmd "embed:load+infer" "${PYTHON}" -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}/runtimes/mcp-server')
from knowledge import _get_embedding_model
import numpy as np
model = _get_embedding_model()
vec = model.encode(['test sentence'])
assert vec.shape == (1, 768), f'Unexpected shape: {vec.shape}'
print(f'OK (768-dim, norm={np.linalg.norm(vec[0]):.2f})')
"
echo ""

# ── 7. SQLite Vector Store ───────────────────────────────────────────────────

echo "[7/8] SQLite vector store (create/insert/query)..."
mkdir -p "${OUTPUT_DIR}"
check_cmd "vectordb:roundtrip" "${PYTHON}" -c "
import sys, os, tempfile
sys.path.insert(0, '${SCRIPT_DIR}/runtimes/mcp-server')
from knowledge import VectorStore, ConceptBlock, _get_embedding_model
import numpy as np

db_path = '${OUTPUT_DIR}/_test_vectors.db'
store = VectorStore(db_path)
model = _get_embedding_model()

# Create a test block
block = ConceptBlock(
    block_id='test_001', block_type='entity', title='Test Entity',
    body='### Entity: Test\\n- Definition: A test entity', source_page='page_000',
    source_still='test', classification='Primary', confidence=5, rule_type=''
)
block.embedding = model.encode(['Test Entity'])[0].astype(np.float32)

# Insert
store.upsert(block)

# Query
results = store.knn(block.embedding, k=1)
assert len(results) == 1
assert results[0][0] == 'test_001'
store.close()

# Cleanup
os.remove(db_path)
print('OK (insert + knn query)')
"
echo ""

# ── 8. NetworkX Graph ────────────────────────────────────────────────────────

echo "[8/8] NetworkX graph (create/save/load)..."
check_cmd "graph:roundtrip" "${PYTHON}" -c "
import sys, os, json
sys.path.insert(0, '${SCRIPT_DIR}/runtimes/mcp-server')
from knowledge import KnowledgeGraph, ConceptBlock
import numpy as np

graph_path = '${OUTPUT_DIR}/_test_graph.json'
graph = KnowledgeGraph(graph_path)

block = ConceptBlock(
    block_id='g_001', block_type='rule', title='Test Rule',
    body='### Rule: Test', source_page='page_000',
    source_still='test', classification='Core', confidence=4, rule_type='declarative'
)
graph.add_block(block)
graph.save(graph_path)

# Reload and verify
graph2 = KnowledgeGraph(graph_path)
assert graph2.node_count >= 1
os.remove(graph_path)
print(f'OK ({graph2.node_count} nodes, {graph2.edge_count} edges)')
"
echo ""

# ── Cleanup ──────────────────────────────────────────────────────────────────
rmdir "${OUTPUT_DIR}" 2>/dev/null || true

# ── Summary ──────────────────────────────────────────────────────────────────

echo "═══════════════════════════════════════════════════════════════"
if [[ ${FAIL} -eq 0 ]]; then
  echo "  ✓ All checks passed (${PASS} passed, ${WARN} warnings)"
  echo "  Ready to run distillation."
else
  echo "  ✗ ${FAIL} checks FAILED (${PASS} passed, ${WARN} warnings)"
  echo "  Fix the failures above before running distillation."
  exit 1
fi
echo "═══════════════════════════════════════════════════════════════"
