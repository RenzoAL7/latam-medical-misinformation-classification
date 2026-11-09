from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_NOTEBOOKS = {
    "01_auditar_export_labelstudio.ipynb",
    "02_construir_splits_train_val_test.ipynb",
    "03_entrenar_modelos.ipynb",
    "04_evaluar_y_visualizar_modelos.ipynb",
    "05_clasificar_temas.ipynb",
}


def test_input_and_generated_artifacts_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    result_run_ids = {
        "sem1_20261031_400_claims_v1",
        "sem1_20261102_400_claims_controlled_5ep_v2",
    }
    table_names = [
        "01_distribucion_particiones",
        "02_longitudes_palabras",
        "03_distribucion_pais_territorio",
        "04_checkpoints_seleccionados",
        "05_metricas_principales",
        "06_desglose_precision_recall_f1",
        "07_diagnosticos_generacion",
    ]
    result_tables = {
        f"docs/results/{run_id}/tables/{name}.csv"
        for run_id in result_run_ids
        for name in table_names
    }
    for path in tracked:
        if path.endswith(".csv") and (REPOSITORY_ROOT / path).exists():
            assert path in result_tables
            with (REPOSITORY_ROOT / path).open(encoding="utf-8-sig", newline="") as stream:
                headers = set(csv.DictReader(stream).fieldnames or [])
            assert headers
            assert headers.isdisjoint(
                {
                    "record_id", "title", "body", "source_text", "input_text",
                    "claim", "reference_claim",
                }
            )
    assert not any(path.startswith("artifacts/") for path in tracked)
    assert not any(
        path.startswith("data/") and (REPOSITORY_ROOT / path).exists()
        for path in tracked
    )


def test_obsolete_binary_baseline_is_removed() -> None:
    obsolete = [
        "experiments/models/01_tfidf_naive_bayes/baseline.ipynb",
        "experiments/models/README.md",
        "docs/training_baseline.md",
    ]
    assert all(
        not (REPOSITORY_ROOT / path).exists()
        or not any((REPOSITORY_ROOT / path).iterdir())
        for path in obsolete
    )


def test_notebook_first_components_are_the_only_operational_entrypoints() -> None:
    notebooks = {path.name for path in (REPOSITORY_ROOT / "notebooks").glob("*.ipynb")}
    assert notebooks == ACTIVE_NOTEBOOKS
    assert not (REPOSITORY_ROOT / "scripts").exists()
    assert not (REPOSITORY_ROOT / "Makefile").exists()
    retained = [
        "configs/models.json",
        "src/claim_extraction/workflow.py",
        "src/claim_extraction/registry.py",
    ]
    assert all((REPOSITORY_ROOT / path).is_file() for path in retained)


def test_requirements_installs_the_complete_notebook_environment() -> None:
    requirements = (REPOSITORY_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "-e .[analysis,models,dev]" in requirements


def test_notebooks_are_unexecuted_and_do_not_call_legacy_cli_files() -> None:
    for name in ACTIVE_NOTEBOOKS:
        notebook = json.loads((REPOSITORY_ROOT / "notebooks" / name).read_text(encoding="utf-8"))
        for cell in notebook["cells"]:
            if cell.get("cell_type") != "code":
                continue
            assert not cell.get("outputs")
            source = "".join(cell.get("source", []))
            assert "scripts/" not in source
            assert "subprocess" not in source
            compile(source, f"{name}:cell", "exec")


def test_removed_cloud_and_legacy_paths_are_absent() -> None:
    obsolete = ["labelstudio", "oci"]
    assert all(
        not (REPOSITORY_ROOT / path).exists()
        or not any((REPOSITORY_ROOT / path).iterdir())
        for path in obsolete
    )


def test_removed_external_claim_api_names_are_absent() -> None:
    result = subprocess.run(
        [
            "rg",
            "-n",
            "-i",
            "gemini|google-generative|generativelanguage",
            "src",
            "configs",
            "notebooks",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout
