import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "labelstudio" / "sync_evidence_from_object_storage.py"
SPEC = importlib.util.spec_from_file_location("sync_evidence_from_object_storage", MODULE_PATH)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def test_sync_accepts_only_current_evidence_outputs() -> None:
    assert sync._evidence_object_name(
        "silver/evidence/evidence_run_20261009T130636Z_8a27e68e.csv", "silver/evidence"
    )
    assert not sync._evidence_object_name(
        "silver/evidence/evidence_translated_run_20261009T020412Z_02085de4.csv", "silver/evidence"
    )
    assert not sync._evidence_object_name(
        "silver/rejected/evidence/rejected_evidence_run.csv", "silver/evidence"
    )


def test_sync_cache_path_is_stable_and_flat(tmp_path: Path) -> None:
    first = sync._local_csv_path(tmp_path, "silver/evidence/evidence_run_20261009T130636Z_a.csv")
    second = sync._local_csv_path(tmp_path, "silver/evidence/evidence_run_20261009T130636Z_a.csv")

    assert first == second
    assert first.parent == tmp_path
    assert first.name.endswith("evidence_run_20261009T130636Z_a.csv")
