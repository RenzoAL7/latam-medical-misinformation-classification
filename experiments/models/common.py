"""Shared, article-level data and evaluation contract for model experiments."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "scripts" / "03_train_baseline.py"
SPLITS = ("train", "validation", "test")
_BASELINE_MODULE: Any | None = None


def load_baseline_module() -> Any:
    """Reuse the repository's validated loader, sampler, split, and metrics."""

    global _BASELINE_MODULE
    if _BASELINE_MODULE is not None:
        return _BASELINE_MODULE
    if not BASELINE_PATH.is_file():
        raise FileNotFoundError(f"Could not locate the baseline contract: {BASELINE_PATH}")
    spec = importlib.util.spec_from_file_location("thesis_baseline_for_experiments", BASELINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the existing baseline: {BASELINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _BASELINE_MODULE = module
    return module


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, required=True, help="Gold training.csv from script 04")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/training_runs"),
        help="Local, Git-ignored directory for metrics, predictions, and model weights",
    )
    parser.add_argument("--run-name", default="", help="Stable name for this run")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Same pilot size used by the current baseline; raise it to include more articles",
    )
    parser.add_argument("--min-rows", type=int, default=100, help="Minimum eligible articles")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--split-manifest",
        type=Path,
        help="Optional split_manifest.csv from the baseline; reuses exactly those article IDs",
    )
    parser.add_argument("--overwrite", action="store_true", help="Allow writing into a non-empty run folder")


def make_parser(model_id: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.set_defaults(model_id=model_id)
    add_common_arguments(parser)
    return parser


def _read_split_manifest(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"Split manifest does not exist: {path}")
    assignments: dict[str, str] = {}
    labels: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = {"record_id", "dataset_split", "label"} - fields
        if missing:
            raise ValueError(f"Split manifest is missing columns: {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            record_id = str(row.get("record_id") or "").strip()
            split = str(row.get("dataset_split") or "").strip()
            label = str(row.get("label") or "").strip()
            if not record_id:
                raise ValueError(f"Split manifest has an empty record_id at line {line_number}")
            if record_id in assignments:
                raise ValueError(f"Split manifest repeats record_id {record_id!r}")
            if split not in SPLITS:
                raise ValueError(f"Invalid dataset_split {split!r} for {record_id}")
            assignments[record_id] = split
            labels[record_id] = label
    if not assignments:
        raise ValueError(f"Split manifest is empty: {path}")
    if set(assignments.values()) != set(SPLITS):
        raise ValueError("Split manifest must contain train, validation, and test rows")
    return assignments, labels


def prepare_experiment(args: argparse.Namespace) -> dict[str, Any]:
    """Load only binary Gold articles and create/reuse the common 70/15/15 split."""

    baseline = load_baseline_module()
    rows = baseline.load_training_rows(args.input)
    if not rows:
        raise ValueError("No complete RESPALDADA/REFUTADA article rows were found in the input CSV")

    if args.split_manifest:
        assignments, manifest_labels = _read_split_manifest(args.split_manifest)
        by_id = {row["record_id"]: row for row in rows}
        missing = sorted(set(assignments) - set(by_id))
        if missing:
            raise ValueError(f"The split manifest refers to article IDs missing from training.csv: {missing[:5]}")
        for record_id, label in manifest_labels.items():
            if by_id[record_id]["label"] != label:
                raise ValueError(f"Label mismatch for {record_id} between training.csv and split manifest")
        selected = [by_id[record_id] for record_id in assignments]
        split_source = str(args.split_manifest)
    else:
        selected = baseline.select_rows(rows, args.limit, args.seed)
        try:
            assignments = baseline.stratified_split(selected, args.seed)
        except ValueError as exc:
            raise ValueError(f"Could not create the stratified 70/15/15 split: {exc}") from exc
        split_source = f"generated with seed={args.seed}, limit={args.limit}"

    if len(selected) < args.min_rows:
        raise ValueError(
            f"Only {len(selected)} binary articles are selected; --min-rows is {args.min_rows}. "
            "Complete more human reviews or adjust the pilot size."
        )
    if set(row["label"] for row in selected) != set(baseline.LABEL_TO_ID):
        raise ValueError("The selected articles must contain both RESPALDADA and REFUTADA labels")
    if set(assignments) != {row["record_id"] for row in selected}:
        raise ValueError("Each selected article must appear exactly once in the split manifest")

    by_split = {
        split: [row for row in selected if assignments[row["record_id"]] == split]
        for split in SPLITS
    }
    if any(not rows_for_split for rows_for_split in by_split.values()):
        raise ValueError("The split must contain train, validation, and test articles")
    if set(row["label"] for row in by_split["train"]) != set(baseline.LABEL_TO_ID):
        raise ValueError("The train split must contain examples from both binary classes")

    return {
        "baseline": baseline,
        "selected": selected,
        "assignments": assignments,
        "by_split": by_split,
        "split_source": split_source,
    }


def _safe_run_dir(args: argparse.Namespace, model_id: str) -> Path:
    run_name = args.run_name or (
        f"{model_id}_seed{args.seed}_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_name).strip("._")
    if not safe_name:
        raise ValueError("--run-name must contain at least one letter or number")
    run_dir = args.output_dir / safe_name
    if run_dir.exists() and not run_dir.is_dir():
        raise FileExistsError(f"Run output path is a file: {run_dir}")
    if run_dir.is_dir() and any(run_dir.iterdir()) and not args.overwrite:
        raise FileExistsError(f"Run folder already contains files: {run_dir}; choose another --run-name")
    return run_dir


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_evaluation(
    prepared: dict[str, Any],
    run_dir: Path,
    model_id: str,
    predictor: Callable[[list[str]], tuple[Any, Any | None]],
    model_metadata: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    baseline = prepared["baseline"]
    predictions: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    for split in SPLITS:
        split_rows = prepared["by_split"][split]
        texts = [baseline.build_text(row) for row in split_rows]
        y_true = [baseline.LABEL_TO_ID[row["label"]] for row in split_rows]
        y_pred_raw, y_score_raw = predictor(texts)
        y_pred = [int(value) for value in list(y_pred_raw)]
        y_score = None if y_score_raw is None else [float(value) for value in list(y_score_raw)]
        if len(y_pred) != len(split_rows) or (y_score is not None and len(y_score) != len(split_rows)):
            raise RuntimeError(f"Predictor returned the wrong number of rows for split {split}")
        metrics[split] = baseline._metrics(y_true, y_pred, y_score)
        for row, true_id, predicted_id, score in zip(split_rows, y_true, y_pred, y_score or [None] * len(y_pred)):
            predictions.append(
                {
                    "record_id": row["record_id"],
                    "dataset_split": split,
                    "true_label": baseline.ID_TO_LABEL[true_id],
                    "predicted_label": baseline.ID_TO_LABEL[predicted_id],
                    "correct": str(int(true_id == predicted_id)),
                    "score_respaldada": "" if score is None else round(score, 7),
                }
            )

    manifest_rows = [
        {
            "record_id": row["record_id"],
            "dataset_split": prepared["assignments"][row["record_id"]],
            "label": row["label"],
            "label_id": baseline.LABEL_TO_ID[row["label"]],
            "source_name": row.get("source_name", ""),
            "topic": row.get("topic", ""),
        }
        for row in prepared["selected"]
    ]
    _write_csv(
        run_dir / "split_manifest.csv",
        ["record_id", "dataset_split", "label", "label_id", "source_name", "topic"],
        manifest_rows,
    )
    _write_csv(
        run_dir / "predictions.csv",
        ["record_id", "dataset_split", "true_label", "predicted_label", "correct", "score_respaldada"],
        predictions,
    )
    summary = {
        "status": "ok",
        "model_id": model_id,
        "input_csv": str(args.input),
        "run_dir": str(run_dir),
        "seed": args.seed,
        "rows_available": len(baseline.load_training_rows(args.input)),
        "rows_selected": len(prepared["selected"]),
        "class_counts": dict(Counter(row["label"] for row in prepared["selected"])),
        "split_counts": {split: len(rows) for split, rows in prepared["by_split"].items()},
        "features": ["title", "body"],
        "training_unit": "one article per record_id",
        "target": "label",
        "label_mapping": baseline.LABEL_TO_ID,
        "split": "70/15/15 stratified",
        "split_source": prepared["split_source"],
        "model": model_metadata,
        "metrics": metrics,
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


def run_experiment(
    args: argparse.Namespace,
    model_id: str,
    fit_model: Callable[[dict[str, Any], argparse.Namespace, Path], tuple[Callable, dict[str, Any]]],
) -> int:
    prepared = prepare_experiment(args)
    run_dir = _safe_run_dir(args, model_id)
    run_dir_preexisted = run_dir.exists()
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        predictor, model_metadata = fit_model(prepared, args, run_dir)
    except Exception:
        if not run_dir_preexisted and run_dir.exists() and not any(run_dir.iterdir()):
            run_dir.rmdir()
        raise
    summary = save_evaluation(prepared, run_dir, model_id, predictor, model_metadata, args)
    print(json.dumps({"status": summary["status"], "model_id": model_id, "run_dir": str(run_dir)}))
    return 0


def run_cli(
    model_id: str,
    description: str,
    fit_model: Callable,
    add_model_arguments: Callable[[argparse.ArgumentParser], None] | None = None,
) -> int:
    parser = make_parser(model_id, description)
    if add_model_arguments is not None:
        add_model_arguments(parser)
    args = parser.parse_args()
    try:
        return run_experiment(args, model_id, fit_model)
    except ImportError as exc:
        print(
            "ERROR: A required dependency is missing. Install the selected extras, "
            "for example: python3 -m pip install -e '.[training]'. "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 2
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
