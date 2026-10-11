from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "labelstudio"))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = _load_module("export_annotations", ROOT / "labelstudio" / "export_annotations.py")
trainer = _load_module("train_baseline", ROOT / "scripts" / "03_train_baseline.py")


def test_offline_export_joins_evidence_body_and_latest_annotation(tmp_path: Path) -> None:
    evidence_path = tmp_path / "silver_evidence_run.csv"
    with evidence_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["record_id", "body", "topic"])
        writer.writeheader()
        writer.writerow({"record_id": "r1", "body": "Article body", "topic": "health"})

    payload = [
        {
            "data": {"record_id": "r1", "title": "Article title"},
            "annotations": [
                {
                    "id": 1,
                    "completed": True,
                    "updated_at": "2026-10-11T08:00:00Z",
                    "result": [],
                },
                {
                    "id": 2,
                    "completed": True,
                    "updated_at": "2026-10-11T09:00:00Z",
                    "completed_by": "reviewer",
                    "result": [
                        {"from_name": "evidence_relation", "value": {"choices": ["supports"]}},
                        {"from_name": "final_label", "value": {"choices": ["RESPALDADA"]}},
                    ],
                },
            ],
        }
    ]

    rows = exporter._gold_rows(payload, exporter._evidence_rows([str(evidence_path)]))

    assert rows[0]["body"] == "Article body"
    assert rows[0]["label"] == "RESPALDADA"
    assert rows[0]["annotation_id"] == 2


def test_training_loader_keeps_only_complete_binary_rows(tmp_path: Path) -> None:
    path = tmp_path / "gold_training.csv"
    rows = [
        {"record_id": "r1", "title": "Title 1", "body": "Body 1", "label": "RESPALDADA"},
        {"record_id": "r2", "title": "Title 2", "body": "Body 2", "label": "REFUTADA"},
        {"record_id": "r3", "title": "Title 3", "body": "Body 3", "label": "NO_DETERMINABLE"},
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    loaded = trainer.load_training_rows(path)

    assert [row["record_id"] for row in loaded] == ["r1", "r2"]
