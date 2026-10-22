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


def test_default_mode_dispatches_job_events_to_the_same_function(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.delenv("PIPELINE_MODE", raising=False)
    monkeypatch.setenv("SILVER_RETRIEVAL_JOB_PREFIX", "silver/evidence-retrieval-jobs")
    monkeypatch.setenv("SILVER_TRANSLATION_JOB_PREFIX", "silver/evidence-translation-jobs")
    calls: list[str] = []

    def fake_retrieval(ctx, data):
        calls.append("retrieval")
        assert ctx == "ctx"
        assert data.tell() == 0
        return "retrieval-response"

    def fake_translation(ctx, data):
        calls.append("translation")
        assert ctx == "ctx"
        assert data.tell() == 0
        return "translation-response"

    monkeypatch.setattr(module, "_handler_retrieval_continuation", fake_retrieval)
    monkeypatch.setattr(module, "_handler_translation_continuation", fake_translation)

    retrieval_event = {
        "data": {
            "resourceName": "silver/evidence-retrieval-jobs/run_id=parent/job-0001.job.json"
        }
    }
    translation_event = {
        "data": {
            "resourceName": "silver/evidence-translation-jobs/run_id=parent/job-0001.job.json"
        }
    }

    assert (
        module.handler("ctx", io.BytesIO(json.dumps(retrieval_event).encode("utf-8")))
        == "retrieval-response"
    )
    assert (
        module.handler("ctx", io.BytesIO(json.dumps(translation_event).encode("utf-8")))
        == "translation-response"
    )
    assert calls == ["retrieval", "translation"]


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


def test_translation_continuation_chains_without_refetching_pubmed_or_embeddings(monkeypatch) -> None:
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
            "llm_status": "OK",
            "claim_text": f"Claim {claim_id}",
            "claim_text_en": f"Claim {claim_id}",
            "pubmed_query_en": f"query {claim_id}",
            "claim_type": "tratamiento",
            "evidence_status": "ERROR",
            "evidence_error": "TRANSLATION_TIME_BUDGET",
            "pubmed_result_count": 1,
            "best_pmid": pmid,
            "best_embedding_similarity": "0.7",
            "embedding_status": "OK",
            "embedding_model": "cohere.embed-v4.0",
            "translation_status": "TIME_BUDGET",
            "translation_error": "Translation time budget reached",
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

    parent_object = "silver/evidence/run_id=run_parent/evidence.jsonl"
    first = record("c01", "111")
    second = record("c02", "222")
    job_prefix = "silver/evidence-translation-jobs"
    job_object = module._translation_job_object_name(job_prefix, "run_parent", 1)
    job = module._translation_job_document(
        "run_parent",
        parent_object,
        "silver/claims/run_id=claims_parent/claims.jsonl",
        1,
        1,
        3,
        [{"record": first, "attempt": 0}, {"record": second, "attempt": 0}],
        [],
    )
    storage = FakeStorage(
        {
            parent_object: (json.dumps(first) + "\n" + json.dumps(second) + "\n").encode("utf-8"),
            job_object: json.dumps(job).encode("utf-8"),
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
    monkeypatch.setenv("SILVER_TRANSLATION_JOB_PREFIX", job_prefix)
    monkeypatch.setenv("TRANSLATION_CONTINUATION_MAX_SECONDS", "60")
    monkeypatch.setenv("TRANSLATION_CONTINUATION_TIME_BUFFER", "1")

    first_event = {"data": {"resourceName": job_object, "additionalDetails": {"bucketName": "bucket"}}}
    first_response = module._handler_translation_continuation(
        None, io.BytesIO(json.dumps(first_event).encode("utf-8"))
    )
    first_result = json.loads(first_response.response_data)
    assert first_result["status"] == "continued"
    next_job_object = first_result["next_job_object"]
    assert next_job_object in storage.objects

    duplicate_response = module._handler_translation_continuation(
        None, io.BytesIO(json.dumps(first_event).encode("utf-8"))
    )
    assert json.loads(duplicate_response.response_data)["status"] == "already_complete"

    second_event = {"data": {"resourceName": next_job_object, "additionalDetails": {"bucketName": "bucket"}}}
    second_response = module._handler_translation_continuation(
        None, io.BytesIO(json.dumps(second_event).encode("utf-8"))
    )
    second_result = json.loads(second_response.response_data)
    assert second_result["status"] == "complete"
    final_records = [
        json.loads(line)
        for line in storage.objects[second_result["object_name"]].decode("utf-8").splitlines()
    ]
    assert [item["claim_id"] for item in final_records] == ["c01", "c02"]
    assert all(item["evidence_status"] == "OK" for item in final_records)
    assert all(item["translation_status"] == "OK" for item in final_records)
    assert all(item["candidates"][0]["abstract_es"] for item in final_records)


def test_retrieval_continuation_serializes_large_claim_fanout(monkeypatch) -> None:
    """A parent keeps every claim while only one bounded tranche hits PubMed."""

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

    body_object = "silver/body/body_parent.csv"
    claims_object = "silver/claims/run_id=claims_parent/claims.jsonl"
    claim_ids = ["news_001_c01", "news_002_c01", "news_003_c01"]
    claims_records = []
    for number, claim_id in enumerate(claim_ids, start=1):
        record_id = f"news_{number:03d}"
        claims_records.append(
            {
                "schema_version": "claims.v1",
                "record_id": record_id,
                "source_body_object": body_object,
                "llm_status": "OK",
                "article": {"title": f"Noticia {number}", "country": "pe"},
                "claims": [
                    {
                        "claim_id": claim_id,
                        "claim_index": 1,
                        "claim_text": f"El tratamiento {number} mejora el resultado.",
                        "claim_text_en": f"Treatment {number} improves the outcome.",
                        "pubmed_query_en": f"treatment {number} outcome",
                        "query_status": "OK",
                        "claim_type": "tratamiento",
                        "is_claim_eligible": True,
                    }
                ],
            }
        )
    body_rows = [
        "record_id,country,source_name,canonical_url,title,topic,body,extraction_status",
        *[
            f"news_{number:03d},pe,Fuente,https://example.test/{number},Noticia {number},health,Texto médico {number},OK"
            for number in range(1, 4)
        ],
    ]
    storage = FakeStorage(
        {
            body_object: ("\n".join(body_rows) + "\n").encode("utf-8"),
            claims_object: (
                "".join(json.dumps(record) + "\n" for record in claims_records)
            ).encode("utf-8"),
        }
    )

    monkeypatch.setattr(module, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(module, "_vault_secret_value", lambda _secret: "translation-key")
    monkeypatch.setattr(module, "_search_pubmed", lambda query, *_args: [f"pmid-{query.split()[1]}"])
    monkeypatch.setattr(
        module,
        "_fetch_pubmed",
        lambda pmids, *_args: {
            pmid: {
                "pmid": pmid,
                "title": "Clinical study",
                "abstract": "A biomedical abstract with usable evidence.",
                "journal": "Journal",
                "publication_date": "2026",
                "doi": "",
                "publication_types": ["Clinical Trial"],
            }
            for pmid in pmids
        },
    )
    monkeypatch.setattr(module, "_generative_ai_client", lambda *_args: object())
    monkeypatch.setattr(
        module,
        "_embed_in_batches",
        lambda _client, texts, *_args: [[1.0, 0.0] for _ in texts],
    )
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
    monkeypatch.setenv("PIPELINE_SCHEMA", "v1")
    monkeypatch.setenv("PIPELINE_MODE", "retrieve")
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "ns")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("GENAI_COMPARTMENT_ID", "compartment")
    monkeypatch.setenv("PUBMED_EMAIL", "research@example.test")
    monkeypatch.setenv("GOOGLE_TRANSLATION_SECRET_OCIDS", "secret")
    monkeypatch.setenv("EVIDENCE_MAX_SECONDS", "60")
    monkeypatch.setenv("EVIDENCE_TIME_BUFFER", "1")
    monkeypatch.setenv("RETRIEVAL_CONTINUATION_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_CONTINUATION_MAX_CLAIMS", "1")
    monkeypatch.setenv("RETRIEVAL_CONTINUATION_MAX_SECONDS", "60")
    monkeypatch.setenv("RETRIEVAL_CONTINUATION_TIME_BUFFER", "1")
    monkeypatch.setenv("SILVER_RETRIEVAL_JOB_PREFIX", "silver/evidence-retrieval-jobs")
    monkeypatch.setenv("SILVER_RETRIEVAL_STAGE_PREFIX", "silver/evidence-retrieval-stage")

    parent_event = {"data": {"resourceName": claims_object, "additionalDetails": {"bucketName": "bucket"}}}
    parent_response = module.handler(None, io.BytesIO(json.dumps(parent_event).encode("utf-8")))
    parent_result = json.loads(parent_response.response_data)
    assert parent_result["status"] == "partial"
    assert parent_result["continuation"]["stage"] == "retrieval_continuation"
    parent_records = [
        json.loads(line)
        for line in storage.objects[parent_result["object_name"]].decode("utf-8").splitlines()
    ]
    assert len(parent_records) == 3
    assert sum(record["evidence_status"] == "OK" for record in parent_records) == 1
    assert sum(
        record["evidence_error"] == "EVIDENCE_RETRIEVAL_CONTINUATION_QUEUED"
        for record in parent_records
    ) == 2

    monkeypatch.setenv("PIPELINE_MODE", "retrieval_continuation")
    first_job = parent_result["continuation"]["job_object"]
    first_event = {"data": {"resourceName": first_job, "additionalDetails": {"bucketName": "bucket"}}}
    first_response = module.handler(None, io.BytesIO(json.dumps(first_event).encode("utf-8")))
    first_result = json.loads(first_response.response_data)
    assert first_result["status"] == "continued"
    duplicate_response = module.handler(None, io.BytesIO(json.dumps(first_event).encode("utf-8")))
    assert json.loads(duplicate_response.response_data)["status"] == "already_complete"

    second_event = {
        "data": {
            "resourceName": first_result["next_job_object"],
            "additionalDetails": {"bucketName": "bucket"},
        }
    }
    second_response = module.handler(None, io.BytesIO(json.dumps(second_event).encode("utf-8")))
    second_result = json.loads(second_response.response_data)
    assert second_result["status"] == "complete"
    final_records = [
        json.loads(line)
        for line in storage.objects[second_result["object_name"]].decode("utf-8").splitlines()
    ]
    assert [record["claim_id"] for record in final_records] == claim_ids
    assert all(record["evidence_status"] == "OK" for record in final_records)
    assert all(record["embedding_status"] == "OK" for record in final_records)
    assert all(record["translation_status"] == "OK" for record in final_records)
