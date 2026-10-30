# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Judge output contract: one JSON Schema, used twice.

1. As the ``inputSchema`` of a Bedrock Converse tool, so the model emits
   structured JSON instead of free text.
2. As a validator for whatever comes back, so a malformed verdict raises
   instead of being silently mis-parsed.

The validator here is deliberately dependency-free: the test suite must run
without ``jsonschema`` installed. ``test_judge_schema.py`` cross-checks it
against ``jsonschema`` when that package is importable.
"""
from __future__ import annotations

import hashlib
import json

TOOL_NAME = "record_judgments"
SCHEMA_VERSION = "2026-10-06.1"

#: ``uncertain`` is the abstention verdict. The judge is told to use it rather
#: than guess; abstentions are reported as their own bucket, never forced.
VERDICTS = ("matched", "correct_extra", "unsupported", "incorrect", "uncertain")
DEGREES = ("full", "partial")
CONFIDENCES = ("high", "medium", "low")

#: Synthetic verdict for a finding the judge never mentioned. Never produced by
#: the model; assigned by the runner so every finding has a label.
UNJUDGED = "unjudged"
ALL_VERDICT_LABELS = VERDICTS + (UNJUDGED,)

MATCH_SCHEMA = {
    "type": "object",
    "required": ["key_id", "degree"],
    "additionalProperties": False,
    "properties": {
        "key_id": {"type": "string", "maxLength": 64},
        "degree": {"type": "string", "enum": list(DEGREES)},
    },
}

JUDGMENT_SCHEMA = {
    "type": "object",
    "required": ["finding_id", "verdict"],
    "additionalProperties": False,
    "properties": {
        "finding_id": {"type": "string", "maxLength": 64},
        "matches": {"type": "array", "maxItems": 10, "items": MATCH_SCHEMA},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
        "note": {"type": "string", "maxLength": 400},
    },
}

JUDGE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["judgments"],
    "additionalProperties": False,
    "properties": {
        "judgments": {"type": "array", "maxItems": 2000, "items": JUDGMENT_SCHEMA},
    },
}

TOOL_DESCRIPTION = (
    "Record one judgment per extracted finding. Call exactly once, with a judgment "
    "for every finding id you were given and no others."
)


def canonical(obj) -> str:
    """Stable JSON text for hashing (sorted keys, no incidental whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def schema_hash() -> str:
    """Content hash of the output contract; part of every cache key."""
    return hashlib.sha256(canonical(JUDGE_OUTPUT_SCHEMA).encode()).hexdigest()[:16]


class SchemaError(ValueError):
    """Judge output violated the contract. Raised, never swallowed."""


# --------------------------------------------------------------------------- validator
_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def validate(instance, schema=None, path: str = "$") -> None:
    """Validate ``instance`` against ``schema``; raise :class:`SchemaError` on the
    first violation, naming the JSON path. Supports the keyword subset actually
    used by this contract: type, properties, required, additionalProperties,
    items, enum, maxLength, minLength, maxItems, minItems."""
    schema = JUDGE_OUTPUT_SCHEMA if schema is None else schema

    t = schema.get("type")
    if t is not None:
        expected = _TYPE_MAP[t]
        # bool is a subclass of int in Python; JSON Schema treats them as distinct
        if t in ("integer", "number") and isinstance(instance, bool):
            raise SchemaError(f"{path}: expected {t}, got boolean")
        if not isinstance(instance, expected):
            got = type(instance).__name__
            raise SchemaError(f"{path}: expected {t}, got {got}")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} not one of {schema['enum']}")

    if isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(f"{path}: string longer than {schema['maxLength']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaError(f"{path}: string shorter than {schema['minLength']}")

    if isinstance(instance, list):
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaError(f"{path}: fewer than {schema['minItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for i, v in enumerate(instance):
                validate(v, item_schema, f"{path}[{i}]")

    if isinstance(instance, dict):
        for req in schema.get("required", []):
            if req not in instance:
                raise SchemaError(f"{path}: missing required property {req!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(props))
            if extra:
                raise SchemaError(f"{path}: unexpected properties {extra}")
        for k, v in instance.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")


def drop_null_optionals(instance, schema=None):
    """Return a copy of ``instance`` with ``null`` removed from OPTIONAL properties.

    Why: the model sometimes emits ``"note": null`` for a field it has nothing to
    say about. Absence and null mean the same thing for an optional field, and the
    runner already treats an absent ``note`` as ``""``. Failing a whole scoring
    stage on a cosmetic null (one judgment in ~2900 did exactly that) is stricter
    than the contract needs to be, so we normalise it away *before* validating.

    What it does NOT do, on purpose: it never touches a REQUIRED property (a null
    ``verdict`` still fails validation), never rewrites a non-null value (a wrong
    ``verdict`` string such as ``matches`` is left for validate + the corrective
    retry), and never removes a property the schema does not declare (those stay
    visible to ``additionalProperties: false``). The output contract itself
    (``JUDGE_OUTPUT_SCHEMA``, hence :func:`schema_hash` and every cache key) is
    unchanged. The input is not mutated, so a value read from the cache stays as
    it was on disk.
    """
    schema = JUDGE_OUTPUT_SCHEMA if schema is None else schema
    if isinstance(instance, list):
        item_schema = schema.get("items")
        if not item_schema:
            return list(instance)
        return [drop_null_optionals(v, item_schema) for v in instance]
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        out = {}
        for k, v in instance.items():
            if v is None and k in props and k not in required:
                continue
            out[k] = drop_null_optionals(v, props[k]) if k in props else v
        return out
    return instance


def tool_config() -> dict:
    """Bedrock Converse ``toolConfig`` that forces structured output."""
    return {
        "tools": [{
            "toolSpec": {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": {"json": JUDGE_OUTPUT_SCHEMA},
            }
        }],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }
