from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_repository_does_not_track_generated_csvs() -> None:
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

    # A local Gold CSV is useful for the notebook, but generated datasets must
    # stay outside Git so that annotations and snapshots are not published.
    assert not tracked_csvs


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
