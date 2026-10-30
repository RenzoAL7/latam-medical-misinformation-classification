from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]


def _task(record_id: str, split: str, claim: str) -> dict:
    return {
        "data": {
            "record_id": record_id,
            "title": f"Título {record_id}",
            "body": f"Cuerpo médico suficiente de {record_id}.",
            "country": "pe",
            "topic": "health",
        },
        "annotations": [
            {
                "result": [
                    {
                        "from_name": "news_eligibility",
                        "value": {"choices": ["APTA_CON_CLAIM"]},
                    },
                    {
                        "from_name": "reference_claim",
                        "value": {"text": [claim]},
                    },
                ]
            }
        ],
        "_split": split,
    }


def test_labelstudio_export_respects_manifest_and_builds_fixed_splits(tmp_path: Path) -> None:
    tasks = [
        _task("train-1", "train", "Claim de entrenamiento."),
        _task("validation-1", "validation", "Claim de validación."),
        _task("test-1", "test", "Claim de prueba."),
        _task("reserve-1", "train", "No debe entrar al Gold."),
    ]
    tasks[-1]["annotations"][0]["result"][0]["value"]["choices"] = [
        "SIN_CLAIM_MEDICO"
    ]
    tasks[-1]["annotations"][0]["result"].pop()
    export = tmp_path / "labelstudio.json"
    export.write_text(json.dumps([{key: value for key, value in task.items() if key != "_split"} for task in tasks]), encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_sem1_manifest.py"),
            "--input",
            str(export),
            "--output",
            str(manifest),
            "--train",
            "1",
            "--validation",
            "1",
            "--test",
            "1",
        ],
        check=True,
    )

    gold = tmp_path / "reference_claims.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_annotation_gold.py"),
            "--input",
            str(export),
            "--manifest",
            str(manifest),
            "--output",
            str(gold),
            "--require-complete",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"gold_rows": 3' in result.stdout
    gold_frame = pd.read_csv(gold)
    assert set(gold_frame["record_id"]) == {"train-1", "validation-1", "test-1"}
    assert "reserve-1" not in gold_frame["record_id"].tolist()

    outputs = {split: tmp_path / f"{split}.csv" for split in ("train", "validation", "test")}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_sem1_splits.py"),
            "--input",
            str(gold),
            "--train-output",
            str(outputs["train"]),
            "--validation-output",
            str(outputs["validation"]),
            "--test-output",
            str(outputs["test"]),
            "--expected-train",
            "1",
            "--expected-validation",
            "1",
            "--expected-test",
            "1",
        ],
        check=True,
    )
    split_record_ids = set()
    for output in outputs.values():
        frame = pd.read_csv(output)
        assert len(frame) == 1
        split_record_ids.update(frame["record_id"])
    assert split_record_ids == {"train-1", "validation-1", "test-1"}


def test_full_sem1_dry_run_with_700_tasks_and_300_gold_rows(tmp_path: Path) -> None:
    """Prueba el tamaño real del flujo sin cargar pesos ni tocar datos reales."""
    apt_tasks = [
        _task(
            f"apt-{index:03d}",
            "",
            f"La afirmación médica número {index} es verificable.",
        )
        for index in range(300)
    ]
    for index, task in enumerate(apt_tasks):
        task["data"]["body"] = f"Cuerpo médico único {index} con información verificable."

    non_claim_tasks = [
        _task(f"sin-claim-{index:03d}", "", "") for index in range(240)
    ]
    noisy_tasks = [
        _task(f"ruido-{index:03d}", "", "") for index in range(160)
    ]
    for task in non_claim_tasks:
        task["annotations"][0]["result"] = [
            {"from_name": "news_eligibility", "value": {"choices": ["SIN_CLAIM_MEDICO"]}}
        ]
    for task in noisy_tasks:
        task["annotations"][0]["result"] = [
            {
                "from_name": "news_eligibility",
                "value": {"choices": ["NO_UTILIZABLE_POR_RUIDO"]},
            }
        ]

    export = tmp_path / "export_700.json"
    export.write_text(
        json.dumps(apt_tasks + non_claim_tasks + noisy_tasks), encoding="utf-8"
    )
    manifest = tmp_path / "manifest_final_300.csv"
    build_manifest = [
        sys.executable,
        str(ROOT / "scripts" / "build_sem1_manifest.py"),
        "--input",
        str(export),
        "--output",
        str(manifest),
    ]
    manifest_result = subprocess.run(
        build_manifest, check=True, capture_output=True, text=True
    )
    assert '"exported_tasks": 700' in manifest_result.stdout
    assert '"selected": 300' in manifest_result.stdout
    assert len(pd.read_csv(manifest)) == 300
    assert subprocess.run(build_manifest, check=False).returncode != 0

    gold = tmp_path / "reference_claims.csv"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_annotation_gold.py"),
            "--input",
            str(export),
            "--manifest",
            str(manifest),
            "--output",
            str(gold),
            "--require-complete",
        ],
        check=True,
    )
    gold_frame = pd.read_csv(gold)
    assert len(gold_frame) == 300
    assert gold_frame["split"].value_counts().to_dict() == {
        "train": 240,
        "validation": 30,
        "test": 30,
    }

    outputs = {split: tmp_path / f"{split}.csv" for split in ("train", "validation", "test")}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_sem1_splits.py"),
            "--input",
            str(gold),
            "--train-output",
            str(outputs["train"]),
            "--validation-output",
            str(outputs["validation"]),
            "--test-output",
            str(outputs["test"]),
        ],
        check=True,
    )
    assert {split: len(pd.read_csv(path)) for split, path in outputs.items()} == {
        "train": 240,
        "validation": 30,
        "test": 30,
    }

    predictions = tmp_path / "predictions.jsonl"
    test_frame = pd.read_csv(outputs["test"])
    predictions.write_text(
        "".join(
            json.dumps(
                {
                    "record_id": row.record_id,
                    "model_id": "dry-run",
                    "model_display_name": "Dry run",
                    "claim": row.reference_claim,
                },
                ensure_ascii=False,
            )
            + "\n"
            for row in test_frame.itertuples(index=False)
        ),
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "evaluate_claims.py"),
            "--predictions",
            str(predictions),
            "--references",
            str(outputs["test"]),
            "--output-dir",
            str(evaluation),
        ],
        check=True,
    )
    summary = pd.read_csv(evaluation / "summary.csv").iloc[0]
    assert int(summary["prediction_n"]) == 30
    assert int(summary["reference_n"]) == 30
    assert float(summary["meteor"]) > 0.99
    assert float(summary["rouge_l_f1"]) == 1.0
    assert float(summary["exact_match"]) == 1.0
