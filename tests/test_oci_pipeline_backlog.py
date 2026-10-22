from __future__ import annotations

import csv
import importlib.util
import io
import types
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _load(function_name: str) -> types.ModuleType:
    path = ROOT / "oci" / "functions" / function_name / "func.py"
    spec = importlib.util.spec_from_file_location(f"{function_name}_backlog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def list_objects(self, **_: object) -> types.SimpleNamespace:
        names = [types.SimpleNamespace(name=name) for name in sorted(self.objects)]
        return types.SimpleNamespace(data=types.SimpleNamespace(objects=names, next_start_with=None))

    def get_object(self, *, object_name: str, **_: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(data=types.SimpleNamespace(content=self.objects[object_name]))

    def put_object(self, *, object_name: str, put_object_body: io.BytesIO, **_: object) -> None:
        self.objects[object_name] = put_object_body.getvalue()


def _csv(rows: list[dict[str, str]], fields: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _bronze_row(record_id: str) -> dict[str, str]:
    return {
        "record_id": record_id,
        "canonical_url": f"https://example.test/{record_id}",
        "source_name": "source",
        "published_at": "2026-10-11",
        "title": f"Title {record_id}",
        "subtitle_or_bajada": "",
        "topic": "health",
        "selection_status": "PENDIENTE",
        "retrieved_at": "2026-10-11T13:00:00Z",
        "country": "pe",
    }


def test_body_backlog_skips_terminal_rows_and_counts_all_pending() -> None:
    module = _load("extract_news_body")
    bronze_fields = module.BRONZE_FIELDS
    body_fields = module.SILVER_FIELDS
    storage = _Storage(
        {
            "bronze/a.csv": _csv([_bronze_row("a"), _bronze_row("b")], bronze_fields),
            "bronze/b.csv": _csv([_bronze_row("c")], bronze_fields),
            "silver/body/body_current_a.csv": _csv(
                [
                    {
                        **_bronze_row("a"),
                        "body": "complete",
                        "extraction_status": "OK",
                        "extraction_error": "",
                    },
                    {
                        **_bronze_row("b"),
                        "body": "",
                        "extraction_status": "HTTP_ERROR",
                        "http_status": "503",
                        "extraction_error": "HTTP 503",
                    },
                ],
                body_fields,
            ),
        }
    )

    selected, meta = module._pending_rows(storage, "ns", "bucket", "bronze", "silver/body", 1)

    assert [row["record_id"] for row in selected] == ["b"]
    assert meta["selected_source_object"] == "bronze/a.csv"
    assert meta["pending_rows"] == 2
    assert module._body_output_name("silver/body", "bronze/a.csv") == module._body_output_name(
        "silver/body", "bronze/a.csv"
    )


def test_body_event_uses_only_the_triggering_bronze_object() -> None:
    module = _load("extract_news_body")
    storage = _Storage(
        {
            "bronze/old.csv": _csv([_bronze_row("old")], module.BRONZE_FIELDS),
            "bronze/new.csv": _csv([_bronze_row("new")], module.BRONZE_FIELDS),
        }
    )

    selected, meta = module._event_rows(
        storage,
        "ns",
        "bucket",
        "bronze/new.csv",
        50,
        "silver/body",
    )

    assert [row["record_id"] for row in selected] == ["new"]
    assert meta["source_object"] == "bronze/new.csv"


def test_claim_backlog_retries_failed_rows_and_keeps_source_batch() -> None:
    module = _load("extract_claims")
    body_fields = module.BRONZE_FIELDS + module.BODY_FIELDS
    claim_fields = module.OUTPUT_FIELDS
    body_row = {
        **_bronze_row("a"),
        "body": "A sufficiently long body",
        "extraction_status": "OK",
    }
    body_row_b = {**body_row, "record_id": "b", "canonical_url": "https://example.test/b"}
    storage = _Storage(
        {
            "silver/body/body_a.csv": _csv([body_row, body_row_b], body_fields),
            "silver/body/body_b.csv": _csv(
                [{**body_row, "record_id": "c", "canonical_url": "https://example.test/c"}],
                body_fields,
            ),
            "silver/claims/claims_current_body_a.csv": _csv(
                [{"record_id": "a", "llm_status": "OK"}], claim_fields
            ),
        }
    )

    selected, meta = module._pending_rows(storage, "ns", "bucket", "silver/body", "silver/claims", 1)

    assert [row["record_id"] for row in selected] == ["b"]
    assert selected[0]["source_silver_object"] == "silver/body/body_a.csv"
    assert meta["pending_rows"] == 2
    assert meta["selected_source_object"] == "silver/body/body_a.csv"


def test_claim_event_uses_only_the_triggering_body_object() -> None:
    module = _load("extract_claims")
    body_fields = module.BRONZE_FIELDS + module.BODY_FIELDS
    body_row = {
        **_bronze_row("new"),
        "body": "A sufficiently long body",
        "extraction_status": "OK",
    }
    storage = _Storage(
        {
            "silver/body/old.csv": _csv(
                [{**body_row, "record_id": "old"}], body_fields
            ),
            "silver/body/new.csv": _csv([body_row], body_fields),
            "silver/claims/claims_current_old.csv": _csv(
                [{"record_id": "old", "llm_status": "ERROR"}], module.OUTPUT_FIELDS
            ),
        }
    )

    selected, meta = module._event_claim_rows(
        storage,
        "ns",
        "bucket",
        "silver/body/new.csv",
        "silver/claims",
        25,
    )

    assert [row["record_id"] for row in selected] == ["new"]
    assert meta["source_object"] == "silver/body/new.csv"


def test_evidence_selection_is_bounded_per_claims_source() -> None:
    module = _load("retrieve_pubmed_evidence")
    rows = {
        "a": {
            "record_id": "a",
            "is_claim_eligible": "true",
            "claim_text": "claim a",
            "claim_text_en": "claim a",
            "pubmed_query_en": "claim a",
            "query_status": "OK",
            "source_claims_object": "silver/claims/claims_a.csv",
        },
        "b": {
            "record_id": "b",
            "is_claim_eligible": "true",
            "claim_text": "claim b",
            "claim_text_en": "claim b",
            "pubmed_query_en": "claim b",
            "query_status": "OK",
            "source_claims_object": "silver/claims/claims_a.csv",
        },
        "c": {
            "record_id": "c",
            "is_claim_eligible": "true",
            "claim_text": "claim c",
            "claim_text_en": "claim c",
            "pubmed_query_en": "claim c",
            "query_status": "OK",
            "source_claims_object": "silver/claims/claims_b.csv",
        },
    }

    selected, meta = module._select_claims(rows, {"a"}, 1)

    assert [row["record_id"] for row in selected] == ["b"]
    assert meta["pending"] == 2
    assert meta["selected_source_object"] == "silver/claims/claims_a.csv"
    assert module._evidence_output_name("silver/evidence", "silver/claims/claims_a.csv").startswith(
        "silver/evidence/evidence_run_zz_current_"
    )
    assert module._evidence_row_is_terminal({"evidence_status": "NO_RESULTS"})
    assert not module._evidence_row_is_terminal(
        {"evidence_status": "ERROR", "evidence_error": "EVIDENCE_TIME_BUDGET"}
    )


def test_evidence_event_uses_only_the_triggering_claims_object() -> None:
    module = _load("retrieve_pubmed_evidence")
    fields = ["record_id", "claim_text", "claim_text_en", "pubmed_query_en"]
    storage = _Storage(
        {
            "silver/claims/old.csv": _csv([{"record_id": "old"}], fields),
            "silver/claims/new.csv": _csv([{"record_id": "new"}], fields),
        }
    )

    rows, meta = module._event_claim_rows(
        storage,
        "ns",
        "bucket",
        "silver/claims/new.csv",
    )

    assert list(rows) == ["new"]
    assert meta["source_object"] == "silver/claims/new.csv"


def test_fetch_does_not_write_an_empty_bronze_csv(monkeypatch) -> None:
    module = _load("fetch_newsdata")
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "ns")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "bucket")
    storage = _Storage({})
    result = module._write_bronze(
        [],
        {
            "candidate_rows": 3,
            "duplicates_skipped": 3,
            "existing_record_ids": 10,
            "dedup_enabled": True,
            "countries_attempted": ["pe"],
        },
        storage,
    )

    assert result["write_skipped"] == "empty_batch"
    assert result["object_name"] == ""
    assert storage.objects == {}


def test_fetch_queries_all_country_groups_and_selects_round_robin(monkeypatch) -> None:
    module = _load("fetch_newsdata")
    monkeypatch.setenv("NEWSDATA_COUNTRY_SEQUENCE", "ar,bo,pe,pr")
    monkeypatch.setenv("NEWSDATA_COUNTRY_GROUP_SIZE", "2")
    monkeypatch.setenv("NEWSDATA_TARGET_ROWS", "4")
    monkeypatch.setenv("NEWSDATA_MAX_SECONDS", "30")
    monkeypatch.setenv("NEWSDATA_TIME_BUFFER", "0")
    monkeypatch.setenv("NEWSDATA_COUNTRY_DELAY", "0")
    calls: list[str] = []

    def fake_request(
        _api_keys: list[str],
        requested_country: str,
        key_start_index: int,
        deadline: float | None = None,
    ) -> tuple[dict[str, object], dict[str, str]]:
        del deadline
        del key_start_index
        calls.append(requested_country)
        results = []
        for country in requested_country.split(","):
            for index in range(2):
                results.append(
                    {
                        "language": "es",
                        "category": ["health"],
                        "country": country,
                        "link": f"https://example.test/{country}-{index}",
                        "title": f"Noticia {country} {index}",
                        "source_name": "Fuente de prueba",
                    }
                )
        return {"results": results}, {"pages_fetched": "1", "key_slot": "1"}

    monkeypatch.setattr(module, "_request_newsdata_with_failover", fake_request)

    rows, metadata = module._collect_country_batch(
        ["secret"], _Storage({}), "ns", "bucket", "bronze"
    )

    assert calls == ["ar,bo", "pe,pr"]
    assert [row["country"] for row in rows] == ["ar", "bo", "pe", "pr"]
    assert metadata["selection_mode"] == "round_robin_country"
    assert metadata["selected_country_counts"] == {"ar": 1, "bo": 1, "pe": 1, "pr": 1}
    assert metadata["countries_attempted"] == ["ar", "bo", "pe", "pr"]
    assert all(item["selected_rows"] == 2 for item in metadata["country_stats"])


def test_fetch_keeps_successful_groups_when_one_newsdata_group_fails(monkeypatch) -> None:
    module = _load("fetch_newsdata")
    monkeypatch.setenv("NEWSDATA_COUNTRY_SEQUENCE", "ar,bo,pe,pr")
    monkeypatch.setenv("NEWSDATA_COUNTRY_GROUP_SIZE", "2")
    monkeypatch.setenv("NEWSDATA_TARGET_ROWS", "2")
    monkeypatch.setenv("NEWSDATA_MAX_SECONDS", "30")
    monkeypatch.setenv("NEWSDATA_TIME_BUFFER", "0")
    monkeypatch.setenv("NEWSDATA_COUNTRY_DELAY", "0")
    calls: list[str] = []

    def fake_request(
        _api_keys: list[str],
        requested_country: str,
        key_start_index: int,
        deadline: float | None = None,
    ) -> tuple[dict[str, object], dict[str, str]]:
        del key_start_index
        del deadline
        calls.append(requested_country)
        if requested_country == "ar,bo":
            raise RuntimeError("temporary NewsData provider failure")
        return {
            "results": [
                {
                    "language": "es",
                    "category": ["health"],
                    "country": country,
                    "link": f"https://example.test/{country}",
                    "title": f"Noticia {country}",
                    "source_name": "Fuente de prueba",
                }
                for country in requested_country.split(",")
            ]
        }, {"pages_fetched": "1", "key_slot": "1"}

    monkeypatch.setattr(module, "_request_newsdata_with_failover", fake_request)

    rows, metadata = module._collect_country_batch(
        ["secret"], _Storage({}), "ns", "bucket", "bronze"
    )

    assert calls == ["ar,bo", "pe,pr"]
    assert [row["country"] for row in rows] == ["pe", "pr"]
    assert metadata["partial_batch"] is True
    assert metadata["failed_groups"] == [
        {
            "countries": ["ar", "bo"],
            "group_index": 0,
            "error_type": "RuntimeError",
            "error": "temporary NewsData provider failure",
        }
    ]
    assert metadata["country_stats"][0]["request"]["status"] == "ERROR"
