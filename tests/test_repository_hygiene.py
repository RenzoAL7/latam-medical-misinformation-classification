from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_input_and_generated_artifacts_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert not any(
        path.endswith(".csv") and (REPOSITORY_ROOT / path).exists()
        for path in tracked
    )
    assert not any(path.startswith("artifacts/") for path in tracked)
    assert not any(path.startswith("data/raw/") for path in tracked)


def test_obsolete_binary_baseline_is_removed() -> None:
    obsolete = [
        "experiments/models/01_tfidf_naive_bayes/baseline.ipynb",
        "experiments/models/README.md",
        "docs/training_baseline.md",
        "gold/training/training_20261019.csv",
    ]
    assert all(
        not (REPOSITORY_ROOT / path).exists()
        or not any((REPOSITORY_ROOT / path).iterdir())
        for path in obsolete
    )


def test_removed_cloud_and_legacy_paths_are_absent() -> None:
    obsolete = [
        "labelstudio",
        "oci",
    ]
    assert all(
        not (REPOSITORY_ROOT / path).exists()
        or not any((REPOSITORY_ROOT / path).iterdir())
        for path in obsolete
    )


def test_local_model_pipeline_components_remain() -> None:
    retained = [
        "configs/models.json",
        "scripts/train_seq2seq.py",
        "scripts/run_inference.py",
        "scripts/evaluate_claims.py",
        "src/claim_extraction/registry.py",
    ]
    assert all((REPOSITORY_ROOT / path).is_file() for path in retained)


def test_removed_external_claim_api_names_are_absent() -> None:
    result = subprocess.run(
        [
            "rg",
            "-n",
            "-i",
            "gemini|google-generative|generativelanguage|label.?studio",
            "src",
            "scripts",
            "configs",
        ],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout
