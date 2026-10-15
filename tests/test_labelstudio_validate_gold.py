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
