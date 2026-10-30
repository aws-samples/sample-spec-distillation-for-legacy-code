#!/usr/bin/env python3
"""Analyze the rule raw concat to count distinct logical business rules."""

import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import bedrock_client

RAW_PATH = sys.argv[1] if len(sys.argv) > 1 else ""

raw = Path(RAW_PATH).read_text()
CHUNK_SIZE = 400000
chunks = [raw[i:i+CHUNK_SIZE] for i in range(0, len(raw), CHUNK_SIZE)]
print(f"Total: {len(raw)/1024:.0f} KB, {len(chunks)} chunks")

llm = bedrock_client.create_llm_client(os.environ.get("AWS_PROFILE", ""), "us-east-1")

SYSTEM = "You are a precise analyst. Output only valid JSON. No markdown fencing."

PROMPT = (
    "Analyze this chunk of extracted business rules from a construction ERP.\n\n"
    "Count the DISTINCT LOGICAL BUSINESS RULES in this chunk. "
    "A logical business rule = one enforceable business constraint "
    "that belongs as a single entry in a spec.\n\n"
    "NOT rules (exclude from count):\n"
    "- Entity/schema/state definitions (descriptions of what things ARE, not constraints)\n"
    "- Technical infrastructure details (caching, indexing, serialization)\n"
    "- Migration/changelog bookkeeping\n"
    "- Meta-commentary or gap flags\n"
    "- Duplicate restatements of a rule already counted in THIS chunk\n\n"
    "Output JSON:\n"
    '{"chunk_number": N, "blocks_in_chunk": N, "distinct_rules_found": N, '
    '"non_rules_found": N, "rule_names": ["short name of each distinct rule"]}\n\n'
)

results = []


def analyze_chunk(idx, chunk_text):
    blocks_count = len(re.findall(r"^### ", chunk_text, re.MULTILINE))
    user_msg = f"{PROMPT}CHUNK {idx+1} of {len(chunks)}:\n\n{chunk_text}"
    result = bedrock_client.call_llm(
        llm,
        "us.anthropic.claude-sonnet-4-6",
        SYSTEM,
        user_msg,
        max_tokens=32768,
    )
    return idx, blocks_count, result.output_text


print("Analyzing chunks...")
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(analyze_chunk, i, c): i for i, c in enumerate(chunks)}
    for future in as_completed(futures):
        idx, blocks, output = future.result()
        results.append((idx, blocks, output))
        print(f"  Chunk {idx+1}/{len(chunks)}: {blocks} blocks analyzed")

results.sort(key=lambda x: x[0])

# Parse and aggregate
total_rules = 0
total_non_rules = 0
all_rule_names = []
for idx, blocks, output in results:
    try:
        output = re.sub(r"^```json?\n?", "", output.strip())
        output = re.sub(r"\n?```$", "", output.strip())
        data = json.loads(output)
        rules = data.get("distinct_rules_found", 0)
        non_rules = data.get("non_rules_found", 0)
        names = data.get("rule_names", [])
        total_rules += rules
        total_non_rules += non_rules
        all_rule_names.extend(names)
        print(f"  Chunk {idx+1}: {rules} rules, {non_rules} non-rules")
    except json.JSONDecodeError as e:
        print(f"  Chunk {idx+1}: PARSE ERROR: {e}")
        print(f"    First 200 chars: {output[:200]}")

print(f"\n=== TOTALS (before cross-chunk dedup) ===")
print(f"  Distinct rules per chunk (summed): {total_rules}")
print(f"  Non-rules: {total_non_rules}")
print(f"  Rule names collected: {len(all_rule_names)}")

# Save for inspection
out_path = Path(RAW_PATH).parent / "rule_count_analysis.json"
out_path.write_text(json.dumps({
    "total_rules_before_cross_chunk_dedup": total_rules,
    "total_non_rules": total_non_rules,
    "chunks_analyzed": len(chunks),
    "rule_names": all_rule_names,
}, indent=2))
print(f"  Saved: {out_path}")
