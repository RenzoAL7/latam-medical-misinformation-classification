from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import xml.etree.ElementTree as ET
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


def _load_script(path: Path, module_name: str) -> types.ModuleType:
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXTRACT = _load_function(ROOT / "oci/functions/extract_claims/func.py", "extract_claims_multiclaim")
EVIDENCE = _load_function(
    ROOT / "oci/functions/retrieve_pubmed_evidence/func.py", "retrieve_evidence_multiclaim"
)
IMPORTER = _load_script(ROOT / "labelstudio/import_evidence.py", "import_evidence_multiclaim")
EXPORTER = _load_script(ROOT / "labelstudio/export_annotations.py", "export_annotations_multiclaim")
AGGREGATOR = _load_script(ROOT / "labelstudio/aggregate_article_labels.py", "aggregate_article_labels")
VALIDATOR = _load_script(ROOT / "labelstudio/validate_gold.py", "validate_gold_multiclaim")


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


def test_labelstudio_csv_fixture_creates_one_task_per_claim_and_xml_has_no_article_label() -> None:
    fixture = ROOT / "tests/fixtures/labelstudio_multiclaim.fixture"
    rows = IMPORTER._rows_from_path(fixture)
    tasks = [IMPORTER._task_from_row(row) for row in rows]

    assert [task["data"]["claim_key"] for task in tasks] == [
        "news_001::news_001_c01_a1",
        "news_001::news_001_c02_b2",
    ]
    xml_root = ET.parse(ROOT / "labelstudio/config_multiclaim.xml").getroot()
    control_names = {element.attrib["name"] for element in xml_root.iter() if "name" in element.attrib}
    assert {"claim_materiality", "evidence_relation", "claim_verdict", "selected_pmids"}.issubset(control_names)
    assert "final_label" not in control_names


def test_jsonl_import_filters_degraded_claim_and_aggregates_one_article() -> None:
    rows = IMPORTER._rows_from_path(ROOT / "tests/fixtures/evidence_multiclaim.jsonl")
    ready = [row for row in rows if IMPORTER._ready_for_review(row)]
    assert [row["claim_id"] for row in ready] == ["news_001_c01_a1", "news_001_c02_b2"]

    payload = []
    for index, row in enumerate(ready, start=1):
        task = IMPORTER._task_from_row(row)
        relation = "contradicts" if index == 1 else "supports"
        verdict = "REFUTADO" if index == 1 else "RESPALDADO"
        task["annotations"] = [
            {
                "id": index,
                "completed": True,
                "updated_at": f"2026-10-14T0{index}:00:00Z",
                "completed_by": "reviewer@example.test",
                "result": [
                    {"from_name": "claim_materiality", "value": {"choices": ["central"]}},
                    {"from_name": "evidence_relation", "value": {"choices": [relation]}},
                    {"from_name": "claim_verdict", "value": {"choices": [verdict]}},
                    {"from_name": "selected_pmids", "value": {"text": [row["best_pmid"]]}},
                    {"from_name": "verification_note", "value": {"text": ["Revisión humana"]}},
                ],
            }
        ]
        payload.append(task)

    claim_reviews = EXPORTER._claim_review_rows(payload)
    articles = AGGREGATOR.aggregate_rows(
        claim_reviews,
        {
            "news_001": {
                "record_id": "news_001",
                "title": "Noticia médica",
                "body": "Cuerpo completo para entrenar.",
            }
        },
    )
    reviewed, training, rejected = VALIDATOR.validate_rows(articles)

    assert len(claim_reviews) == 2
    assert len(articles) == 1
    assert articles[0]["label"] == "REFUTADA"
    assert articles[0]["aggregation_status"] == "COMPLETADA"
    assert len(reviewed) == len(training) == 1
    assert not rejected


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
    monkeypatch.setenv("EVIDENCE_MAX_ROWS", "0")
    monkeypatch.setenv("EVIDENCE_MAX_SECONDS", "60")
    monkeypatch.setenv("EVIDENCE_TIME_BUFFER", "1")
    monkeypatch.setenv("GENAI_COMPARTMENT_ID", "compartment")
    monkeypatch.setenv("PUBMED_EMAIL", "research@example.test")
    monkeypatch.setenv("GOOGLE_TRANSLATION_SECRET_OCIDS", "secret")
    evidence_event = {"data": {"resourceName": claims_object, "additionalDetails": {"bucketName": "bucket"}}}

    evidence_response = EVIDENCE.handler(None, io.BytesIO(json.dumps(evidence_event).encode("utf-8")))
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
    assert all(len(record["candidates"]) == 5 for record in evidence_records)
    assert len(embedding_inputs["SEARCH_QUERY"]) == 2
    assert len(embedding_inputs["SEARCH_DOCUMENT"]) == 5
    assert evidence_manifest["configuration"]["embed_all_retrieved_candidates_with_abstract"] is True
    assert evidence_manifest["status"] == "COMPLETE"
