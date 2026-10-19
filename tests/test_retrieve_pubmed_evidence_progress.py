import importlib.util
import io
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


def test_retrieval_rejections_are_written_under_translate() -> None:
    module = _load_module()

    assert module._retrieval_rejected_object_name(
        "silver/rejected", "run_123"
    ) == "silver/rejected/translate/translate_run_123.jsonl"


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
    assert "best_cosine_similarity" not in module.OUTPUT_FIELDS


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
    assert row["best_embedding_similarity"]
    assert "best_cosine_similarity" not in row


def test_translation_stage_consumes_flat_translate_jsonl_and_publishes_evidence(monkeypatch) -> None:
    module = _load_module()

    class FakeStorage:
        def __init__(self, objects: dict[str, bytes]) -> None:
            self.objects = objects

        def get_object(self, namespace_name, bucket_name, object_name):
            return types.SimpleNamespace(data=types.SimpleNamespace(content=io.BytesIO(self.objects[object_name])))

        def put_object(self, namespace_name, bucket_name, object_name, put_object_body, content_type):
            self.objects[object_name] = put_object_body.read()

    class FakeResponse:
        def __init__(self, _ctx, response_data, status_code=200, headers=None) -> None:
            self.response_data = response_data
            self.status_code = status_code
            self.headers = headers or {}

    def record(claim_id: str, pmid: str) -> dict:
        return {
            "schema_version": "evidence.v1",
            "run_id": "run_parent",
            "evidence_run_id": "run_parent",
            "record_id": "news_001",
            "claim_id": claim_id,
            "article": {"title": "Noticia médica", "country": "pe"},
            "body": "Cuerpo completo de la noticia.",
            "llm_status": "OK",
            "claim_text": f"Claim {claim_id}",
            "claim_text_en": f"Claim {claim_id}",
            "pubmed_query_en": f"query {claim_id}",
            "claim_type": "tratamiento",
            "evidence_status": "OK",
            "evidence_error": "",
            "pubmed_result_count": 1,
            "best_pmid": pmid,
            "best_embedding_similarity": "0.7",
            "embedding_status": "OK",
            "embedding_model": "cohere.embed-v4.0",
            "translation_status": "PENDING",
            "translation_error": "",
            "candidates": [
                {
                    "pmid": pmid,
                    "title": "Clinical article",
                    "abstract_excerpt": "A biomedical abstract with usable evidence.",
                    "pubmed_rank": 1,
                    "embedding_similarity": 0.7,
                }
            ],
        }

    translate_object = "silver/translate/translate_run_parent.jsonl"
    first = record("c01", "111")
    second = record("c02", "222")
    storage = FakeStorage(
        {
            translate_object: (json.dumps(first) + "\n" + json.dumps(second) + "\n").encode("utf-8"),
        }
    )
    monkeypatch.setattr(module, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(module, "_vault_secret_value", lambda _secret: "translation-key")
    monkeypatch.setattr(module, "_search_pubmed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no PubMed")))
    monkeypatch.setattr(module, "_apply_embeddings", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no embed")))
    monkeypatch.setattr(
        module,
        "_translate_batch",
        lambda _key, _model, rows, _tokens, _timeout: {
            module._claim_identity(row): {
                candidate["pmid"]: "Un abstract biomédico en español."
                for candidate in module._candidate_list(row)
            }
            for row in rows
        },
    )
    monkeypatch.setattr(module.response, "Response", FakeResponse)
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "ns")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("GOOGLE_TRANSLATION_SECRET_OCIDS", "secret")
    monkeypatch.setenv("PIPELINE_MODE", "translation")
    monkeypatch.setenv("SILVER_TRANSLATE_PREFIX", "silver/translate")
    monkeypatch.setenv("SILVER_EVIDENCE_PREFIX", "silver/evidence")
    monkeypatch.setenv("SILVER_REJECTED_PREFIX", "silver/rejected")
    monkeypatch.setenv("TRANSLATION_MAX_SECONDS", "60")
    monkeypatch.setenv("TRANSLATION_TIME_BUFFER", "1")

    event = {"data": {"resourceName": translate_object, "additionalDetails": {"bucketName": "bucket"}}}
    response = module.handler(None, io.BytesIO(json.dumps(event).encode("utf-8")))
    result = json.loads(response.response_data)
    assert result["status"] == "complete"
    final_records = [
        json.loads(line)
        for line in storage.objects[result["object_name"]].decode("utf-8").splitlines()
    ]
    assert [item["claim_id"] for item in final_records] == ["c01", "c02"]
    assert all(item["evidence_status"] == "OK" for item in final_records)
    assert all(item["translation_status"] == "OK" for item in final_records)
    assert all(item["candidates"][0]["abstract_es"] for item in final_records)
    assert all(item["body"] == "Cuerpo completo de la noticia." for item in final_records)
    assert result["manifest_object"] == "silver/evidence/evidence_run_parent.manifest.json"
    assert not any(name.startswith("silver/rejected/") for name in storage.objects)


def test_translation_failure_is_written_to_rejected(monkeypatch) -> None:
    module = _load_module()

    class FakeStorage:
        def __init__(self, objects: dict[str, bytes]) -> None:
            self.objects = objects

        def get_object(self, namespace_name, bucket_name, object_name):
            return types.SimpleNamespace(data=types.SimpleNamespace(content=io.BytesIO(self.objects[object_name])))

        def put_object(self, namespace_name, bucket_name, object_name, put_object_body, content_type):
            self.objects[object_name] = put_object_body.read()

    class FakeResponse:
        def __init__(self, _ctx, response_data, status_code=200, headers=None) -> None:
            self.response_data = response_data
            self.status_code = status_code
            self.headers = headers or {}

    translate_object = "silver/translate/translate_run_failed.jsonl"
    record = {
        "schema_version": "evidence.v1",
        "run_id": "run_failed",
        "evidence_run_id": "run_failed",
        "record_id": "news_001",
        "claim_id": "c01",
        "article": {"title": "Noticia"},
        "body": "Cuerpo",
        "llm_status": "OK",
        "claim_text": "Claim",
        "claim_text_en": "Claim",
        "pubmed_query_en": "claim",
        "evidence_status": "OK",
        "embedding_status": "OK",
        "translation_status": "PENDING",
        "candidates": [{"pmid": "111", "title": "Study", "abstract_excerpt": "Abstract"}],
    }
    storage = FakeStorage({translate_object: (json.dumps(record) + "\n").encode("utf-8")})
    monkeypatch.setattr(module, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(module, "_vault_secret_value", lambda _secret: "translation-key")
    monkeypatch.setattr(module, "_translate_batch", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")))
    monkeypatch.setattr(module, "_translate_row_individually", lambda *args, **kwargs: ({}, ["provider down"], 1, set()))
    monkeypatch.setattr(module.response, "Response", FakeResponse)
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "ns")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("PIPELINE_MODE", "translation")
    monkeypatch.setenv("GOOGLE_TRANSLATION_SECRET_OCIDS", "secret")
    monkeypatch.setenv("TRANSLATION_MAX_SECONDS", "60")
    monkeypatch.setenv("TRANSLATION_TIME_BUFFER", "1")

    event = {"data": {"resourceName": translate_object, "additionalDetails": {"bucketName": "bucket"}}}
    response = module.handler(None, io.BytesIO(json.dumps(event).encode("utf-8")))
    result = json.loads(response.response_data)
    assert result["status"] == "failed"
    rejected_translate = "silver/rejected/translate/translate_run_failed.jsonl"
    rejected_evidence = "silver/rejected/evidence/evidence_run_failed.jsonl"
    assert rejected_translate in storage.objects
    assert rejected_evidence in storage.objects
    rejected_record = json.loads(storage.objects[rejected_translate].decode("utf-8").strip())
    assert rejected_record["translation_status"] == "ERROR"
    assert rejected_record["evidence_status"] == "ERROR"
