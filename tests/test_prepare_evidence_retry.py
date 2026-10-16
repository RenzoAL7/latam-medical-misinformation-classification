from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "06_prepare_evidence_retry.py"
SPEC = importlib.util.spec_from_file_location("prepare_evidence_retry", SCRIPT)
assert SPEC and SPEC.loader
RETRY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RETRY)


def test_prepare_evidence_retry_selects_only_requested_failed_claims(tmp_path: Path) -> None:
    claims = tmp_path / "claims.jsonl"
    evidence = tmp_path / "evidence.jsonl"
    output = tmp_path / "retry" / "claims.jsonl"
    claims.write_text(
        json.dumps(
            {
                "schema_version": "claims.v1",
                "run_id": "original",
                "record_id": "news_001",
                "llm_status": "OK",
                "claims": [
                    {"claim_id": "news_001_c01", "claim_text": "Primero", "is_claim_eligible": True},
                    {"claim_id": "news_001_c02", "claim_text": "Segundo", "is_claim_eligible": True},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    evidence.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": "evidence.v1",
                        "record_id": "news_001",
                        "claim_id": "news_001_c01",
                        "evidence_status": "OK",
                        "evidence_error": "",
                    }
                ),
                json.dumps(
                    {
                        "schema_version": "evidence.v1",
                        "record_id": "news_001",
                        "claim_id": "news_001_c02",
                        "evidence_status": "ERROR",
                        "evidence_error": "TRANSLATION_TIME_BUDGET",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = RETRY.prepare_evidence_retry(
        claims,
        evidence,
        output,
        "retry_translation_001",
        "TRANSLATION_TIME_BUDGET",
    )

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert summary["claims_selected"] == 1
    assert summary["articles_selected"] == 1
    assert records[0]["run_id"] == "retry_translation_001"
    assert [claim["claim_id"] for claim in records[0]["claims"]] == ["news_001_c02"]
