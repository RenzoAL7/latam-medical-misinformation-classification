import csv
import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "labelstudio" / "validate_gold.py"
SPEC = importlib.util.spec_from_file_location("validate_gold", MODULE_PATH)
assert SPEC and SPEC.loader
validate_gold = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validate_gold)


def _row(record_id: str, label: str, relation: str, **overrides: str) -> dict[str, str]:
    row = {
        "record_id": record_id,
        "country": "pe",
        "source_name": "Fuente",
        "canonical_url": f"https://example.test/{record_id}",
        "title": "Título",
        "body": "Texto completo de la noticia para entrenamiento.",
        "claim_text": "Claim verificable",
        "best_pmid": "12345678",
        "best_embedding_similarity": "0.75",
        "evidence_relation": relation,
        "label": label,
        "verification_note": "PMID 12345678 respalda la decisión.",
        "annotation_id": f"annotation-{record_id}",
        "annotator": "reviewer@example.test",
        "annotated_at": "2026-10-07T13:00:00Z",
    }
    row.update(overrides)
    return row


def test_validate_rows_splits_audit_and_binary_training() -> None:
    reviewed, training, rejected = validate_gold.validate_rows(
        [
            _row("a", "RESPALDADA", "supports"),
            _row("b", "REFUTADA", "contradicts"),
            _row("c", "NO_DETERMINABLE", "unclear", best_pmid=""),
            _row("d", "EXCLUIDA", "unclear", best_pmid=""),
        ]
    )

    assert [row["record_id"] for row in reviewed] == ["a", "b", "c", "d"]
    assert [row["record_id"] for row in training] == ["a", "b"]
    assert not rejected
    assert training[0]["final_training_eligible"] == "SI"
    assert reviewed[2]["final_training_eligible"] == "NO"
    assert all(row["validation_status"] == "COMPLETADA" for row in reviewed)


def test_validate_rows_rejects_inconsistent_and_duplicate_records() -> None:
    reviewed, training, rejected = validate_gold.validate_rows(
        [
            _row("a", "RESPALDADA", "contradicts"),
            _row("a", "REFUTADA", "contradicts"),
            _row("c", "REFUTADA", "contradicts", best_pmid=""),
        ]
    )

    assert not reviewed
    assert not training
    assert [row["rejection_reasons"] for row in rejected] == [
        "label_relation_mismatch",
        "duplicate_record_id",
        "missing_best_pmid_for_binary_label",
    ]
    assert all(row["validation_status"] == "REQUIERE_CORRECCION" for row in rejected)


def test_write_csv_includes_eligible_flag(tmp_path: Path) -> None:
    reviewed, training, rejected = validate_gold.validate_rows([_row("a", "RESPALDADA", "supports")])
    reviewed_path = tmp_path / "reviewed.csv"
    training_path = tmp_path / "training.csv"
    rejected_path = tmp_path / "rejected.csv"
    validate_gold._write_csv(reviewed_path, validate_gold.OUTPUT_FIELDS, reviewed)
    validate_gold._write_csv(training_path, validate_gold.TRAINING_FIELDS, training)
    validate_gold._write_csv(rejected_path, validate_gold.REJECT_FIELDS, rejected)

    assert len(list(csv.DictReader(reviewed_path.open(encoding="utf-8-sig")))) == 1
    training_rows = list(csv.DictReader(training_path.open(encoding="utf-8-sig")))
    assert len(training_rows) == 1
    assert list(training_rows[0]) == list(validate_gold.TRAINING_FIELDS)
    assert set(training_rows[0]) == {"record_id", "title", "body", "label", "dataset_split"}
    assert not list(csv.DictReader(rejected_path.open(encoding="utf-8-sig")))
