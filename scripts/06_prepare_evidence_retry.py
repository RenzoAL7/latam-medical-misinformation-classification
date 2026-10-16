#!/usr/bin/env python3
"""Prepare a minimal Claims JSONL retry from transient Evidence failures.

The pipeline normally moves a whole Claims JSONL run to Evidence.  When a
small set of claims reaches a transient terminal state such as
``TRANSLATION_TIME_BUDGET``, this utility keeps only those exact claim IDs in a
new Claims JSONL object.  Upload that file below
``silver/claims/run_id=<retry>/claims.jsonl`` to trigger Evidence again without
re-extracting article bodies or duplicating successful claims.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,100}$")


def _read_jsonl(path_value: str | Path) -> list[dict[str, Any]]:
    path = Path(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"JSONL input does not exist: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL in {path} at line {line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"JSONL record in {path} at line {line_number} is not an object")
        records.append(record)
    return records


def _failed_claim_keys(
    evidence_records: Iterable[dict[str, Any]], error_code: str
) -> set[tuple[str, str]]:
    selected: set[tuple[str, str]] = set()
    for record in evidence_records:
        if str(record.get("schema_version", "")).strip() != "evidence.v1":
            raise ValueError("Evidence input must use evidence.v1 records.")
        if str(record.get("evidence_status", "")).strip().upper() != "ERROR":
            continue
        error = str(record.get("evidence_error", "")).strip()
        if error_code and error_code not in error:
            continue
        record_id = str(record.get("record_id", "")).strip()
        claim_id = str(record.get("claim_id", "")).strip()
        if record_id and claim_id:
            selected.add((record_id, claim_id))
    if not selected:
        raise ValueError("No matching failed Evidence claims were found.")
    return selected


def prepare_evidence_retry(
    claims_input: str | Path,
    evidence_input: str | Path,
    output: str | Path,
    retry_run_id: str,
    error_code: str = "",
) -> dict[str, Any]:
    """Write Claims v1 records limited to the selected transient failures."""

    if not RUN_ID_PATTERN.fullmatch(retry_run_id):
        raise ValueError("retry_run_id must use lowercase letters, digits, _ or -, with 3-101 characters.")
    selected = _failed_claim_keys(_read_jsonl(evidence_input), error_code)
    output_records: list[dict[str, Any]] = []
    found: set[tuple[str, str]] = set()
    for record in _read_jsonl(claims_input):
        if str(record.get("schema_version", "")).strip() != "claims.v1":
            raise ValueError("Claims input must use claims.v1 records.")
        if str(record.get("llm_status", "")).strip().upper() != "OK":
            continue
        record_id = str(record.get("record_id", "")).strip()
        claims = record.get("claims", [])
        if not isinstance(claims, list):
            raise ValueError(f"Claims record {record_id or '<missing>'} has a non-list claims field.")
        retry_claims = [
            dict(claim)
            for claim in claims
            if isinstance(claim, dict)
            and (record_id, str(claim.get("claim_id", "")).strip()) in selected
        ]
        found.update(
            (record_id, str(claim.get("claim_id", "")).strip()) for claim in retry_claims
        )
        if not retry_claims:
            continue
        retry_record = dict(record)
        retry_record["run_id"] = retry_run_id
        retry_record["claims"] = retry_claims
        retry_record["claims_count"] = len(retry_claims)
        retry_record["claims_truncated"] = False
        retry_record["retry_prepared_at"] = datetime.now(timezone.utc).isoformat()
        retry_record["retry_reason"] = error_code or "EVIDENCE_ERROR"
        output_records.append(retry_record)

    missing = sorted(selected - found)
    if missing:
        preview = ", ".join(f"{record_id}::{claim_id}" for record_id, claim_id in missing[:5])
        raise ValueError(f"Failed Evidence claim IDs are absent from Claims input: {preview}")

    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in output_records),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "retry_run_id": retry_run_id,
        "claims_selected": len(selected),
        "articles_selected": len(output_records),
        "output": str(destination),
        "error_code": error_code or "all_error_claims",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims-input", required=True, help="Original Claims v1 JSONL file.")
    parser.add_argument("--evidence-input", required=True, help="Evidence v1 JSONL that contains failed claims.")
    parser.add_argument("--output", required=True, help="Generated retry Claims v1 JSONL path.")
    parser.add_argument("--retry-run-id", required=True, help="Run identifier for the retry Claims JSONL.")
    parser.add_argument(
        "--error-code",
        default="",
        help="Optional evidence_error substring to retry, e.g. TRANSLATION_TIME_BUDGET.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = prepare_evidence_retry(
            args.claims_input,
            args.evidence_input,
            args.output,
            args.retry_run_id,
            args.error_code,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
