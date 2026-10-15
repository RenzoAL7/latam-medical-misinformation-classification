#!/usr/bin/env python3
"""Train the first reproducible Spanish-news baseline locally.

The input must be the human-validated binary CSV produced by
``scripts/04_build_training_dataset.py``. Only ``title`` and ``body`` become model
features; claims, evidence, similarity scores, source and topic remain audit
metadata.  The default pilot requires at least 100 eligible records and uses a
stratified 70/15/15 train/validation/test split.

This script intentionally keeps the first experiment small: word-based
Spanish TF-IDF followed by class-balanced logistic regression.  A
majority-class baseline is reported beside it so a high-looking score cannot be
mistaken for useful learning on a small corpus.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


LABEL_TO_ID = {"REFUTADA": 0, "RESPALDADA": 1}
ID_TO_LABEL = {value: key for key, value in LABEL_TO_ID.items()}
REQUIRED_COLUMNS = ("record_id", "title", "body", "label")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_row(row: dict[str, Any]) -> dict[str, str]:
    return {str(key): _clean(value) for key, value in row.items() if key}


def load_training_rows(path: Path) -> list[dict[str, str]]:
    """Load binary rows and fail loudly on duplicate or incomplete records."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = [field for field in REQUIRED_COLUMNS if field not in fields]
        if missing:
            raise ValueError(f"Input is missing required columns: {', '.join(missing)}")

        rows: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for line_number, source_row in enumerate(reader, start=2):
            row = _clean_row(source_row)
            record_id = row.get("record_id", "")
            if record_id in seen_ids:
                raise ValueError(f"Duplicate record_id at CSV line {line_number}: {record_id}")
            if record_id:
                seen_ids.add(record_id)

            # A reviewed Gold file may also contain NO_DETERMINABLE or
            # EXCLUIDA. They stay in audit Gold but never enter binary training.
            if row.get("label", "") not in LABEL_TO_ID:
                continue
            if row.get("final_training_eligible", "").upper() not in {"", "SI"}:
                continue
            if row.get("validation_status", "").upper() not in {"", "COMPLETADA"}:
                continue
            if not record_id:
                raise ValueError(f"Binary row at CSV line {line_number} has no record_id")
            missing_text = [field for field in ("title", "body") if not row.get(field)]
            if missing_text:
                raise ValueError(
                    f"{record_id} is binary-eligible but has empty {', '.join(missing_text)}"
                )
            rows.append(row)

    return rows


def select_rows(rows: list[dict[str, str]], limit: int, seed: int) -> list[dict[str, str]]:
    """Select a deterministic, approximately class-balanced pilot subset."""

    if limit <= 0:
        raise ValueError("--limit must be greater than zero")
    if len(rows) <= limit:
        return sorted(rows, key=lambda row: row["record_id"])

    grouped: dict[str, list[dict[str, str]]] = {label: [] for label in LABEL_TO_ID}
    for row in rows:
        grouped[row["label"]].append(row)
    if any(not values for values in grouped.values()):
        raise ValueError("The pilot subset needs both RESPALDADA and REFUTADA rows")

    total = len(rows)
    allocations = {
        label: max(1, int(limit * len(values) / total))
        for label, values in grouped.items()
    }
    while sum(allocations.values()) < limit:
        label = max(
            grouped,
            key=lambda item: (len(grouped[item]) / total - allocations[item] / limit, item),
        )
        if allocations[label] < len(grouped[label]):
            allocations[label] += 1
        else:
            break
    while sum(allocations.values()) > limit:
        label = max(allocations, key=lambda item: (allocations[item] > 1, allocations[item], item))
        if allocations[label] <= 1:
            raise ValueError("Could not allocate the requested pilot size across both labels")
        allocations[label] -= 1

    rng = random.Random(seed)
    selected: list[dict[str, str]] = []
    for label, values in grouped.items():
        shuffled = list(values)
        rng.shuffle(shuffled)
        selected.extend(shuffled[: allocations[label]])
    return sorted(selected, key=lambda row: row["record_id"])


def build_text(row: dict[str, str]) -> str:
    """Build the only model input: title followed by article body."""

    return f"{row['title']}\n\n{row['body']}"


def stratified_split(rows: list[dict[str, str]], seed: int) -> dict[str, str]:
    """Return record_id -> split using an exact 70/15/15 stratified split."""

    try:
        from sklearn.model_selection import train_test_split
    except ImportError as exc:  # pragma: no cover - exercised in the CLI environment
        raise RuntimeError(
            "Training dependencies are missing. Install with: python3 -m pip install -e '.[training]'"
        ) from exc

    indexes = list(range(len(rows)))
    labels = [LABEL_TO_ID[row["label"]] for row in rows]
    train_indexes, remainder_indexes = train_test_split(
        indexes,
        test_size=0.30,
        random_state=seed,
        stratify=labels,
    )
    remainder_labels = [labels[index] for index in remainder_indexes]
    validation_indexes, test_indexes = train_test_split(
        remainder_indexes,
        test_size=0.50,
        random_state=seed,
        stratify=remainder_labels,
    )
    assignments: dict[str, str] = {}
    for index in train_indexes:
        assignments[rows[index]["record_id"]] = "train"
    for index in validation_indexes:
        assignments[rows[index]["record_id"]] = "validation"
    for index in test_indexes:
        assignments[rows[index]["record_id"]] = "test"
    if len(assignments) != len(rows):
        raise RuntimeError("The split did not assign every selected record exactly once")
    return assignments


def _metrics(y_true: list[int], y_pred: Any, y_score: Any | None) -> dict[str, Any]:
    from sklearn.metrics import (
        accuracy_score,
        classification_report,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    report = classification_report(
        y_true,
        y_pred,
        labels=[0, 1],
        target_names=[ID_TO_LABEL[0], ID_TO_LABEL[1]],
        output_dict=True,
        zero_division=0,
    )
    result: dict[str, Any] = {
        "n": len(y_true),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        "macro_precision": float(precision_score(y_true, y_pred, average="macro", zero_division=0)),
        "macro_recall": float(recall_score(y_true, y_pred, average="macro", zero_division=0)),
        "refutada_f1": float(report[ID_TO_LABEL[0]]["f1-score"]),
        "respaldada_f1": float(report[ID_TO_LABEL[1]]["f1-score"]),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist(),
        "classification_report": report,
    }
    if y_score is not None and len(set(y_true)) == 2:
        result["roc_auc"] = float(roc_auc_score(y_true, y_score))
    return result


def _predict(model: Any, texts: list[str]) -> tuple[Any, Any | None]:
    predictions = model.predict(texts)
    scores = None
    if hasattr(model, "predict_proba"):
        scores = model.predict_proba(texts)[:, 1]
    elif hasattr(model, "decision_function"):
        scores = model.decision_function(texts)
    return predictions, scores


def _write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_training(
    input_path: Path,
    output_dir: Path,
    run_name: str,
    limit: int,
    min_rows: int,
    seed: int,
    overwrite: bool = False,
) -> Path:
    rows = load_training_rows(input_path)
    if len(rows) < min_rows:
        raise ValueError(
            f"Only {len(rows)} binary Gold rows are ready; the pilot requires at least {min_rows}. "
            "Complete and validate more Label Studio annotations first."
        )
    selected = select_rows(rows, limit, seed)
    if len(selected) < min_rows:
        raise ValueError(f"The selected pilot has {len(selected)} rows; increase --limit to at least {min_rows}")
    selected_class_counts = Counter(row["label"] for row in selected)
    if set(selected_class_counts) != set(LABEL_TO_ID):
        raise ValueError("The selected pilot must contain both RESPALDADA and REFUTADA rows")

    assignments = stratified_split(selected, seed)
    by_split = {
        split: [row for row in selected if assignments[row["record_id"]] == split]
        for split in ("train", "validation", "test")
    }

    try:
        from joblib import dump
        from sklearn.dummy import DummyClassifier
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "Training dependencies are missing. Install with: python3 -m pip install -e '.[training]'"
        ) from exc

    train_texts = [build_text(row) for row in by_split["train"]]
    train_labels = [LABEL_TO_ID[row["label"]] for row in by_split["train"]]
    model = Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    lowercase=True,
                    strip_accents="unicode",
                    ngram_range=(1, 2),
                    min_df=1,
                    sublinear_tf=True,
                    max_features=30_000,
                ),
            ),
            (
                "classifier",
                LogisticRegression(
                    class_weight="balanced",
                    max_iter=1_000,
                    random_state=seed,
                    solver="liblinear",
                ),
            ),
        ]
    )
    model.fit(train_texts, train_labels)

    majority = DummyClassifier(strategy="most_frequent")
    majority.fit([[0] for _ in train_labels], train_labels)

    model_metrics: dict[str, Any] = {}
    majority_metrics: dict[str, Any] = {}
    prediction_rows: list[dict[str, Any]] = []
    for split, split_rows in by_split.items():
        texts = [build_text(row) for row in split_rows]
        labels = [LABEL_TO_ID[row["label"]] for row in split_rows]
        predictions, scores = _predict(model, texts)
        dummy_predictions, dummy_scores = _predict(majority, [[0] for _ in split_rows])
        model_metrics[split] = _metrics(labels, predictions, scores)
        majority_metrics[split] = _metrics(labels, dummy_predictions, dummy_scores)
        score_values = [None] * len(labels) if scores is None else list(scores)
        for row, true_id, prediction, score in zip(split_rows, labels, predictions, score_values):
            prediction_rows.append(
                {
                    "record_id": row["record_id"],
                    "dataset_split": split,
                    "source_name": row.get("source_name", ""),
                    "topic": row.get("topic", ""),
                    "true_label": ID_TO_LABEL[true_id],
                    "predicted_label": ID_TO_LABEL[int(prediction)],
                    "correct": str(int(true_id == int(prediction))),
                    "probability_respaldada": "" if score is None else round(float(score), 6),
                }
            )

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_name).strip("._")
    if not safe_name:
        safe_name = datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    run_dir = output_dir / safe_name
    if run_dir.exists() and any(run_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"Output run already exists: {run_dir}; use --overwrite to replace it")
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = [
        {
            "record_id": row["record_id"],
            "dataset_split": assignments[row["record_id"]],
            "label": row["label"],
            "label_id": LABEL_TO_ID[row["label"]],
            "source_name": row.get("source_name", ""),
            "topic": row.get("topic", ""),
        }
        for row in selected
    ]
    _write_csv(
        run_dir / "split_manifest.csv",
        ("record_id", "dataset_split", "label", "label_id", "source_name", "topic"),
        manifest_rows,
    )
    _write_csv(
        run_dir / "predictions.csv",
        (
            "record_id",
            "dataset_split",
            "source_name",
            "topic",
            "true_label",
            "predicted_label",
            "correct",
            "probability_respaldada",
        ),
        prediction_rows,
    )
    dump(model, run_dir / "model.joblib")

    summary = {
        "status": "ok",
        "input_csv": str(input_path),
        "run_dir": str(run_dir),
        "seed": seed,
        "rows_available": len(rows),
        "rows_selected": len(selected),
        "class_counts": dict(Counter(row["label"] for row in selected)),
        "split_counts": dict(Counter(assignments.values())),
        "features": ["title", "body"],
        "target": "label",
        "label_mapping": LABEL_TO_ID,
        "split": "70/15/15 stratified",
        "model": {
            "type": "TfidfVectorizer + LogisticRegression",
            "class_weight": "balanced",
            "ngram_range": [1, 2],
            "max_features": 30_000,
        },
        "metrics": {"model": model_metrics, "majority_baseline": majority_metrics},
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Validated Gold CSV")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/training_runs"))
    parser.add_argument("--run-name", default="", help="Stable output folder name")
    parser.add_argument("--limit", type=int, default=100, help="Rows in the pilot subset")
    parser.add_argument("--min-rows", type=int, default=100, help="Minimum eligible rows required")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    try:
        run_dir = run_training(
            args.input,
            args.output_dir,
            args.run_name or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ"),
            args.limit,
            args.min_rows,
            args.seed,
            args.overwrite,
        )
    except (FileNotFoundError, ValueError, FileExistsError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "run_dir": str(run_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
