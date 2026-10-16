#!/usr/bin/env python3
"""Prepare one Object Storage job for translation-only Evidence recovery.

This is the controlled manual entrypoint for a historical partial Evidence
run.  It reads its already-written ``evidence.jsonl`` and produces the exact
``evidence.translation.job.v1`` payload consumed by the deployed
``continue-evidence-translation`` Function.  It never reads article bodies,
calls PubMed, or calculates embeddings.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "evidence.translation.job.v1"
STAGE = "translation_continuation"
EVIDENCE_SCHEMA_VERSION = "evidence.v1"


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _claim_key(record: dict[str, Any]) -> str:
    record_id = _as_text(record.get("record_id"))
    claim_id = _as_text(record.get("claim_id"))
    if not record_id or not claim_id:
        raise ValueError("Evidence record lacks record_id or claim_id")
    return f"{record_id}::{claim_id}"


def _retryable_translation(record: dict[str, Any]) -> bool:
    """Mirror the deployed Function's translation-only retry boundary."""

    if _as_text(record.get("schema_version")) != EVIDENCE_SCHEMA_VERSION:
        return False
    if _as_text(record.get("llm_status")).upper() != "OK":
        return False
    if _as_text(record.get("embedding_status")).upper() != "OK":
        return False
    status = _as_text(record.get("translation_status")).upper()
    if status in {"TIME_BUDGET", "PARTIAL"}:
        return True
    if status != "ERROR":
        return False
    error_text = " ".join(
        (_as_text(record.get("translation_error")), _as_text(record.get("evidence_error")))
    ).lower()
    return any(
        marker in error_text
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "time budget",
            "temporarily",
            "unavailable",
            "network",
            "quota",
            "rate limit",
            "recitation",
            "empty response",
            "omitted translation",
        )
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Evidence JSONL record {line_number} is not an object")
        _claim_key(record)
        records.append(record)
    if not records:
        raise ValueError("Evidence JSONL is empty")
    return records


def _manifest_metadata(path: Path) -> tuple[str, str, str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError("Manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "evidence.manifest.v1":
        raise ValueError("Manifest must use evidence.manifest.v1")
    parent_run_id = _as_text(manifest.get("run_id"))
    parent_evidence_object = _as_text(manifest.get("evidence_object"))
    source_claims_object = _as_text(manifest.get("source_object"))
    if not parent_run_id or not parent_evidence_object or not source_claims_object:
        raise ValueError("Manifest lacks run_id, evidence_object, or source_object")
    return parent_run_id, parent_evidence_object, source_claims_object


def build_job(
    evidence_records: list[dict[str, Any]],
    parent_run_id: str,
    parent_evidence_object: str,
    source_claims_object: str,
    chunk_size: int,
    max_attempts: int,
) -> dict[str, Any]:
    if not 1 <= chunk_size <= 20:
        raise ValueError("chunk_size must be between 1 and 20")
    if not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be between 1 and 5")
    retryable = [record for record in evidence_records if _retryable_translation(record)]
    if not retryable:
        raise ValueError("No retryable translation records were found in Evidence JSONL")
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "parent_run_id": parent_run_id,
        "parent_evidence_object": parent_evidence_object,
        "source_claims_object": source_claims_object,
        "sequence": 1,
        "chunk_size": chunk_size,
        "max_attempts": max_attempts,
        "items": [{"record": record, "attempt": 0} for record in retryable],
        "staging_objects": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--manifest-input",
        type=Path,
        help="Evidence manifest from which parent object metadata is read.",
    )
    parser.add_argument("--parent-run-id")
    parser.add_argument("--parent-evidence-object")
    parser.add_argument("--source-claims-object")
    parser.add_argument("--chunk-size", type=int, default=4)
    parser.add_argument("--max-attempts", type=int, default=3)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        records = _read_jsonl(args.evidence_input)
        if args.manifest_input:
            parent_run_id, parent_evidence_object, source_claims_object = _manifest_metadata(args.manifest_input)
        else:
            parent_run_id = _as_text(args.parent_run_id)
            parent_evidence_object = _as_text(args.parent_evidence_object)
            source_claims_object = _as_text(args.source_claims_object)
            if not parent_run_id or not parent_evidence_object or not source_claims_object:
                raise ValueError(
                    "Pass --manifest-input or all of --parent-run-id, --parent-evidence-object, and --source-claims-object"
                )
        job = build_job(
            records,
            parent_run_id,
            parent_evidence_object,
            source_claims_object,
            args.chunk_size,
            args.max_attempts,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "ready",
                "retryable_claims": len(job["items"]),
                "parent_run_id": parent_run_id,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
