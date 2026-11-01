from __future__ import annotations

import json
import os
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
        _task("train-1", "train", "Claim central de entrenamiento verificable."),
        _task("validation-1", "validation", "Claim central de validación verificable."),
        _task("test-1", "test", "Claim central de prueba verificable."),
        _task("reserve-1", "train", "No debe entrar al conjunto de referencia."),
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

    reference_set = tmp_path / "reference_claims.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_reference_set.py"),
            "--input",
            str(export),
            "--manifest",
            str(manifest),
            "--output",
            str(reference_set),
            "--require-complete",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"reference_rows": 3' in result.stdout
    reference_frame = pd.read_csv(reference_set)
    assert set(reference_frame["record_id"]) == {"train-1", "validation-1", "test-1"}
    assert "reserve-1" not in reference_frame["record_id"].tolist()

    outputs = {split: tmp_path / f"{split}.csv" for split in ("train", "validation", "test")}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_sem1_splits.py"),
            "--input",
            str(reference_set),
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


def test_full_sem1_dry_run_with_700_tasks_and_400_reference_rows(tmp_path: Path) -> None:
    """Prueba el tamaño real del flujo sin cargar pesos ni tocar datos reales."""
    apt_tasks = [
        _task(
            f"apt-{index:03d}",
            "",
            f"La afirmación médica número {index} es verificable.",
        )
        for index in range(400)
    ]
    for index, task in enumerate(apt_tasks):
        task["data"]["body"] = f"Cuerpo médico único {index} con información verificable."

    non_claim_tasks = [
        _task(f"sin-claim-{index:03d}", "", "") for index in range(200)
    ]
    noisy_tasks = [
        _task(f"ruido-{index:03d}", "", "") for index in range(100)
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
    manifest = tmp_path / "manifest_final_400.csv"
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
    assert '"selected": 400' in manifest_result.stdout
    assert len(pd.read_csv(manifest)) == 400
    assert subprocess.run(build_manifest, check=False).returncode != 0

    reference_set = tmp_path / "reference_claims.csv"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "import_reference_set.py"),
            "--input",
            str(export),
            "--manifest",
            str(manifest),
            "--output",
            str(reference_set),
            "--require-complete",
        ],
        check=True,
    )
    reference_frame = pd.read_csv(reference_set)
    assert len(reference_frame) == 400
    assert reference_frame["split"].value_counts().to_dict() == {
        "train": 320,
        "validation": 40,
        "test": 40,
    }

    outputs = {split: tmp_path / f"{split}.csv" for split in ("train", "validation", "test")}
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_sem1_splits.py"),
            "--input",
            str(reference_set),
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
        "train": 320,
        "validation": 40,
        "test": 40,
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
    bertscore_stub = tmp_path / "bertscore_stub"
    bertscore_stub.mkdir()
    (bertscore_stub / "bert_score.py").write_text(
        "class BERTScorer:\n"
        "    def __init__(self, **kwargs):\n"
        "        pass\n"
        "    def score(self, cands, refs):\n"
        "        values = [1.0] * len(cands)\n"
        "        return values, values, values\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(bertscore_stub), environment.get("PYTHONPATH", "")]
    )
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
        env=environment,
    )
    summary = pd.read_csv(evaluation / "summary.csv").iloc[0]
    assert int(summary["prediction_n"]) == 40
    assert int(summary["reference_n"]) == 40
    assert float(summary["meteor"]) > 0.99
    assert float(summary["rouge_l_precision"]) == 1.0
    assert float(summary["rouge_l_recall"]) == 1.0
    assert float(summary["rouge_l_f1"]) == 1.0
    assert float(summary["bertscore_precision"]) == 1.0
    assert float(summary["bertscore_recall"]) == 1.0
    assert float(summary["bertscore_f1"]) == 1.0


def test_audit_export_keeps_all_tasks_and_flags_duplicates(tmp_path: Path) -> None:
    first = _task("apt-legacy", "", "Un mismo claim médico verificable.")
    first["annotations"][0]["result"] = [
        {"from_name": "news_utility", "value": {"choices": ["APTA"]}},
        {
            "from_name": "news_eligibility",
            "value": {"choices": ["APTA_CON_CLAIM"]},
        },
        {
            "from_name": "reference_claim",
            "value": {"text": ["Un mismo claim médico verificable."]},
        },
    ]
    second = _task("apt-duplicate", "", "Un mismo claim médico verificable.")
    second["data"]["body"] = "Otro cuerpo fuente para el mismo claim."
    non_apt = _task("sin-claim", "", "Claim escrito por error.")
    non_apt["annotations"][0]["result"][0]["value"]["choices"] = [
        "SIN_CLAIM_MEDICO"
    ]

    export = tmp_path / "labelstudio.json"
    export.write_text(json.dumps([first, second, non_apt]), encoding="utf-8")
    audit = tmp_path / "audit.csv"
    duplicates = tmp_path / "duplicates.csv"
    summary = tmp_path / "summary.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "audit_labelstudio_export.py"),
            "--input",
            str(export),
            "--audit-output",
            str(audit),
            "--duplicates-output",
            str(duplicates),
            "--summary-output",
            str(summary),
        ],
        check=True,
    )

    audit_frame = pd.read_csv(audit)
    assert len(audit_frame) == 3
    assert set(audit_frame) >= {
        "reference_claim",
        "source_hash",
        "claim_hash",
        "eligible_for_reference_set",
        "exact_claim_duplicate_group",
    }
    assert (
        audit_frame.loc[audit_frame["record_id"] == "apt-legacy", "news_eligibility"].iloc[0]
        == "APTA_CON_CLAIM"
    )
    assert audit_frame["exact_claim_duplicate_group"].notna().sum() == 2
    assert "reference_claim_sin_elegibilidad_apta" in (
        audit_frame.loc[audit_frame["record_id"] == "sin-claim", "quality_issue"].iloc[0]
    )

    duplicate_frame = pd.read_csv(duplicates)
    assert "exact_claim" in duplicate_frame["duplicate_type"].tolist()
    summary_data = json.loads(summary.read_text(encoding="utf-8"))
    assert summary_data["tasks"] == 3
    assert summary_data["eligible_after_exact_source_and_claim_deduplication"] == 1


def test_manifest_applies_reviewed_record_exclusions(tmp_path: Path) -> None:
    tasks = [
        _task("keep", "", "Claim central que se conserva aquí."),
        _task("drop", "", "Claim central que se excluye tras revisión."),
        _task("other", "", "Otro claim médico válido disponible hoy."),
        _task("third", "", "Tercer claim médico válido disponible hoy."),
        _task("too-short", "", "Corto."),
        _task("too-long", "", " ".join(["palabra"] * 81)),
    ]
    export = tmp_path / "labelstudio.json"
    export.write_text(json.dumps(tasks), encoding="utf-8")
    exclusions = tmp_path / "exclusions.csv"
    exclusions.write_text("record_id,reason\ndrop,noticia_republicada\n", encoding="utf-8")
    manifest = tmp_path / "manifest.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_sem1_manifest.py"),
            "--input",
            str(export),
            "--output",
            str(manifest),
            "--exclude-record-ids",
            str(exclusions),
            "--train",
            "1",
            "--validation",
            "1",
            "--test",
            "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"manual_exclusions": 1' in result.stdout
    selected = pd.read_csv(manifest)["record_id"].tolist()
    assert "drop" not in selected
    assert "too-short" not in selected
    assert "too-long" not in selected
