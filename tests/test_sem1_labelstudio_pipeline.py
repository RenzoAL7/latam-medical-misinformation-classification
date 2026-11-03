from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from claim_extraction.workflow import evaluate_predictions, validate_sem1_splits


def _reference_rows(prefix: str, count: int) -> pd.DataFrame:
    rows = []
    for index in range(count):
        record_id = f"{prefix}-{index}"
        source_text = f"Noticia médica única {prefix} {index}."
        claim = f"La afirmación médica número {prefix} {index} es verificable."
        rows.append(
            {
                "record_id": record_id,
                "source_text": source_text,
                "reference_claim": claim,
                "source_hash": hashlib.sha256(source_text.encode()).hexdigest()[:16],
                "claim_hash": hashlib.sha256(claim.encode()).hexdigest()[:16],
            }
        )
    return pd.DataFrame(rows)


def test_validate_splits_rejects_leakage_and_accepts_disjoint_csvs(tmp_path: Path) -> None:
    paths = {name: tmp_path / f"{name}.csv" for name in ("train", "validation", "test")}
    for name, frame in {
        "train": _reference_rows("train", 2),
        "validation": _reference_rows("validation", 1),
        "test": _reference_rows("test", 1),
    }.items():
        frame.to_csv(paths[name], index=False)

    splits = validate_sem1_splits(
        paths, expected_sizes={"train": 2, "validation": 1, "test": 1}
    )
    assert {name: len(frame) for name, frame in splits.items()} == {
        "train": 2,
        "validation": 1,
        "test": 1,
    }

    leaked = _reference_rows("train", 1)
    leaked.to_csv(paths["test"], index=False)
    with pytest.raises(ValueError, match="fuga"):
        validate_sem1_splits(
            paths, expected_sizes={"train": 2, "validation": 1, "test": 1}
        )


def test_evaluation_uses_reference_claims_without_external_model_dependencies() -> None:
    references = _reference_rows("test", 2)
    predictions = pd.DataFrame(
        [
            {
                "record_id": row.record_id,
                "model_id": "dry_run",
                "model_display_name": "Dry run",
                "claim": row.reference_claim,
            }
            for row in references.itertuples(index=False)
        ]
    )

    summary, details = evaluate_predictions(
        predictions,
        references,
        bertscore_scorer=lambda references, predictions: [(1.0, 1.0, 1.0)] * len(predictions),
        meteor_scorer=lambda reference, prediction: 1.0,
    )

    assert len(details) == 2
    result = summary.iloc[0]
    assert result["model_id"] == "dry_run"
    assert int(result["prediction_n"]) == 2
    assert int(result["reference_n"]) == 2
    assert float(result["meteor"]) == 1.0
    assert float(result["rouge_l_f1"]) == 1.0
    assert float(result["bertscore_f1"]) == 1.0


def test_evaluation_rejects_duplicate_predictions_for_one_model() -> None:
    references = _reference_rows("test", 1)
    predictions = pd.DataFrame(
        [
            {"record_id": "test-0", "model_id": "m", "claim": "Una afirmación."},
            {"record_id": "test-0", "model_id": "m", "claim": "Otra afirmación."},
        ]
    )
    with pytest.raises(ValueError, match="duplicadas"):
        evaluate_predictions(
            predictions,
            references,
            bertscore_scorer=lambda references, predictions: [],
            meteor_scorer=lambda reference, prediction: 0.0,
        )
