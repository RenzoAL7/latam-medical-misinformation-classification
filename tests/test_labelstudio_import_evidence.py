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
        "best_cosine_similarity": "0.8",
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
    assert data["best_cosine_similarity"] == "0.8"
    assert data["evidence_primary"] != "No hay abstracts disponibles."
    assert data["evidence_count"] == "1"
