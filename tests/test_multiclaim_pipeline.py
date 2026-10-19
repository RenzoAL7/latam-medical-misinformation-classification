from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _load_function(path: Path, module_name: str) -> types.ModuleType:
    fdk = types.ModuleType("fdk")
    fdk.response = types.SimpleNamespace(Response=object)
    previous = sys.modules.get("fdk")
    sys.modules["fdk"] = fdk
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("fdk", None)
        else:
            sys.modules["fdk"] = previous


EXTRACT = _load_function(ROOT / "oci/functions/extract_claims/func.py", "extract_claims_multiclaim")
EVIDENCE = _load_function(
    ROOT / "oci/functions/retrieve_pubmed_evidence/func.py", "retrieve_evidence_multiclaim"
)


def test_claim_extraction_preserves_all_distinct_claims_without_default_cap() -> None:
    claims, truncated, reason, medical = EXTRACT._normalize_multiclaim_item(
        {
            "is_medical": True,
            "reason": "Contiene dos afirmaciones médicas comprobables.",
            "claims": [
                {
                    "claim_text": "El tratamiento X cura la enfermedad Y.",
                    "claim_text_en": "Treatment X cures disease Y.",
                    "pubmed_query_en": "treatment X disease Y",
                    "claim_type": "tratamiento",
                    "is_claim_eligible": True,
                },
                {
                    "claim_text": "La enfermedad Y afecta principalmente a adultos.",
                    "claim_text_en": "Disease Y mainly affects adults.",
                    "pubmed_query_en": "disease Y adults",
                    "claim_type": "riesgo",
                    "is_claim_eligible": True,
                },
            ],
        },
        "news_001",
        max_claims=0,
    )

    assert medical is True
    assert truncated is False
    assert "dos afirmaciones" in reason
    assert [claim["claim_index"] for claim in claims] == [1, 2]
    assert len({claim["claim_id"] for claim in claims}) == 2
    assert all(claim["claim_status"] == "PENDING_EVIDENCE" for claim in claims)


def test_multiclaim_prompt_requests_high_recall_without_rewriting_polarity() -> None:
    prompt = EXTRACT._multiclaim_prompt(
        [
            {
                "record_id": "news_001",
                "title": "El tratamiento no garantiza la cura",
                "subtitle_or_bajada": "Declaración de una especialista",
                "body": "El artículo contiene una afirmación sanitaria.",
            }
        ],
        max_body_chars=24000,
    )

    assert "pasada de cobertura" in prompt
    assert "no descartes una afirmación" in prompt.lower()
    assert "no conviertas" in prompt.lower()
    assert '"claims": []' in prompt


def test_claim_coverage_metrics_separate_medical_zero_claims_from_errors() -> None:
    metrics = EXTRACT._claims_coverage_metrics(
        [
            {"llm_status": "OK", "is_medical": True, "claims_count": 2},
            {"llm_status": "OK", "is_medical": True, "claims_count": 0},
            {"llm_status": "OK", "is_medical": False, "claims_count": 0},
            {"llm_status": "ERROR", "is_medical": None, "claims_count": 0},
        ]
    )

    assert metrics == {
        "medical_articles_with_claims": 1,
        "medical_articles_without_claims": 1,
        "nonmedical_articles_without_claims": 1,
    }


def test_controlled_body_retry_suppresses_only_same_record_exact_claim_texts() -> None:
    records = [
        {
            "record_id": "news_001",
            "llm_status": "OK",
            "claims_count": 2,
            "claims": [
                {"claim_text": "La vacuna reduce el riesgo de enfermedad."},
                {"claim_text": "El seguimiento posterior es necesario."},
            ],
        },
        {
            "record_id": "news_002",
            "llm_status": "OK",
            "claims_count": 1,
            "claims": [{"claim_text": "La vacuna reduce el riesgo de enfermedad."}],
        },
    ]

    stats = EXTRACT._suppress_existing_claim_texts(
        records,
        {"news_001": {"  LA VACUNA REDUCE EL RIESGO DE ENFERMEDAD.  "}},
    )

    assert stats == {"claims_suppressed": 1, "articles_with_suppressed_claims": 1}
    assert records[0]["claims_count"] == 1
    assert [claim["claim_text"] for claim in records[0]["claims"]] == [
        "El seguimiento posterior es necesario."
    ]
    assert records[1]["claims_count"] == 1


def test_controlled_body_retry_reads_prior_claims_only_for_the_same_record() -> None:
    class FakeStorage:
        def __init__(self) -> None:
            self.objects = {
                "silver/claims/run_id=old/claims.jsonl": (
                    json.dumps(
                        {
                            "schema_version": "claims.v1",
                            "record_id": "news_001",
                            "llm_status": "OK",
                            "claims": [{"claim_text": "Una afirmación previa."}],
                        }
                    )
                    + "\n"
                    + json.dumps(
                        {
                            "schema_version": "claims.v1",
                            "record_id": "news_002",
                            "llm_status": "OK",
                            "claims": [{"claim_text": "No debe compararse."}],
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            }

        def list_objects(self, **_kwargs):
            return types.SimpleNamespace(
                data=types.SimpleNamespace(
                    objects=[types.SimpleNamespace(name=name) for name in self.objects],
                    next_start_with=None,
                )
            )

        def get_object(self, namespace_name, bucket_name, object_name):
            return types.SimpleNamespace(
                data=types.SimpleNamespace(content=io.BytesIO(self.objects[object_name]))
            )

    existing = EXTRACT._existing_v1_claim_texts(
        FakeStorage(),
        "namespace",
        "bucket",
        "silver/claims",
        {"news_001"},
    )

    assert existing == {"news_001": {"una afirmación previa."}}


def test_v1_claim_handler_recovers_a_malformed_batch_by_retrying_singletons(monkeypatch) -> None:
    class FakeStorage:
        def __init__(self, objects: dict[str, bytes]) -> None:
            self.objects = objects

        def get_object(self, namespace_name, bucket_name, object_name):
            return types.SimpleNamespace(data=types.SimpleNamespace(content=io.BytesIO(self.objects[object_name])))

        def put_object(self, namespace_name, bucket_name, object_name, put_object_body, content_type):
            self.objects[object_name] = put_object_body.read()

    class FakeResponse:
        def __init__(self, _ctx, response_data, status_code=200, headers=None):
            self.response_data = response_data
            self.status_code = status_code
            self.headers = headers or {}

    body_object = "silver/body/body_run.csv"
    storage = FakeStorage(
        {
            body_object: (
                "record_id,country,source_name,canonical_url,title,topic,body,extraction_status\n"
                "news_001,pe,Fuente,https://example.test/1,Primera,health,El tratamiento A mejora B.,OK\n"
                "news_002,pe,Fuente,https://example.test/2,Segunda,health,El tratamiento C mejora D.,OK\n"
            ).encode("utf-8")
        }
    )
    call_sizes: list[int] = []

    def fake_gemini(*args, **_kwargs):
        record_ids = re.findall(r"record_id \(copia exactamente\): ([^\n]+)", args[2])
        call_sizes.append(len(record_ids))
        if len(record_ids) > 1:
            return {"text": '{"items": [}' }
        record_id = record_ids[0]
        valid = {
            "items": [
                {
                    "record_id": record_id,
                    "is_medical": True,
                    "reason": "Una afirmación médica verificable.",
                    "claims": [
                        {
                            "claim_text": f"El tratamiento de {record_id} mejora un resultado.",
                            "claim_text_en": f"Treatment for {record_id} improves an outcome.",
                            "pubmed_query_en": "treatment outcome",
                            "claim_type": "tratamiento",
                            "is_claim_eligible": True,
                        }
                    ],
                }
            ]
        }
        return {"text": json.dumps(valid)}

    monkeypatch.setattr(EXTRACT, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(EXTRACT, "_vault_secret_value", lambda _secret: "claim-key")
    monkeypatch.setattr(EXTRACT, "_google_gemini_request", fake_gemini)
    monkeypatch.setattr(EXTRACT.response, "Response", FakeResponse)
    monkeypatch.setenv("PIPELINE_SCHEMA", "v1")
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "ns")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("LLM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_GEMINI_SECRET_OCIDS", "secret")
    monkeypatch.setenv("LLM_MAX_ROWS", "0")
    monkeypatch.setenv("LLM_REQUEST_BATCH_SIZE", "2")
    monkeypatch.setenv("LLM_MAX_SECONDS", "60")
    monkeypatch.setenv("LLM_TIME_BUFFER", "1")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    event = {"data": {"resourceName": body_object, "additionalDetails": {"bucketName": "bucket"}}}

    result = EXTRACT.handler(None, io.BytesIO(json.dumps(event).encode("utf-8")))
    payload = json.loads(result.response_data)
    records = [json.loads(line) for line in storage.objects[payload["object_name"]].decode("utf-8").splitlines()]
    manifest = json.loads(storage.objects[payload["manifest_object"]].decode("utf-8"))

    assert payload["status"] == "complete"
    assert call_sizes == [2, 1, 1]
    assert [record["llm_status"] for record in records] == ["OK", "OK"]
    assert manifest["recovery"] == {
        "malformed_batches_split": 1,
        "articles_retried_individually": 2,
    }


def test_multiclaim_batch_recovery_recognizes_schema_and_json_errors() -> None:
    assert EXTRACT._should_split_multiclaim_batch_error(
        RuntimeError("LLM call failed: ValueError: LLM item must contain a claims array of objects")
    )
    assert EXTRACT._should_split_multiclaim_batch_error(
        RuntimeError("LLM call failed: JSONDecodeError: Expecting property name")
    )
    assert not EXTRACT._should_split_multiclaim_batch_error(
        RuntimeError("Gemini HTTP 429: quota exhausted")
    )


def test_google_key_failover_treats_transient_server_overload_as_switchable() -> None:
    assert EXTRACT._google_key_switchable(
        RuntimeError("Gemini HTTP 503: model is experiencing high demand (UNAVAILABLE)")
    )
    assert not EXTRACT._google_key_switchable(RuntimeError("Gemini HTTP 400: invalid request"))


def test_multiclaim_call_retries_a_transient_503_with_backoff(monkeypatch) -> None:
    row = {"record_id": "news_001", "title": "Noticia", "body": "Texto médico."}
    calls = 0

    def fake_request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Gemini HTTP 503: temporarily unavailable")
        return {
            "text": json.dumps(
                {
                    "items": [
                        {
                            "record_id": "news_001",
                            "is_medical": True,
                            "reason": "Claim médico.",
                            "claims": [],
                        }
                    ]
                }
            )
        }

    monkeypatch.setattr(EXTRACT, "_google_gemini_request", fake_request)
    results = EXTRACT._call_multiclaim_llm_batch(
        None,
        [row],
        "model",
        24000,
        8000,
        0.0,
        0.9,
        retries=1,
        deadline=float("inf"),
        provider="google",
        google_api_key="key",
        request_timeout=30,
        retry_backoff_seconds=0.0,
    )

    assert calls == 2
    assert results[0][0]["record_id"] == "news_001"


def test_evidence_excludes_pubmed_results_without_abstract() -> None:
    candidates = EVIDENCE._candidate_results(
        {},
        ["1", "2"],
        {
            "1": {"pmid": "1", "title": "Title only", "abstract": ""},
            "2": {"pmid": "2", "title": "Usable", "abstract": "A usable abstract."},
        },
        top_k=5,
        abstract_max_chars=1500,
    )

    assert [candidate["pmid"] for candidate in candidates] == ["2"]


def test_prefilter_keeps_every_fetched_candidate_when_embedding_all() -> None:
    candidates = [
        {
            "pmid": str(index),
            "title": f"Study {index}",
            "abstract_excerpt": f"Abstract about treatment {index}",
            "pubmed_rank": index,
        }
        for index in range(1, 6)
    ]

    ranked = EVIDENCE._prefilter_candidates(
        {"claim_text_en": "treatment outcome"}, candidates
    )

    assert len(ranked) == 5
    assert {candidate["pmid"] for candidate in ranked} == {"1", "2", "3", "4", "5"}


def test_embedding_is_per_claim_and_reuses_identical_pubmed_document(monkeypatch) -> None:
    rows = [
        {
            "record_id": "news_001",
            "claim_id": "news_001_c01",
            "claim_text_en": "first claim",
            "evidence_status": "OK",
            "pubmed_results_json": json.dumps(
                [{"pmid": "123", "title": "Study", "abstract_excerpt": "Same abstract", "pubmed_rank": 1}]
            ),
        },
        {
            "record_id": "news_001",
            "claim_id": "news_001_c02",
            "claim_text_en": "second claim",
            "evidence_status": "OK",
            "pubmed_results_json": json.dumps(
                [{"pmid": "123", "title": "Study", "abstract_excerpt": "Same abstract", "pubmed_rank": 1}]
            ),
        },
    ]

    def fake_embed(*args, **kwargs):
        texts = args[1]
        input_type = args[4]
        if input_type == "SEARCH_QUERY":
            return [[1.0, 0.0], [0.0, 1.0]][: len(texts)]
        return [[1.0, 0.0]][: len(texts)]

    monkeypatch.setattr(EVIDENCE, "_embed_in_batches", fake_embed)
    result = EVIDENCE._apply_embeddings(
        rows, object(), "compartment", "model", 512, 6000, 30, float("inf"), 96, 100000
    )

    first = json.loads(rows[0]["pubmed_results_json"])[0]
    second = json.loads(rows[1]["pubmed_results_json"])[0]
    assert result["status"] == "ok"
    assert first["embedding_similarity"] == 1.0
    assert second["embedding_similarity"] == 0.0
    assert result["embedding_inputs"] == 3  # 2 claims + 1 exact PMID/abstract document
    assert result["document_embedding_cache_hits"] == 1


def test_translation_reuses_identical_pubmed_abstract_between_claims(monkeypatch) -> None:
    rows = [
        {
            "record_id": "news_001",
            "claim_id": "news_001_c01",
            "claim_text_en": "first claim",
            "evidence_status": "OK",
            "pubmed_results_json": json.dumps(
                [{"pmid": "123", "title": "Study", "abstract_excerpt": "Same abstract", "pubmed_rank": 1}]
            ),
        },
        {
            "record_id": "news_001",
            "claim_id": "news_001_c02",
            "claim_text_en": "second claim",
            "evidence_status": "OK",
            "pubmed_results_json": json.dumps(
                [{"pmid": "123", "title": "Study", "abstract_excerpt": "Same abstract", "pubmed_rank": 1}]
            ),
        },
    ]
    calls: list[list[str]] = []

    def fake_translate(_key, _model, batch, _tokens, _timeout):
        calls.append([row["claim_id"] for row in batch])
        return {
            EVIDENCE._claim_identity(row): {"123": "El mismo resumen en español."}
            for row in batch
        }

    monkeypatch.setattr(EVIDENCE, "_translate_batch", fake_translate)
    stats = EVIDENCE._apply_translations(
        rows,
        ["translation-key"],
        "model",
        1,
        1000,
        30,
        float("inf"),
        "run_1",
        True,
    )

    assert calls == [["news_001_c01"]]
    assert stats["translation_cache_hits"] == 1
    assert stats["translation_cache_rows"] == 1
    assert [row["translation_status"] for row in rows] == ["OK", "OK"]


def test_translation_batches_only_combine_small_complete_claim_rows() -> None:
    def row(claim_id: str, abstracts: list[str]) -> dict[str, str]:
        return {
            "record_id": "news_001",
            "claim_id": claim_id,
            "pubmed_results_json": json.dumps(
                [
                    {
                        "pmid": f"{claim_id}-{index}",
                        "title": "Study",
                        "abstract_excerpt": abstract,
                    }
                    for index, abstract in enumerate(abstracts, start=1)
                ]
            ),
        }

    rows = [
        row("c01", ["a" * 100]),
        row("c02", ["b" * 100]),
        row("c03", ["c" * 450]),
    ]

    batches = EVIDENCE._translation_batches(
        rows,
        batch_size=2,
        max_input_chars=300,
        max_abstracts=2,
    )

    assert [[item["claim_id"] for item in batch] for batch in batches] == [["c01", "c02"], ["c03"]]


def test_manifest_metrics_make_body_truncation_and_abstract_workload_auditable() -> None:
    claim_metrics = EXTRACT._claims_input_metrics(
        [{"body": "a" * 10}, {"body": "b" * 4}],
        max_body_chars=6,
    )
    evidence_metrics = EVIDENCE._evidence_abstract_metrics(
        [
            {
                "candidates": [
                    {"pmid": "1", "abstract_excerpt": "Same", "abstract_es": "Igual"},
                    {"pmid": "1", "abstract_excerpt": "Same", "abstract_es": "Igual"},
                ]
            }
        ]
    )

    assert claim_metrics == {
        "articles": 2,
        "body_chars_available": 14,
        "body_chars_sent": 10,
        "body_chars_omitted_by_limit": 4,
        "articles_truncated_by_body_limit": 1,
    }
    assert evidence_metrics["candidate_abstracts"] == 2
    assert evidence_metrics["unique_pmid_abstracts"] == 1
    assert evidence_metrics["abstract_source_chars"] == 8


def test_translation_batch_rejects_omitted_pmid_for_fallback_recovery(monkeypatch) -> None:
    row = {
        "record_id": "news_003",
        "claim_id": "news_003_c01",
        "pubmed_results_json": json.dumps(
            [
                {"pmid": "111", "abstract_excerpt": "First abstract"},
                {"pmid": "222", "abstract_excerpt": "Second abstract"},
            ]
        ),
    }

    monkeypatch.setattr(
        EVIDENCE,
        "_google_generate_json",
        lambda *_args, **_kwargs: {
            "items": [
                {
                    "claim_id": "news_003_c01",
                    "translations": [{"pmid": "111", "abstract_es": "Primer abstract."}],
                }
            ]
        },
    )

    try:
        EVIDENCE._translate_batch("key", "model", [row], 1000, 30)
    except ValueError as exc:
        assert "omitted translation(s)" in str(exc)
        assert "222" in str(exc)
    else:
        raise AssertionError("A missing PMID must trigger candidate-level recovery")


def test_parallel_translation_recovers_recitation_with_paraphrase(monkeypatch) -> None:
    """The two-key path must reach the per-abstract RECITATION fallback."""

    row = {
        "record_id": "news_001",
        "claim_id": "news_001_c01",
        "claim_text_en": "first claim",
        "evidence_status": "OK",
        "pubmed_results_json": json.dumps(
            [{"pmid": "123", "title": "Study", "abstract_excerpt": "Literal abstract", "pubmed_rank": 1}]
        ),
    }

    def recitation(*_args, **_kwargs):
        raise RuntimeError("Gemini translation returned an empty response (RECITATION)")

    monkeypatch.setattr(EVIDENCE, "_translate_batch", recitation)
    monkeypatch.setattr(EVIDENCE, "_google_generate_text", lambda *_args, **_kwargs: "Resumen fiel en español.")

    stats = EVIDENCE._apply_translations(
        [row],
        ["translation-key-1", "translation-key-2"],
        "model",
        1,
        1000,
        30,
        float("inf"),
        "run_1",
        True,
        parallelism=2,
    )

    candidate = json.loads(row["pubmed_results_json"])[0]
    assert row["translation_status"] == "OK"
    assert row["translation_error"] == ""
    assert candidate["translation_status"] == "PARAPHRASED"
    assert candidate["abstract_es"] == "Resumen fiel en español."
    assert stats["translation_candidate_fallbacks"] == 1
    assert stats["paraphrased_abstracts"] == 1


def test_segmented_paraphrase_recovers_when_whole_abstract_is_recitation_blocked(monkeypatch) -> None:
    row = {
        "record_id": "news_002",
        "claim_id": "news_002_c01",
        "claim_text_en": "second claim",
        "evidence_status": "OK",
        "pubmed_results_json": json.dumps(
            [
                {
                    "pmid": "456",
                    "title": "Study",
                    "abstract_excerpt": f"{'a' * 500}. {'b' * 500}.",
                    "pubmed_rank": 1,
                }
            ]
        ),
    }

    def recitation(*_args, **_kwargs):
        raise RuntimeError("Gemini translation returned an empty response (RECITATION)")

    def text_fallback(*args, **_kwargs):
        prompt = args[2]
        if "Abstract en inglés:" in prompt:
            raise RuntimeError("Gemini plain-text translation returned an empty response")
        return "Resumen fiel del fragmento."

    monkeypatch.setattr(EVIDENCE, "_translate_batch", recitation)
    monkeypatch.setattr(EVIDENCE, "_google_generate_text", text_fallback)

    stats = EVIDENCE._apply_translations(
        [row],
        ["translation-key-1"],
        "model",
        1,
        1000,
        30,
        float("inf"),
        "run_2",
        True,
    )

    candidate = json.loads(row["pubmed_results_json"])[0]
    assert row["translation_status"] == "OK"
    assert candidate["translation_status"] == "PARAPHRASED"
    assert candidate["abstract_es"] == "Resumen fiel del fragmento. Resumen fiel del fragmento."
    assert stats["paraphrased_abstracts"] == 1


def test_gate_changes_ok_to_error_when_embedding_or_translation_failed() -> None:
    rows = [
        {
            "llm_status": "OK",
            "evidence_status": "OK",
            "embedding_status": "ERROR",
            "translation_status": "OK",
        },
        {
            "llm_status": "OK",
            "evidence_status": "OK",
            "embedding_status": "OK",
            "translation_status": "PARTIAL",
        },
    ]

    EVIDENCE._gate_evidence_for_human_review(rows)

    assert [row["evidence_status"] for row in rows] == ["ERROR", "ERROR"]
    assert "EMBEDDING_ERROR" in rows[0]["evidence_error"]
    assert "TRANSLATION_PARTIAL" in rows[1]["evidence_error"]


def test_v1_claim_loader_explicitly_excludes_llm_failures() -> None:
    claims_object = "silver/claims/run_id=run_1/claims.jsonl"
    body_object = "silver/body/body_run.csv"
    records = [
        {
            "schema_version": "claims.v1",
            "record_id": "ok-news",
            "source_body_object": body_object,
            "llm_status": "OK",
            "article": {"title": "Ok"},
            "claims": [
                {
                    "claim_id": "ok-news_c01",
                    "claim_text": "Claim.",
                    "claim_text_en": "Claim.",
                    "pubmed_query_en": "claim",
                    "query_status": "OK",
                    "is_claim_eligible": True,
                }
            ],
        },
        {
            "schema_version": "claims.v1",
            "record_id": "failed-news",
            "source_body_object": body_object,
            "llm_status": "ERROR",
            "article": {"title": "Failed"},
            "claims": [
                {
                    "claim_id": "failed-news_c01",
                    "claim_text": "Should not search.",
                    "claim_text_en": "Should not search.",
                    "pubmed_query_en": "should not search",
                    "query_status": "OK",
                    "is_claim_eligible": True,
                }
            ],
        },
    ]

    class Client:
        def get_object(self, namespace_name, bucket_name, object_name):
            payload = (
                "\n".join(json.dumps(record) for record in records).encode("utf-8")
                if object_name == claims_object
                else b"record_id,body,extraction_status\nok-news,body,OK\nfailed-news,body,OK\n"
            )
            return types.SimpleNamespace(data=types.SimpleNamespace(content=io.BytesIO(payload)))

    rows, stats = EVIDENCE._v1_claim_rows(Client(), "ns", "bucket", claims_object)

    assert [row["claim_id"] for row in rows] == ["ok-news_c01"]
    assert stats["skipped_llm_not_ok"] == 1


def test_local_v1_handlers_run_end_to_end_with_all_claims(monkeypatch) -> None:
    class FakeStorage:
        def __init__(self, objects: dict[str, bytes]) -> None:
            self.objects = objects

        def get_object(self, namespace_name, bucket_name, object_name):
            return types.SimpleNamespace(data=types.SimpleNamespace(content=io.BytesIO(self.objects[object_name])))

        def put_object(self, namespace_name, bucket_name, object_name, put_object_body, content_type):
            self.objects[object_name] = put_object_body.read()

    class FakeResponse:
        def __init__(self, _ctx, response_data, status_code=200, headers=None):
            self.response_data = response_data
            self.status_code = status_code
            self.headers = headers or {}

    body_object = "silver/body/body_run.csv"
    storage = FakeStorage(
        {
            body_object: (
                "record_id,country,source_name,canonical_url,title,topic,body,extraction_status\n"
                "news_001,pe,Fuente,https://example.test/news-001,Noticia,health,"
                "El tratamiento X cura la enfermedad Y. La enfermedad Y afecta a adultos.,OK\n"
            ).encode("utf-8")
        }
    )
    extraction_document = {
        "items": [
            {
                "record_id": "news_001",
                "is_medical": True,
                "reason": "Dos claims.",
                "claims": [
                    {
                        "claim_text": "El tratamiento X cura la enfermedad Y.",
                        "claim_text_en": "Treatment X cures disease Y.",
                        "pubmed_query_en": "treatment X disease Y",
                        "claim_type": "tratamiento",
                        "is_claim_eligible": True,
                    },
                    {
                        "claim_text": "La enfermedad Y afecta a adultos.",
                        "claim_text_en": "Disease Y affects adults.",
                        "pubmed_query_en": "disease Y adults",
                        "claim_type": "riesgo",
                        "is_claim_eligible": True,
                    },
                ],
            }
        ]
    }
    monkeypatch.setattr(EXTRACT, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(EXTRACT, "_vault_secret_value", lambda _secret: "claim-key")
    monkeypatch.setattr(EXTRACT, "_google_gemini_request", lambda *args, **kwargs: {"text": json.dumps(extraction_document)})
    monkeypatch.setattr(EXTRACT.response, "Response", FakeResponse)
    monkeypatch.setenv("PIPELINE_SCHEMA", "v1")
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "ns")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "bucket")
    monkeypatch.setenv("LLM_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_GEMINI_SECRET_OCIDS", "secret")
    monkeypatch.setenv("LLM_MAX_ROWS", "0")
    monkeypatch.setenv("LLM_MAX_SECONDS", "60")
    monkeypatch.setenv("LLM_TIME_BUFFER", "1")
    claim_event = {"data": {"resourceName": body_object, "additionalDetails": {"bucketName": "bucket"}}}

    claim_response = EXTRACT.handler(None, io.BytesIO(json.dumps(claim_event).encode("utf-8")))
    claim_result = json.loads(claim_response.response_data)
    claims_object = claim_result["object_name"]
    claims_records = [json.loads(line) for line in storage.objects[claims_object].decode("utf-8").splitlines()]
    assert claim_result["status"] == "complete"
    assert len(claims_records) == 1
    assert claims_records[0]["claims_count"] == 2

    monkeypatch.setattr(EVIDENCE, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(EVIDENCE, "_vault_secret_value", lambda _secret: "translation-key")
    pmids = ["12345671", "12345672", "12345673", "12345674", "12345675"]
    monkeypatch.setattr(EVIDENCE, "_search_pubmed", lambda *args, **kwargs: pmids)
    monkeypatch.setattr(
        EVIDENCE,
        "_fetch_pubmed",
        lambda *args, **kwargs: {
            pmid: {
                "pmid": pmid,
                "title": "Clinical study",
                "abstract": "A biomedical abstract with usable evidence.",
                "journal": "Journal",
                "publication_date": "2025",
                "doi": "",
                "publication_types": ["Clinical Trial"],
            }
            for pmid in pmids
        },
    )
    monkeypatch.setattr(EVIDENCE, "_generative_ai_client", lambda *args, **kwargs: object())
    embedding_inputs: dict[str, list[str]] = {}

    def fake_embed(*args, **kwargs):
        texts = args[1]
        input_type = args[4]
        embedding_inputs.setdefault(input_type, []).extend(texts)
        return [[1.0, 0.0] for _ in texts] if input_type == "SEARCH_QUERY" else [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(EVIDENCE, "_embed_in_batches", fake_embed)
    monkeypatch.setattr(
        EVIDENCE,
        "_translate_batch",
        lambda _key, _model, rows, _tokens, _timeout: {
            EVIDENCE._claim_identity(row): {
                pmid: "Un abstract biomédico en español." for pmid in pmids
            }
            for row in rows
        },
    )
    monkeypatch.setattr(EVIDENCE.response, "Response", FakeResponse)
    monkeypatch.setenv("PIPELINE_MODE", "retrieve")
    monkeypatch.setenv("EVIDENCE_MAX_ROWS", "0")
    monkeypatch.setenv("EVIDENCE_MAX_SECONDS", "60")
    monkeypatch.setenv("EVIDENCE_TIME_BUFFER", "1")
    monkeypatch.setenv("GENAI_COMPARTMENT_ID", "compartment")
    monkeypatch.setenv("PUBMED_EMAIL", "research@example.test")
    monkeypatch.setenv("GOOGLE_TRANSLATION_SECRET_OCIDS", "secret")
    evidence_event = {"data": {"resourceName": claims_object, "additionalDetails": {"bucketName": "bucket"}}}

    translate_response = EVIDENCE.handler(None, io.BytesIO(json.dumps(evidence_event).encode("utf-8")))
    translate_result = json.loads(translate_response.response_data)
    translate_object = translate_result["object_name"]
    translate_records = [
        json.loads(line) for line in storage.objects[translate_object].decode("utf-8").splitlines()
    ]
    assert translate_result["status"] == "complete"
    assert translate_object.startswith("silver/translate/translate_")
    assert all(record["translation_status"] == "PENDING" for record in translate_records)

    monkeypatch.setenv("PIPELINE_MODE", "translation")
    translation_event = {"data": {"resourceName": translate_object, "additionalDetails": {"bucketName": "bucket"}}}
    evidence_response = EVIDENCE.handler(None, io.BytesIO(json.dumps(translation_event).encode("utf-8")))
    evidence_result = json.loads(evidence_response.response_data)
    evidence_records = [
        json.loads(line) for line in storage.objects[evidence_result["object_name"]].decode("utf-8").splitlines()
    ]
    evidence_manifest = json.loads(storage.objects[evidence_result["manifest_object"]].decode("utf-8"))

    assert evidence_result["status"] == "complete"
    assert len(evidence_records) == 2
    assert {record["claim_id"] for record in evidence_records} == {
        claim["claim_id"] for claim in claims_records[0]["claims"]
    }
    assert all(record["evidence_status"] == "OK" for record in evidence_records)
    assert all(record["evidence_run_id"] == evidence_result["run_id"] for record in evidence_records)
    assert all(record["body"] for record in evidence_records)
    assert all(len(record["candidates"]) == 5 for record in evidence_records)
    assert len(embedding_inputs["SEARCH_QUERY"]) == 2
    assert len(embedding_inputs["SEARCH_DOCUMENT"]) == 5
    assert evidence_manifest["configuration"]["embed_all_retrieved_candidates_with_abstract"] is True
    assert evidence_manifest["workload"]["abstracts"]["candidate_abstracts"] == 10
    assert evidence_manifest["status"] == "COMPLETE"
