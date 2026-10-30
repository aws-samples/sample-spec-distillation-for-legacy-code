#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────
# setup.sh — one-time local setup for the spec-distillation sample (Linux or macOS).
#
# Creates runtimes/mcp-server/.venv, installs the Python dependencies, downloads the local
# embedding model (sentence-transformers/all-mpnet-base-v2) into the Hugging Face cache, and,
# if the .NET 8 SDK is installed, builds the optional structural-facts parser.
#
# It installs NO system packages and needs no root. Prerequisites (install them yourself):
#   - Python 3.10 or newer          (set PYTHON_BIN to choose an interpreter)
#   - AWS CLI v2                    (credentials setup, check-dependencies.sh)
#   - .NET 8 SDK (optional)         (parser facts; without it runs continue without them)
#
# Usage:  ./setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"
VENV_DIR="${SCRIPT_DIR}/runtimes/mcp-server/.venv"

echo "═══════════════════════════════════════════════════════════════"
echo "  Specification Distillation sample — local setup"
echo "═══════════════════════════════════════════════════════════════"

# ── 1. Python ────────────────────────────────────────────────────────────────
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  for c in python3.12 python3.11 python3.10 python3; do
    if command -v "${c}" &>/dev/null; then PYTHON_BIN="${c}"; break; fi
  done
fi
[[ -n "${PYTHON_BIN}" ]] || { echo "ERROR: Python 3.10+ not found (set PYTHON_BIN)"; exit 1; }
"${PYTHON_BIN}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "ERROR: ${PYTHON_BIN} is older than 3.10"; exit 1; }
echo "[1/4] Python: $("${PYTHON_BIN}" --version)"

# ── 2. Virtual environment + Python packages ─────────────────────────────────
echo "[2/4] Python virtual environment: ${VENV_DIR}"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/python" -m pip install --quiet --upgrade pip
# On Linux, install the CPU-only PyTorch build first. Otherwise sentence-transformers pulls a
# CUDA build (several GB). Embeddings here are small and run fine on CPU. On macOS the default
# PyPI wheel is already CPU/MPS-only.
if [[ "$(uname -s)" == "Linux" ]]; then
  "${VENV_DIR}/bin/pip" install --quiet torch --index-url https://download.pytorch.org/whl/cpu
fi
"${VENV_DIR}/bin/pip" install --quiet -r runtimes/mcp-server/requirements.txt -r runtimes/pipeline/requirements.txt
echo "  Installed: $("${VENV_DIR}/bin/python" -c 'import boto3, mcp, networkx; print("boto3", boto3.__version__, "| networkx", networkx.__version__)')"

# ── 3. Embedding model (downloaded once; runs read it offline) ───────────────
echo "[3/4] Downloading the embedding model (sentence-transformers/all-mpnet-base-v2, Apache-2.0)..."
"${VENV_DIR}/bin/python" -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-mpnet-base-v2')" 2>&1 | tail -2
echo "  Cached under \${HF_HOME:-~/.cache/huggingface}. Runs load it with HF_HUB_OFFLINE=1."

# ── 4. Optional: .NET 8 structural-facts parser ──────────────────────────────
echo "[4/4] Structural-facts parser (optional, needs the .NET 8 SDK)..."
if command -v dotnet &>/dev/null; then
  if (cd runtimes/facts/structure && DOTNET_CLI_TELEMETRY_OPTOUT=1 dotnet publish -c Release -o out 2>&1 | tail -2) \
     && [[ -f runtimes/facts/structure/out/LeanStructure.dll ]]; then
    echo "  Built runtimes/facts/structure/out/LeanStructure.dll"
  else
    echo "  WARNING: build failed; runs will continue without structural facts"
  fi
else
  echo "  dotnet not found; skipping. Runs will continue without structural facts (DISTILL_FACTS=0)."
fi

chmod +x run_distillation.sh check-dependencies.sh
mkdir -p output

cat <<MSG

═══════════════════════════════════════════════════════════════
  Setup complete.

  Next:
    1. Configure AWS credentials (aws configure, aws sso login, or AWS_* env vars)
       for an account with Amazon Bedrock model access in us-east-1.
    2. ./check-dependencies.sh            # includes one small Bedrock call
    3. Run on the bundled sample (see README.md, "Quick start").
═══════════════════════════════════════════════════════════════
MSG
