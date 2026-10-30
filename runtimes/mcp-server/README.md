# bedrock-distill MCP Server

Local MCP server for high-throughput Amazon Bedrock API access during the spec distillation map phase. Provides tools for submitting classification/extraction workloads to Bedrock (realtime execution), polling job status, and retrieving structured results — all orchestrated by the Kiro agent over stdio transport.

## Prerequisites

- Python 3.11+
- AWS credentials with Bedrock access (profile or environment variables)

## Setup

Dependencies are installed by the package's top-level `setup.sh` (creates the venv at
`runtimes/mcp-server/.venv` and installs boto3, anthropic, sentence-transformers, networkx,
numpy, markdown, hnswlib, olefile, mcp==1.28.1, torch-cpu, + pre-downloads the embedding
model). There is no `requirements.txt`; `setup.sh` is the single source of the dependency set.

```bash
# from the package root, once per machine:
./setup.sh
./check-dependencies.sh    # verifies every dep + an AWS/Bedrock round-trip before a run
```

## Configuration

### MCP Client Config

Add to your MCP client configuration (see `demo/mcp.json.example`). The server is a single top-level module (`server.py`,
launched over stdio), not a package — the command runs it directly:

```json
{
  "mcpServers": {
    "bedrock-distill": {
      "command": "runtimes/mcp-server/.venv/bin/python",
      "args": ["runtimes/mcp-server/server.py"],
      "env": {
        "AWS_PROFILE": "your-profile",
        "AWS_DEFAULT_REGION": "us-east-1",
        "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-6"
      }
    }
  }
}
```

> Most operators do NOT invoke the MCP tools by hand. The orchestrated entry point is the
> top-level `run_distillation.sh`, which drives the whole pipeline (enumerate → page →
> preflight → identity gate → page-centric map + async reduce → knowledge → synthesis →
> blend → bottle → viewer). See the top-level operator runbook (`docs/RUNBOOK.md`) for the
> env knobs (`DISTILL_*`) and the checkpoint contract.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_PROFILE` | Yes* | AWS profile name (*or use IAM role/env creds) |
| `AWS_DEFAULT_REGION` | Yes | AWS region for Bedrock API calls |
| `BEDROCK_MODEL_ID` | Yes | Model ID (e.g. `anthropic.claude-sonnet-4-20250514`) |

## Tools Exposed

The map/reduce surface is **key-based and async**: callers pass still NAMES; the server
resolves each name to its prompt via `still_registry` (no prompt text crosses the boundary),
and `strategy` selects still-centric vs page-centric.

| Tool | Description |
|------|-------------|
| `submit_map` | Submit a map job — `stills=[names]`, `strategy` (`page-centric` \| `still-centric`), pages, output_base. Async; poll with `check_distillation`. |
| `submit_reduce` | Submit an async reduce job — `stills=[names]`, partials_base, output_base (emits `phase=reduce` telemetry). |
| `check_distillation` | Poll job status/progress (shared submit→poll contract for map AND reduce). |
| `cancel_distillation` | Cancel an in-progress job. |
| `get_distillation_results` | Retrieve completed extraction results. |
| `submit_distillation` | Legacy single-still (still-centric) submit — retained; prefer `submit_map`. |
| `reduce_distillation` | Legacy reduce entry — prefer `submit_reduce`. |
| `build_knowledge_graph` | Phase 2.5 knowledge layer (parse → embed → graph → dedup → materialize). |
| `query_spec` | Retrieval-grounded Q&A over the distilled store (viewer chat API). |
| `vend_id` | Deterministic stable human-facing ID issuance (id-vending authority). |

## Usage Examples

### Submit a distillation job (realtime mode)

```json
{
  "tool": "submit_distillation",
  "arguments": {
    "mode": "realtime",
    "source_path": "output/MyProject/pages",
    "output_path": "output/MyProject/stills",
    "pages": [1, 2, 3, 4, 5],
    "concurrency": 4
  }
}
```

Response:

```json
{
  "job_id": "dist-a1b2c3d4",
  "status": "running",
  "total_pages": 5,
  "completed": 0
}
```

### Poll job status

```json
{
  "tool": "check_distillation",
  "arguments": {
    "job_id": "dist-a1b2c3d4"
  }
}
```

Response:

```json
{
  "job_id": "dist-a1b2c3d4",
  "status": "running",
  "total_pages": 5,
  "completed": 3,
  "failed": 0,
  "progress_pct": 60
}
```

### Retrieve results after completion

```json
{
  "tool": "get_distillation_results",
  "arguments": {
    "job_id": "dist-a1b2c3d4"
  }
}
```

### Cancel a running job

```json
{
  "tool": "cancel_distillation",
  "arguments": {
    "job_id": "dist-a1b2c3d4"
  }
}
```

## Orchestration Pattern

The typical orchestration loop from the Kiro agent:

1. Call `submit_distillation` with page list and concurrency settings
2. Poll `check_distillation` every few seconds until status is `completed` or `failed`
3. On completion, call `get_distillation_results` to retrieve structured output
4. Handle failures/retries at the orchestrator level
