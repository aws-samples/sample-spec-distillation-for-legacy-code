<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
     SPDX-License-Identifier: MIT-0 -->
# Path to production (illustrative)

> **What this is:** a high-level, service-level sketch of how the sample could be run as a shared
> service. It is **not** deployable code and it is not part of this sample. The sample itself runs
> on one machine and binds its web servers to `127.0.0.1` only.

## Sample vs. a production shape, in one line

The sample runs everything as local processes on one machine. A production shape would split it
into an authenticated web/API tier and a worker tier, and move local files to managed services.

## Diagram

Source: [`application-service-view.mmd`](application-service-view.mmd) (Mermaid).

```
User (browser)
      │
      ▼
Amazon CloudFront + AWS WAF  ← HTTPS entry point and request filtering
      │
      ▼
Authentication               ← per-user sign-in (for example Amazon Cognito or IAM Identity Center)
      │
      ▼
Web / API service            ← containers on Amazon ECS on AWS Fargate; scales with traffic
      │
      ▼
Distillation workers         ← container tasks; run the pipeline in parallel, scale with workload
      │
      ├──> Amazon Bedrock               (reads code, writes the specification)
      ├──> Amazon S3                    (source code in; spec + viewer out; encrypted, private)
      └──> Graph + vector store         (for example Amazon Neptune and a vector index)
```

## What each piece is

| Piece | Role | Sample today → production shape |
|---|---|---|
| **CloudFront + WAF** | HTTPS entry point and protection | none (loopback only) → CloudFront with WAF |
| **Authentication** | Who may start runs (which spend money) and read specs | shared access code, demo only → per-user identity, least-privilege authorization, audit logging |
| **Web / API service** | UI and control plane (submit runs, status, viewer, chat) | one local Python process → containers on ECS Fargate |
| **Distillation workers** | Run the pipeline (analyze code → produce the spec) | the same local process → a pool of tasks running jobs in parallel, with per-job budget caps |
| **Amazon Bedrock** | Extracts meaning from code and writes the specification | unchanged (Converse API) |
| **Amazon S3** | Source code in; finished specification + viewer out | local disk → S3 with encryption, Block Public Access and lifecycle rules |
| **Graph + vector store** | The knowledge graph and searchable concepts behind the spec | local SQLite / JSON files → managed graph and vector services |

Everything in the right-hand column is design guidance only. A real deployment needs its own
threat model and security review.
