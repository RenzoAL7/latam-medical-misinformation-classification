from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_repository_tracks_only_the_declared_gold_snapshot() -> None:
    expected = {
        REPOSITORY_ROOT / "gold" / "reviewed" / "article_labels_20261019.csv",
        REPOSITORY_ROOT / "gold" / "reviewed" / "claim_reviews_20261019.csv",
        REPOSITORY_ROOT / "gold" / "training" / "training_20261019.csv",
        REPOSITORY_ROOT / "gold" / "rejected" / "rejected_20261019.csv",
    }
    tracked_csvs = {
        REPOSITORY_ROOT / path
        for path in subprocess.run(
            ["git", "ls-files", "*.csv"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    }
    present_csvs = set((REPOSITORY_ROOT / "gold").rglob("*.csv")) if (REPOSITORY_ROOT / "gold").is_dir() else set()

    assert tracked_csvs <= expected
    assert present_csvs <= expected
    assert expected <= present_csvs


def test_legacy_local_collector_is_not_present() -> None:
    obsolete_paths = [
        ".env.example",
        "configs/batch.yaml",
        "scripts/01_collect_newsdata_urls.py",
        "src/peruvian_medical_misinformation",
        "tests/test_batch_contract.py",
        "tests/test_newsdata_batch.py",
        "data/00_control",
        "data/01_candidates",
        "reports/runs",
        "labelstudio/config.xml",
        "labelstudio/LOCAL_WORKFLOW.md",
        "tests/fixtures/labelstudio_multiclaim.fixture",
    ]

    assert all(not (REPOSITORY_ROOT / path).exists() for path in obsolete_paths)
