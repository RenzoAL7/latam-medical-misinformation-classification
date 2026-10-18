from __future__ import annotations

import csv
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from experiments.models.common import prepare_experiment


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "experiments" / "models"


def _gold_row(record_id: str, label: str) -> dict[str, str]:
    return {
        "record_id": record_id,
        "title": f"Titulo {record_id}",
        "body": f"Cuerpo completo {record_id}",
        "label": label,
        "claim_text": "Este claim es solo para auditoria, no es una entrada del modelo.",
        "validation_status": "COMPLETADA",
        "final_training_eligible": "SI",
    }


def _write_gold(path: Path) -> None:
    rows = [
        _gold_row(f"article-{index}", "REFUTADA" if index % 2 == 0 else "RESPALDADA")
        for index in range(8)
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def _write_manifest(path: Path) -> None:
    rows = [
        {"record_id": "article-0", "dataset_split": "train", "label": "REFUTADA"},
        {"record_id": "article-1", "dataset_split": "train", "label": "RESPALDADA"},
        {"record_id": "article-2", "dataset_split": "train", "label": "REFUTADA"},
        {"record_id": "article-3", "dataset_split": "train", "label": "RESPALDADA"},
        {"record_id": "article-4", "dataset_split": "validation", "label": "REFUTADA"},
        {"record_id": "article-5", "dataset_split": "validation", "label": "RESPALDADA"},
        {"record_id": "article-6", "dataset_split": "test", "label": "REFUTADA"},
        {"record_id": "article-7", "dataset_split": "test", "label": "RESPALDADA"},
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_experiment_reuses_same_article_split_and_title_body_only(tmp_path: Path) -> None:
    gold = tmp_path / "training.csv"
    manifest = tmp_path / "split_manifest.csv"
    _write_gold(gold)
    _write_manifest(manifest)
    args = Namespace(
        input=gold,
        split_manifest=manifest,
        limit=8,
        min_rows=8,
        seed=42,
    )

    prepared = prepare_experiment(args)

    assert len(prepared["selected"]) == 8
    assert {row["record_id"] for row in prepared["selected"]} == {
        f"article-{index}" for index in range(8)
    }
    assert prepared["assignments"]["article-6"] == "test"
    assert prepared["baseline"].build_text(prepared["selected"][0]) == "Titulo article-0\n\nCuerpo completo article-0"
    assert "claim_text" not in prepared["baseline"].build_text(prepared["selected"][0])


def test_experiment_rejects_manifest_label_mismatch(tmp_path: Path) -> None:
    gold = tmp_path / "training.csv"
    manifest = tmp_path / "split_manifest.csv"
    _write_gold(gold)
    _write_manifest(manifest)
    content = manifest.read_text(encoding="utf-8").replace(
        "article-0,train,REFUTADA", "article-0,train,RESPALDADA"
    )
    manifest.write_text(content, encoding="utf-8")
    args = Namespace(input=gold, split_manifest=manifest, limit=8, min_rows=8, seed=42)

    with pytest.raises(ValueError, match="Label mismatch"):
        prepare_experiment(args)


def test_every_model_folder_exposes_help_without_optional_ml_packages() -> None:
    entrypoints = sorted(MODELS.rglob("train.py"))
    assert len(entrypoints) == 9

    for entrypoint in entrypoints:
        result = subprocess.run(
            [sys.executable, str(entrypoint), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, f"{entrypoint}: {result.stderr}"
        assert "--input" in result.stdout
        assert "--split-manifest" in result.stdout
