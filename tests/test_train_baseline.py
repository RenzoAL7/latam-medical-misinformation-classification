from __future__ import annotations

import csv
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "03_train_baseline.py"
SPEC = importlib.util.spec_from_file_location("train_baseline", MODULE_PATH)
assert SPEC and SPEC.loader
train_baseline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(train_baseline)


def _row(record_id: str, label: str) -> dict[str, str]:
    return {
        "record_id": record_id,
        "title": f"Título {record_id}",
        "body": f"Texto completo de la noticia {record_id}",
        "label": label,
        "final_training_eligible": "SI",
        "validation_status": "COMPLETADA",
        "source_name": "Fuente",
        "topic": "health",
    }


def test_load_training_rows_excludes_non_binary_audit_labels(tmp_path: Path) -> None:
    path = tmp_path / "gold.csv"
    rows = [_row("a", "RESPALDADA"), _row("b", "REFUTADA"), _row("c", "NO_DETERMINABLE")]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    loaded = train_baseline.load_training_rows(path)

    assert [row["record_id"] for row in loaded] == ["a", "b"]
    assert train_baseline.build_text(loaded[0]).startswith("Título a\n\nTexto")


def test_load_training_rows_rejects_binary_row_without_body(tmp_path: Path) -> None:
    path = tmp_path / "gold.csv"
    row = _row("a", "RESPALDADA")
    row["body"] = ""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)

    try:
        train_baseline.load_training_rows(path)
    except ValueError as exc:
        assert "empty body" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Expected incomplete binary row to fail")


def test_select_rows_is_deterministic_and_keeps_both_labels() -> None:
    rows = [_row(f"r-{index:03d}", "RESPALDADA" if index % 2 else "REFUTADA") for index in range(20)]

    first = train_baseline.select_rows(rows, limit=10, seed=42)
    second = train_baseline.select_rows(rows, limit=10, seed=42)

    assert [row["record_id"] for row in first] == [row["record_id"] for row in second]
    assert {row["label"] for row in first} == {"RESPALDADA", "REFUTADA"}
