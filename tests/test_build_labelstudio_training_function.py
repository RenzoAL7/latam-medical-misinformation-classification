from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).parents[1]
FUNCTION_DIR = ROOT / "oci" / "functions" / "build_labelstudio_training"
sys.path.insert(0, str(FUNCTION_DIR))
spec = importlib.util.spec_from_file_location("build_labelstudio_training_function", FUNCTION_DIR / "func.py")
assert spec and spec.loader
function = importlib.util.module_from_spec(spec)
spec.loader.exec_module(function)


class _Response:
    def __init__(self, _ctx, response_data, status_code=200, headers=None):
        self.response_data = response_data
        self.status_code = status_code
        self.headers = headers or {}


class _Object:
    def __init__(self, name: str, time_modified: str = "2026-10-19T18:00:00Z"):
        self.name = name
        self.time_modified = time_modified


class _Storage:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)

    def list_objects(self, *, prefix: str, **_: object) -> types.SimpleNamespace:
        names = [
            _Object(name)
            for name in sorted(self.objects)
            if name.startswith(prefix)
        ]
        return types.SimpleNamespace(data=types.SimpleNamespace(objects=names, next_start_with=None))

    def get_object(self, *, object_name: str, **_: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(data=types.SimpleNamespace(content=self.objects[object_name]))

    def put_object(self, *, object_name: str, put_object_body: io.BytesIO, **_: object) -> None:
        self.objects[object_name] = put_object_body.getvalue()


def _task(record_id: str, claim_id: str, completed: bool = True) -> dict:
    task = {
        "data": {
            "record_id": record_id,
            "claim_id": claim_id,
            "claim_key": f"{record_id}::{claim_id}",
            "title": f"Título {record_id}",
            "country": "pe",
            "source_name": "Fuente",
            "claim_text": f"Claim {claim_id}",
            "claim_type": "tratamiento",
            "best_pmid": "12345678",
            "best_embedding_similarity": "0.8",
            "best_tfidf_similarity": "0.4",
            "evidence_status": "OK",
            "evidence_run_id": "run_1",
        },
        "annotations": [],
    }
    if completed:
        task["annotations"] = [
            {
                "id": f"annotation-{claim_id}",
                "completed": True,
                "completed_by": "reviewer@example.test",
                "updated_at": "2026-10-19T17:00:00Z",
                "result": [
                    {"from_name": "claim_materiality", "value": {"choices": ["central"]}},
                    {"from_name": "evidence_relation", "value": {"choices": ["supports"]}},
                    {"from_name": "claim_verdict", "value": {"choices": ["RESPALDADO"]}},
                    {"from_name": "selected_pmids", "value": {"text": ["12345678"]}},
                    {"from_name": "verification_note", "value": {"text": ["Revisión humana"]}},
                ],
            }
        ]
    return task


def _body_csv(record_ids: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["record_id", "title", "body"])
    writer.writeheader()
    for record_id in record_ids:
        writer.writerow({"record_id": record_id, "title": f"Título {record_id}", "body": f"Cuerpo completo {record_id}"})
    return output.getvalue().encode("utf-8-sig")


def _evidence(rows: list[tuple[str, str]]) -> bytes:
    payload = []
    for record_id, claim_id in rows:
        payload.append(
            {
                "schema_version": "evidence.v1",
                "record_id": record_id,
                "claim_id": claim_id,
                "evidence_run_id": "run_1",
                "run_id": "run_1",
                "source_body_object": "silver/body/body_run_1.csv",
                "llm_status": "OK",
                "evidence_status": "OK",
                "embedding_status": "OK",
                "translation_status": "OK",
            }
        )
    return ("\n".join(json.dumps(row) for row in payload) + "\n").encode("utf-8")


def _configure(monkeypatch, storage: _Storage) -> None:
    monkeypatch.setenv("OBJECT_STORAGE_NAMESPACE", "namespace")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "mednews-data")
    monkeypatch.setattr(function, "_object_storage_client", lambda: storage)
    monkeypatch.setattr(function, "_run_stamp", lambda: "20261019")
    monkeypatch.setattr(function.response, "Response", _Response)


def test_scheduled_payload_builds_one_gold_csv_and_rejects_pending_articles(monkeypatch) -> None:
    export_name = "silver/annotations/labelstudio/labelstudio_20261019T175500Z.json"
    evidence_name = "silver/evidence/run_id=run_1/evidence.jsonl"
    storage = _Storage(
        {
            export_name: json.dumps([_task("news_001", "c01"), _task("news_002", "c02", completed=False)]).encode(),
            evidence_name: _evidence([("news_001", "c01"), ("news_002", "c02")]),
            "silver/body/body_run_1.csv": _body_csv(["news_001", "news_002"]),
        }
    )
    _configure(monkeypatch, storage)

    result = function.handler(None, io.BytesIO(b"{}"))

    assert result.status_code == 200
    payload = json.loads(result.response_data)
    assert payload["training_records"] == 1
    assert payload["rejected_records"] == 1
    assert payload["status"] == "requires_review"
    training_names = [name for name in storage.objects if name.startswith("gold/training/") and name.endswith(".csv")]
    assert training_names == ["gold/training/training_20261019.csv"]
    assert not any(name.endswith(".summary.json") for name in storage.objects)
    rows = list(csv.DictReader(io.StringIO(storage.objects[training_names[0]].decode("utf-8-sig"))))
    assert rows == [{"record_id": "news_001", "title": "Título news_001", "body": "Cuerpo completo news_001", "label": "RESPALDADA", "dataset_split": ""}]
    rejected_names = [name for name in storage.objects if name.startswith("silver/rejected/training/") and name.endswith(".jsonl")]
    assert len(rejected_names) == 1
    assert json.loads(storage.objects[rejected_names[0]].decode().strip())["record_id"] == "news_002"


def test_event_outside_export_prefix_is_written_to_rejected(monkeypatch) -> None:
    storage = _Storage({})
    _configure(monkeypatch, storage)

    event = {"data": {"resourceName": "silver/evidence/not-an-export.jsonl", "additionalDetails": {"bucketName": "mednews-data"}}}
    result = function.handler(None, io.BytesIO(json.dumps(event).encode()))

    assert result.status_code == 500
    payload = json.loads(result.response_data)
    assert payload["rejected_object"].startswith("silver/rejected/training/")
    error = json.loads(storage.objects[payload["rejected_object"]].decode().strip())
    assert error["error_type"] == "RuntimeError"
    assert "Label Studio export" in error["error"]
