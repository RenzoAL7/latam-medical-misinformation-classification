#!/usr/bin/env python3
"""Ejecuta una corrida Sem1 completa: fine-tuning, inferencia y evaluación.

Cada modelo usa exactamente los mismos CSV congelados. Los pesos, predicciones,
logs y métricas quedan bajo un único directorio de corrida ignorado por Git.
"""

from __future__ import annotations

import argparse
import json
import platform
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table, validate_reference_table
from claim_extraction.registry import get_model, load_registry


EXPECTED_SPLITS = {"train": 320, "validation": 40, "test": 40}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ejecuta la comparación local de cuatro modelos de Sem1."
    )
    parser.add_argument("--train", required=True)
    parser.add_argument("--validation", required=True)
    parser.add_argument("--test", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(load_registry()),
        help="IDs de modelos del registro. Por defecto ejecuta los cuatro.",
    )
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Verifica los tres CSV y termina sin crear una corrida ni cargar pesos.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Omite modelos cuyo entrenamiento y evaluación ya terminaron.",
    )
    return parser.parse_args()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _run(command: list[str], log_path: Path) -> None:
    print("$", shlex.join(command), flush=True)
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            handle.write(line)
            print(line, end="", flush=True)
        returncode = process.wait()
    if returncode:
        raise RuntimeError(
            f"Falló el comando ({returncode}). Revisa el log: {log_path}"
        )


def _validate_split(path: Path, split: str) -> pd.DataFrame:
    frame = validate_reference_table(read_table(path))
    expected_rows = EXPECTED_SPLITS[split]
    if len(frame) != expected_rows:
        raise ValueError(f"{split} debe tener {expected_rows} filas, no {len(frame)}.")
    if frame["record_id"].duplicated().any():
        raise ValueError(f"{split} tiene record_id duplicados.")
    if frame["source_hash"].duplicated().any():
        raise ValueError(f"{split} tiene fuentes duplicadas.")
    if frame["reference_claim"].duplicated().any():
        raise ValueError(f"{split} tiene claims de referencia duplicados.")
    return frame


def _validate_inputs(train_path: Path, validation_path: Path, test_path: Path) -> dict[str, int]:
    frames = {
        "train": _validate_split(train_path, "train"),
        "validation": _validate_split(validation_path, "validation"),
        "test": _validate_split(test_path, "test"),
    }
    for column in ("record_id", "source_hash", "claim_hash"):
        values = [set(frame[column]) for frame in frames.values()]
        if values[0].intersection(values[1]) or values[0].intersection(values[2]) or values[1].intersection(values[2]):
            raise ValueError(f"Hay fuga entre splits en la columna {column}.")
    return {name: len(frame) for name, frame in frames.items()}


def _read_model_summary(path: Path, model_id: str) -> dict[str, object]:
    frame = pd.read_csv(path)
    if len(frame) != 1 or frame.loc[0, "model_id"] != model_id:
        raise ValueError(f"El resumen de {model_id} no tiene la estructura esperada.")
    row = frame.iloc[0].to_dict()
    if int(row["prediction_n"]) != EXPECTED_SPLITS["test"] or int(row["reference_n"]) != EXPECTED_SPLITS["test"]:
        raise ValueError(f"La evaluación de {model_id} no cubre los 40 casos test.")
    if any(pd.isna(row[column]) for column in ("meteor", "rouge_l_f1", "exact_match")):
        raise ValueError(f"La evaluación de {model_id} no produjo las tres métricas de referencia.")
    return row


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("batch-size debe ser positivo.")
    registry = load_registry()
    unknown = sorted(set(args.models).difference(registry))
    if unknown:
        raise ValueError(f"Modelos desconocidos: {', '.join(unknown)}")
    if len(set(args.models)) != len(args.models):
        raise ValueError("Cada modelo debe aparecer una sola vez.")

    train_path = Path(args.train).expanduser().resolve()
    validation_path = Path(args.validation).expanduser().resolve()
    test_path = Path(args.test).expanduser().resolve()
    for path in (train_path, validation_path, test_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    input_rows = _validate_inputs(train_path, validation_path, test_path)

    if args.validate_only:
        print(json.dumps({"status": "validated", "rows": input_rows}, ensure_ascii=False, indent=2))
        return

    run_dir = Path(args.run_dir).expanduser().resolve()
    if run_dir.exists() and any(run_dir.iterdir()) and not args.resume:
        raise FileExistsError(
            f"La corrida ya existe: {run_dir}. Usa --resume o un directorio nuevo."
        )
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "models").mkdir(exist_ok=True)
    (run_dir / "predictions").mkdir(exist_ok=True)
    (run_dir / "evaluation").mkdir(exist_ok=True)

    status_path = run_dir / "run_status.json"
    status: dict[str, object] = {
        "run_id": run_dir.name,
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "device": _device(),
        "platform": platform.platform(),
        "python": sys.version,
        "inputs": {
            "train": str(train_path),
            "validation": str(validation_path),
            "test": str(test_path),
            "rows": input_rows,
        },
        "models": {},
    }
    _write_json(status_path, status)

    try:
        for model_id in args.models:
            spec = get_model(model_id)
            model_dir = run_dir / "models" / model_id
            checkpoint = model_dir / "best"
            prediction_path = run_dir / "predictions" / f"{model_id}.jsonl"
            evaluation_dir = run_dir / "evaluation" / model_id
            summary_path = evaluation_dir / "summary.csv"
            logs_dir = run_dir / "logs"

            reuse_checkpoint = args.resume and checkpoint.is_dir()
            if reuse_checkpoint and summary_path.is_file():
                status["models"][model_id] = {
                    "status": "skipped_completed",
                    "summary": _read_model_summary(summary_path, model_id),
                }
                _write_json(status_path, status)
                continue
            if model_dir.exists() and any(model_dir.iterdir()) and not reuse_checkpoint:
                raise FileExistsError(
                    f"{model_dir} contiene una corrida incompleta. Usa un run-dir nuevo para reintentar sin mezclar artefactos."
                )

            if not reuse_checkpoint:
                status["models"][model_id] = {"status": "training", "display_name": spec.display_name}
                _write_json(status_path, status)
                train_command = [
                    sys.executable,
                    "scripts/train_seq2seq.py",
                    "--model",
                    model_id,
                    "--train",
                    str(train_path),
                    "--validation",
                    str(validation_path),
                    "--output-dir",
                    str(model_dir),
                    "--seed",
                    str(args.seed),
                    "--batch-size",
                    str(args.batch_size),
                ]
                if args.cache_dir:
                    train_command.extend(["--cache-dir", args.cache_dir])
                _run(train_command, logs_dir / f"{model_id}_train.log")

            status["models"][model_id] = {"status": "inferring", "display_name": spec.display_name}
            _write_json(status_path, status)
            infer_command = [
                sys.executable,
                "scripts/run_inference.py",
                "--model",
                model_id,
                "--checkpoint",
                str(checkpoint),
                "--input",
                str(test_path),
                "--output",
                str(prediction_path),
                "--batch-size",
                str(args.batch_size),
            ]
            if args.cache_dir:
                infer_command.extend(["--cache-dir", args.cache_dir])
            _run(infer_command, logs_dir / f"{model_id}_infer.log")

            status["models"][model_id] = {"status": "evaluating", "display_name": spec.display_name}
            _write_json(status_path, status)
            _run(
                [
                    sys.executable,
                    "scripts/evaluate_claims.py",
                    "--predictions",
                    str(prediction_path),
                    "--references",
                    str(test_path),
                    "--output-dir",
                    str(evaluation_dir),
                ],
                logs_dir / f"{model_id}_evaluate.log",
            )
            status["models"][model_id] = {
                "status": "completed",
                "display_name": spec.display_name,
                "checkpoint": str(checkpoint),
                "predictions": str(prediction_path),
                "summary": _read_model_summary(summary_path, model_id),
            }
            _write_json(status_path, status)

        summaries = [
            pd.read_csv(run_dir / "evaluation" / model_id / "summary.csv")
            for model_id in args.models
        ]
        combined = pd.concat(summaries, ignore_index=True)
        combined.to_csv(run_dir / "evaluation" / "summary.csv", index=False)
        status["status"] = "completed"
        status["finished_at"] = _now()
        _write_json(status_path, status)
        print(json.dumps({"run_dir": str(run_dir), "models": args.models}, ensure_ascii=False, indent=2))
    except Exception as exc:
        status["status"] = "failed"
        status["finished_at"] = _now()
        status["error"] = str(exc)
        _write_json(status_path, status)
        raise


if __name__ == "__main__":
    main()
