#!/usr/bin/env python3
"""Train a small reproducible Spanish-news TF-IDF baseline."""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LABEL_TO_ID = {"REFUTADA": 0, "RESPALDADA": 1}
ID_TO_LABEL = {value: key for key, value in LABEL_TO_ID.items()}
REQUIRED_COLUMNS = ("record_id", "title", "body", "label")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def load_training_rows(path: Path) -> list[dict[str, str]]:
    """Load only complete, human-validated binary rows."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = [field for field in REQUIRED_COLUMNS if field not in fields]
        if missing:
            raise ValueError(f"Input is missing required columns: {', '.join(missing)}")

        rows: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for line_number, source_row in enumerate(reader, start=2):
            row = {str(key): _clean(value) for key, value in source_row.items() if key}
            record_id = row.get("record_id", "")
            if record_id in seen_ids:
                raise ValueError(f"Duplicate record_id at CSV line {line_number}: {record_id}")
            if record_id:
                seen_ids.add(record_id)

            if row.get("label") not in LABEL_TO_ID:
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
    """Select a deterministic subset while retaining both binary labels."""

    if limit <= 0:
        raise ValueError("--limit must be greater than zero")
    if len(rows) <= limit:
        return sorted(rows, key=lambda row: row["record_id"])

    grouped: dict[str, list[dict[str, str]]] = {label: [] for label in LABEL_TO_ID}
    for row in rows:
        grouped[row["label"]].append(row)
    if any(not values for values in grouped.values()):
        raise ValueError("The pilot subset needs both RESPALDADA and REFUTADA rows")

    allocations = {
        label: max(1, round(limit * len(values) / len(rows)))
        for label, values in grouped.items()
    }
    while sum(allocations.values()) < limit:
        label = max(grouped, key=lambda item: len(grouped[item]) - allocations[item])
        if allocations[label] >= len(grouped[label]):
            break
        allocations[label] += 1
    while sum(allocations.values()) > limit:
        label = max(allocations, key=allocations.get)
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


def _split_rows(rows: list[dict[str, str]], seed: int) -> dict[str, list[dict[str, str]]]:
    try:
        from sklearn.model_selection import train_test_split
    except ImportError as exc:
        raise RuntimeError("Instala dependencias: python3 -m pip install scikit-learn joblib") from exc

    labels = [row["label"] for row in rows]
    train, remainder = train_test_split(
        rows,
        test_size=0.30,
        random_state=seed,
        stratify=labels,
    )
    remainder_labels = [row["label"] for row in remainder]
    validation, test = train_test_split(
        remainder,
        test_size=0.50,
        random_state=seed,
        stratify=remainder_labels,
    )
    return {"train": train, "validation": validation, "test": test}


def _text(row: dict[str, str]) -> str:
    return f"{row['title']}\n\n{row['body']}"


def _metrics(y_true: list[int], y_pred: Any) -> dict[str, Any]:
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

    return {
        "n": len(y_true),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist(),
        "classification_report": classification_report(
            y_true,
            y_pred,
            labels=[0, 1],
            target_names=[ID_TO_LABEL[0], ID_TO_LABEL[1]],
            output_dict=True,
            zero_division=0,
        ),
    }


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
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
        raise ValueError(f"Only {len(rows)} binary rows are ready; at least {min_rows} are required")
    selected = select_rows(rows, limit, seed)
    if len(selected) < min_rows:
        raise ValueError(f"The selected pilot has {len(selected)} rows; increase --limit")
    if set(row["label"] for row in selected) != set(LABEL_TO_ID):
        raise ValueError("The selected pilot must contain RESPALDADA and REFUTADA rows")

    try:
        from joblib import dump
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
    except ImportError as exc:
        raise RuntimeError("Instala dependencias: python3 -m pip install scikit-learn joblib") from exc

    splits = _split_rows(selected, seed)
    model = Pipeline(
        [
            ("tfidf", TfidfVectorizer(lowercase=True, strip_accents="unicode", ngram_range=(1, 2), max_features=30000)),
            ("classifier", LogisticRegression(class_weight="balanced", max_iter=1000, random_state=seed, solver="liblinear")),
        ]
    )
    model.fit([_text(row) for row in splits["train"]], [LABEL_TO_ID[row["label"]] for row in splits["train"]])

    metrics: dict[str, Any] = {}
    predictions: list[dict[str, Any]] = []
    for split_name, split_rows in splits.items():
        y_true = [LABEL_TO_ID[row["label"]] for row in split_rows]
        y_pred = model.predict([_text(row) for row in split_rows])
        metrics[split_name] = _metrics(y_true, y_pred)
        for row, true_id, predicted_id in zip(split_rows, y_true, y_pred):
            predictions.append(
                {
                    "record_id": row["record_id"],
                    "dataset_split": split_name,
                    "true_label": ID_TO_LABEL[true_id],
                    "predicted_label": ID_TO_LABEL[int(predicted_id)],
                    "correct": int(true_id == int(predicted_id)),
                }
            )

    run_dir = output_dir / run_name
    if run_dir.exists() and any(run_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"Output already exists: {run_dir}; use --overwrite to replace it")
    run_dir.mkdir(parents=True, exist_ok=True)
    dump(model, run_dir / "model.joblib")
    _write_csv(
        run_dir / "predictions.csv",
        ["record_id", "dataset_split", "true_label", "predicted_label", "correct"],
        predictions,
    )
    _write_csv(
        run_dir / "split_manifest.csv",
        ["record_id", "dataset_split", "label", "source_name", "topic"],
        [
            {
                "record_id": row["record_id"],
                "dataset_split": split_name,
                "label": row["label"],
                "source_name": row.get("source_name", ""),
                "topic": row.get("topic", ""),
            }
            for split_name, split_rows in splits.items()
            for row in split_rows
        ],
    )
    summary = {
        "status": "ok",
        "input_csv": str(input_path),
        "run_dir": str(run_dir),
        "features": "title + body",
        "labels": LABEL_TO_ID,
        "rows": len(selected),
        "class_counts": dict(Counter(row["label"] for row in selected)),
        "metrics": metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Validated binary Gold CSV")
    parser.add_argument("--output-dir", type=Path, default=Path("labelstudio/artifacts"))
    parser.add_argument("--run-name", default="pilot_100")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--min-rows", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        run_dir = run_training(
            args.input,
            args.output_dir,
            args.run_name,
            args.limit,
            args.min_rows,
            args.seed,
            args.overwrite,
        )
    except (OSError, RuntimeError, ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "ok", "run_dir": str(run_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
