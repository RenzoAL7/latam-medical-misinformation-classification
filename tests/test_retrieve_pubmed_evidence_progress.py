import importlib.util
import json
import sys
import types
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "oci"
    / "functions"
    / "retrieve_pubmed_evidence"
    / "func.py"
)


def _load_module() -> types.ModuleType:
    fdk = types.ModuleType("fdk")
    fdk.response = types.SimpleNamespace(Response=object)
    previous_fdk = sys.modules.get("fdk")
    sys.modules["fdk"] = fdk
    try:
        spec = importlib.util.spec_from_file_location("retrieve_pubmed_evidence_func", MODULE_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_fdk is None:
            sys.modules.pop("fdk", None)
        else:
            sys.modules["fdk"] = previous_fdk


def test_no_new_claims_stays_in_event_mode() -> None:
    module = _load_module()

    selected, metadata = module._select_claims([], set(), 0)
    assert selected == []
    assert metadata["selected"] == 0


def test_evidence_rejection_reason_is_explicit() -> None:
    module = _load_module()

    assert module._evidence_rejection_reason({"evidence_status": "NO_RESULTS"}) == "PUBMED_NO_RESULTS"


def test_evidence_output_carries_training_text_and_audit_fields() -> None:
    module = _load_module()
    row = module._base_output_row(
        {
            "record_id": "record-1",
            "country": "pe",
            "source_name": "Fuente",
            "topic": "health",
            "title": "Título",
            "body": "Texto completo de la noticia",
            "extraction_status": "OK",
            "claim_text": "Claim",
            "claim_text_en": "Claim",
            "pubmed_query_en": "claim",
        },
        "run-1",
        "2026-10-11T00:00:00Z",
    )

    assert row["body"] == "Texto completo de la noticia"
    assert row["topic"] == "health"
    assert row["extraction_status"] == "OK"
    assert set(("body", "topic", "extraction_status")).issubset(module.OUTPUT_FIELDS)


def test_embedding_ranking_sets_best_pmid_even_before_translation(monkeypatch) -> None:
    module = _load_module()
    row = {
        "record_id": "claim-1",
        "evidence_status": "OK",
        "claim_text": "claim",
        "claim_text_en": "claim",
        "pubmed_results_json": json.dumps(
            [
                {"pmid": "low", "title": "low", "abstract_excerpt": "low", "pubmed_rank": 1},
                {"pmid": "high", "title": "high", "abstract_excerpt": "high", "pubmed_rank": 2},
            ]
        ),
        "best_pmid": "",
    }

    def fake_embed(*args, **kwargs):
        texts = args[1]
        input_type = args[4]
        return [[1.0, 0.0]] if input_type == "SEARCH_QUERY" else [[0.1, 0.9], [0.9, 0.1]][: len(texts)]

    monkeypatch.setattr(module, "_embed_in_batches", fake_embed)
    result = module._apply_embeddings(
        [row], object(), "compartment", "model", 512, 6000, 30, float("inf"), 96, 100000
    )

    assert result["status"] == "ok"
    assert row["best_pmid"] == "high"
