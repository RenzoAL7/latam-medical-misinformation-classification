from __future__ import annotations

import subprocess
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_repository_does_not_track_csv_data() -> None:
    result = subprocess.run(
        ["git", "ls-files", "*.csv"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == ""


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
