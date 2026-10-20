from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "labelstudio" / "import_evidence.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("import_evidence", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_task_payload_does_not_duplicate_full_evidence_text() -> None:
    row = {
        "record_id": "r1",
        "country": "pe",
        "source_name": "source",
        "topic": "health",
        "canonical_url": "https://example.test/r1",
        "title": "Title",
        "claim_text": "Claim",
        "claim_text_en": "Claim",
        "pubmed_query_en": "Claim",
        "evidence_status": "OK",
        "best_pmid": "123",
        "best_embedding_similarity": "0.8",
        "best_tfidf_similarity": "0.4",
        "ranking_method": "oci_cohere_embed_v4",
        "embedding_model": "cohere.embed-v4.0",
        "evidence_run_id": "run-1",
        "pubmed_results_json": json.dumps(
            [
                {
                    "pmid": "123",
                    "title": "Paper",
                    "abstract_es": "Abstract",
                    "embedding_similarity": 0.8,
                }
            ]
        ),
    }

    data = module._task_from_row(row)["data"]

    assert "evidence_text" not in data
    assert data["topic"] == "health"
    assert data["best_embedding_similarity"] == "0.8"
    assert "best_cosine_similarity" not in data
    assert data["evidence_primary"] != "No hay abstracts disponibles."
    assert data["evidence_count"] == "1"


def test_main_writes_manifest_after_import(monkeypatch, tmp_path) -> None:
    evidence_path = tmp_path / "evidence.jsonl"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "evidence.v1",
                "record_id": "r1",
                "claim_id": "c1",
                "llm_status": "OK",
                "evidence_status": "NO_RESULTS",
                "embedding_status": "SKIPPED_NO_EVIDENCE",
                "translation_status": "SKIPPED_NO_EVIDENCE",
                "claim_text": "Claim",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.csv"
    requests = []

    monkeypatch.setattr(module, "_list_tasks", lambda *args, **kwargs: [])

    def fake_request(*args, **kwargs):
        requests.append((args, kwargs))
        return {"task_count": 1}

    monkeypatch.setattr(module, "_request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_evidence.py",
            "--input",
            str(evidence_path),
            "--project-id",
            "7",
            "--token",
            "test-token",
            "--manifest",
            str(manifest_path),
        ],
    )

    assert module.main() == 0
    assert requests
    assert manifest_path.read_text(encoding="utf-8").startswith("claim_key,record_id,claim_id,")
