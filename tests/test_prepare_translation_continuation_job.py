from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "07_prepare_translation_continuation_job.py"
SPEC = importlib.util.spec_from_file_location("prepare_translation_continuation_job", SCRIPT)
assert SPEC and SPEC.loader
CONTINUATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTINUATION)


def test_translation_job_keeps_only_retryable_translation_records() -> None:
    retryable = {
        "schema_version": "evidence.v1",
        "record_id": "news_001",
        "claim_id": "c01",
        "llm_status": "OK",
        "embedding_status": "OK",
        "evidence_status": "ERROR",
        "translation_status": "TIME_BUDGET",
        "translation_error": "Translation time budget reached",
        "candidates": [{"pmid": "123", "abstract_excerpt": "Abstract"}],
    }
    not_retryable = {
        "schema_version": "evidence.v1",
        "record_id": "news_001",
        "claim_id": "c02",
        "llm_status": "OK",
        "embedding_status": "ERROR",
        "evidence_status": "ERROR",
        "translation_status": "ERROR",
        "translation_error": "Gemini translation HTTP 503",
    }

    job = CONTINUATION.build_job(
        [retryable, not_retryable],
        "run_parent",
        "silver/evidence/run_id=run_parent/evidence.jsonl",
        "silver/claims/run_id=claims_parent/claims.jsonl",
        4,
        3,
    )

    assert job["schema_version"] == "evidence.translation.job.v1"
    assert job["chunk_size"] == 4
    assert [item["record"]["claim_id"] for item in job["items"]] == ["c01"]
