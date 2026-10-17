from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "04_build_training_dataset.py"
SPEC = importlib.util.spec_from_file_location("build_training_dataset", MODULE_PATH)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def _task(record_id: str, claim_id: str, verdict: str, relation: str, materiality: str) -> dict:
    return {
        "data": {
            "record_id": record_id,
            "claim_id": claim_id,
            "claim_key": f"{record_id}::{claim_id}",
            "country": "pe",
            "source_name": "Fuente",
            "canonical_url": f"https://example.test/{record_id}",
            "published_at": "2026-10-15",
            "title": "Título de la noticia",
            "claim_text": f"Claim {claim_id}",
            "claim_type": "tratamiento",
            "best_pmid": "12345678",
            "best_embedding_similarity": "0.8",
            "best_tfidf_similarity": "0.4",
            "evidence_status": "OK",
            "evidence_run_id": "run_1",
        },
        "annotations": [
            {
                "id": claim_id,
                "completed": True,
                "completed_by": "reviewer@example.test",
                "updated_at": "2026-10-15T12:00:00Z",
                "result": [
                    {"from_name": "claim_materiality", "value": {"choices": [materiality]}},
                    {"from_name": "evidence_relation", "value": {"choices": [relation]}},
                    {"from_name": "claim_verdict", "value": {"choices": [verdict]}},
                    {"from_name": "selected_pmids", "value": {"text": ["12345678"]}},
                    {"from_name": "verification_note", "value": {"text": ["Revisión humana"]}},
                ],
            }
        ],
    }


def _write_body(path: Path, record_id: str = "news_001") -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["record_id", "title", "body", "country", "source_name"])
        writer.writeheader()
        writer.writerow(
            {
                "record_id": record_id,
                "title": "Título de la noticia",
                "body": "Cuerpo completo de la noticia para entrenamiento.",
                "country": "pe",
                "source_name": "Fuente",
            }
        )


def _write_evidence(
    path: Path,
    claims: list[tuple[str, str, dict[str, str] | None]],
) -> None:
    rows: list[dict] = []
    for record_id, claim_id, overrides in claims:
        row = {
            "schema_version": "evidence.v1",
            "record_id": record_id,
            "claim_id": claim_id,
            "article": {"title": "Título de la noticia", "country": "pe"},
            "llm_status": "OK",
            "evidence_status": "OK",
            "embedding_status": "OK",
            "translation_status": "OK",
        }
        row.update(overrides or {})
        rows.append(row)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_builder_creates_one_binary_article_from_many_claims(tmp_path: Path) -> None:
    export_path = tmp_path / "labelstudio_export.json"
    export_path.write_text(
        json.dumps(
            [
                _task("news_001", "c01", "REFUTADO", "contradicts", "central"),
                _task("news_001", "c02", "RESPALDADO", "supports", "material"),
            ]
        ),
        encoding="utf-8",
    )
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    evidence_path = tmp_path / "evidence.jsonl"
    _write_evidence(evidence_path, [("news_001", "c01", None), ("news_001", "c02", None)])

    summary = builder.build_dataset(export_path, [evidence_path], [body_path], tmp_path / "output")

    assert summary["status"] == "ok"
    assert summary["claim_reviews_completed"] == 2
    assert summary["articles_total"] == summary["training_records"] == 1
    with (tmp_path / "output" / "training.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "record_id": "news_001",
            "title": "Título de la noticia",
            "body": "Cuerpo completo de la noticia para entrenamiento.",
            "label": "REFUTADA",
            "dataset_split": "",
        }
    ]


def test_builder_excludes_article_when_any_claim_task_is_unreviewed(tmp_path: Path) -> None:
    reviewed = _task("news_001", "c01", "RESPALDADO", "supports", "central")
    pending = _task("news_001", "c02", "RESPALDADO", "supports", "material")
    pending["annotations"] = []
    export_path = tmp_path / "labelstudio_export.jsonl"
    export_path.write_text(
        "\n".join(json.dumps(task) for task in [reviewed, pending]) + "\n", encoding="utf-8"
    )
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    evidence_path = tmp_path / "evidence.jsonl"
    _write_evidence(evidence_path, [("news_001", "c01", None), ("news_001", "c02", None)])

    summary = builder.build_dataset(export_path, [evidence_path], [body_path], tmp_path / "output")

    assert summary["status"] == "requires_review"
    assert summary["claims_pending"] == 1
    assert summary["training_records"] == 0
    with (tmp_path / "output" / "article_labels.csv").open(encoding="utf-8-sig", newline="") as handle:
        article = next(csv.DictReader(handle))
    assert article["aggregation_status"] == "PENDIENTE_REVISION"
    assert article["final_training_eligible"] == "NO"


def test_builder_cli_uses_the_conventional_input_folder(tmp_path: Path) -> None:
    (tmp_path / "labelstudio_export.json").write_text(
        json.dumps([_task("news_001", "c01", "RESPALDADO", "supports", "central")]),
        encoding="utf-8",
    )
    _write_body(tmp_path / "body.csv")
    _write_evidence(tmp_path / "evidence.jsonl", [("news_001", "c01", None)])

    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["training_records"] == 1
    assert (tmp_path / "output" / "training.csv").is_file()


def test_builder_blocks_an_article_when_evidence_contains_an_unreviewable_claim(tmp_path: Path) -> None:
    export_path = tmp_path / "labelstudio_export.json"
    export_path.write_text(
        json.dumps([_task("news_001", "c01", "RESPALDADO", "supports", "central")]),
        encoding="utf-8",
    )
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    evidence_path = tmp_path / "evidence.jsonl"
    _write_evidence(
        evidence_path,
        [
            ("news_001", "c01", None),
            (
                "news_001",
                "c02",
                {
                    "evidence_status": "ERROR",
                    "embedding_status": "ERROR",
                    "translation_status": "ERROR",
                },
            ),
        ],
    )

    summary = builder.build_dataset(export_path, [evidence_path], [body_path], tmp_path / "output")

    assert summary["status"] == "requires_review"
    assert summary["claims_without_reviewable_evidence"] == 1
    assert summary["training_records"] == 0
    with (tmp_path / "output" / "article_labels.csv").open(encoding="utf-8-sig", newline="") as handle:
        article = next(csv.DictReader(handle))
    assert article["aggregation_status"] == "PENDIENTE_EVIDENCIA"
    assert article["final_training_eligible"] == "NO"


def test_reviewed_secondary_claim_does_not_block_on_technical_evidence_error(tmp_path: Path) -> None:
    decisive = _task("news_001", "c01", "RESPALDADO", "supports", "central")
    secondary = _task("news_001", "c02", "", "", "secondary")
    secondary["data"]["evidence_status"] = "ERROR"
    secondary["annotations"][0]["result"] = [
        result
        for result in secondary["annotations"][0]["result"]
        if result["from_name"] not in {"evidence_relation", "claim_verdict", "selected_pmids"}
    ]
    export_path = tmp_path / "labelstudio_export.json"
    export_path.write_text(json.dumps([decisive, secondary]), encoding="utf-8")
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    evidence_path = tmp_path / "evidence.jsonl"
    _write_evidence(
        evidence_path,
        [
            ("news_001", "c01", None),
            (
                "news_001",
                "c02",
                {
                    "evidence_status": "ERROR",
                    "embedding_status": "ERROR",
                    "translation_status": "ERROR",
                },
            ),
        ],
    )

    summary = builder.build_dataset(export_path, [evidence_path], [body_path], tmp_path / "output")

    assert summary["status"] == "ok"
    assert summary["training_records"] == 1
    assert summary["claims_without_reviewable_evidence"] == 0
    assert summary["claims_secondary_without_evidence_review"] == 1
    assert summary["claims_with_technical_evidence_blocks"] == 1


def test_terminal_no_results_requires_triage_and_cannot_be_labeled_true_or_false(tmp_path: Path) -> None:
    supported = _task("news_001", "c01", "RESPALDADO", "supports", "central")
    undetermined = _task("news_001", "c02", "NO_DETERMINABLE", "unclear", "material")
    undetermined["data"]["evidence_status"] = "NO_RESULTS"
    undetermined["annotations"][0]["result"] = [
        result
        for result in undetermined["annotations"][0]["result"]
        if result["from_name"] != "selected_pmids"
    ]
    export_path = tmp_path / "labelstudio_export.json"
    export_path.write_text(json.dumps([supported, undetermined]), encoding="utf-8")
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    evidence_path = tmp_path / "evidence.jsonl"
    _write_evidence(
        evidence_path,
        [
            ("news_001", "c01", None),
            (
                "news_001",
                "c02",
                {
                    "evidence_status": "NO_RESULTS",
                    "embedding_status": "SKIPPED_NO_EVIDENCE",
                    "translation_status": "SKIPPED_NO_EVIDENCE",
                },
            ),
        ],
    )

    summary = builder.build_dataset(export_path, [evidence_path], [body_path], tmp_path / "output")

    assert summary["status"] == "ok"
    assert summary["claims_pending"] == 0
    assert summary["training_records"] == 0
    with (tmp_path / "output" / "article_labels.csv").open(encoding="utf-8-sig", newline="") as handle:
        article = next(csv.DictReader(handle))
    assert article["label"] == "NO_DETERMINABLE"
    assert article["aggregation_status"] == "COMPLETADA"


def test_builder_prefers_a_recovered_evidence_revision_for_the_same_claim(tmp_path: Path) -> None:
    export_path = tmp_path / "labelstudio_export.json"
    export_path.write_text(
        json.dumps([_task("news_001", "c01", "RESPALDADO", "supports", "central")]),
        encoding="utf-8",
    )
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    initial = tmp_path / "evidence_initial.jsonl"
    _write_evidence(
        initial,
        [
            (
                "news_001",
                "c01",
                {
                    "evidence_status": "ERROR",
                    "translation_status": "TIME_BUDGET",
                    "retrieved_at": "2026-10-16T13:14:00Z",
                },
            )
        ],
    )
    recovered = tmp_path / "evidence_recovered.jsonl"
    _write_evidence(
        recovered,
        [("news_001", "c01", {"retrieved_at": "2026-10-16T13:20:00Z"})],
    )

    summary = builder.build_dataset(export_path, [initial, recovered], [body_path], tmp_path / "output")

    assert summary["status"] == "ok"
    assert summary["evidence_revisions_superseded"] == 1
    assert summary["claims_without_reviewable_evidence"] == 0
    assert summary["training_records"] == 1


def test_builder_rejects_a_label_studio_export_not_covered_by_evidence(tmp_path: Path) -> None:
    export_path = tmp_path / "labelstudio_export.json"
    export_path.write_text(
        json.dumps(
            [
                _task("news_001", "c01", "RESPALDADO", "supports", "central"),
                _task("news_001", "c02", "RESPALDADO", "supports", "material"),
            ]
        ),
        encoding="utf-8",
    )
    body_path = tmp_path / "body.csv"
    _write_body(body_path)
    evidence_path = tmp_path / "evidence.jsonl"
    _write_evidence(evidence_path, [("news_001", "c01", None)])

    with pytest.raises(ValueError, match="does not cover Label Studio task"):
        builder.build_dataset(export_path, [evidence_path], [body_path], tmp_path / "output")
