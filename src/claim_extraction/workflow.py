"""Funciones locales reutilizables para los notebooks de Sem1.

El repositorio no ofrece comandos CLI para el flujo operativo. Los notebooks
usan estas funciones para mantener validaciones y comportamiento consistentes
sin ocultar la secuencia metodológica fuera de ellos.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from .data import read_table, validate_reference_table
from .metrics import BERTSCORE_MODEL_TYPE, bertscore_prf, meteor, operational_diagnostics, reference_metrics
from .models import configure_model, load_seq2seq_model, load_tokenizer
from .registry import get_model
from .text import input_for_model


EXPECTED_SPLIT_SIZES = {"train": 320, "validation": 40, "test": 40}
_UNIQUE_COLUMNS = ("record_id", "source_hash", "claim_hash")


def available_device() -> str:
    """Devuelve el dispositivo preferido sin requerir Torch al importar el módulo."""
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def validate_sem1_splits(
    paths: Mapping[str, str | Path],
    *,
    expected_sizes: Mapping[str, int] = EXPECTED_SPLIT_SIZES,
) -> dict[str, pd.DataFrame]:
    """Lee y valida los tres CSV congelados antes de ajustar un modelo."""
    expected_names = set(expected_sizes)
    if set(paths) != expected_names:
        raise ValueError(
            "Se esperan exactamente los splits: " + ", ".join(sorted(expected_names))
        )

    frames: dict[str, pd.DataFrame] = {}
    for split, expected_rows in expected_sizes.items():
        path = Path(paths[split]).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        frame = validate_reference_table(read_table(path))
        if len(frame) != expected_rows:
            raise ValueError(
                f"{split} debe tener {expected_rows} filas, no {len(frame)}."
            )
        missing = [column for column in _UNIQUE_COLUMNS if column not in frame]
        if missing:
            raise ValueError(
                f"{split} no contiene columnas de control: {', '.join(missing)}."
            )
        for column in _UNIQUE_COLUMNS:
            if frame[column].duplicated().any():
                raise ValueError(f"{split} tiene {column} duplicados.")
        frames[split] = frame

    for column in _UNIQUE_COLUMNS:
        for left, right in (("train", "validation"), ("train", "test"), ("validation", "test")):
            if set(frames[left][column]).intersection(frames[right][column]):
                raise ValueError(f"Hay fuga entre {left} y {right} en {column}.")
    return frames


def _model_inputs(frame: pd.DataFrame, spec: Any) -> pd.DataFrame:
    work = frame.copy()
    work["input_text"] = [
        input_for_model(row, spec) for row in work.to_dict(orient="records")
    ]
    work["target_text"] = work["reference_claim"].map(str)
    return work[["input_text", "target_text"]]


def _training_arguments(
    training_arguments_class: Any,
    *,
    spec: Any,
    output_dir: Path,
    seed: int,
    batch_size: int,
    epochs: float | None,
    learning_rate: float | None,
    use_cpu: bool,
) -> Any:
    training = spec.training
    effective_batch = int(training["effective_batch_size"])
    accumulation = max(1, effective_batch // max(1, batch_size))
    values: dict[str, Any] = {
        "output_dir": str(output_dir),
        "num_train_epochs": epochs or float(training["epochs"]),
        "learning_rate": learning_rate or float(training["learning_rate"]),
        "weight_decay": float(training.get("weight_decay", 0.0)),
        "warmup_steps": int(training.get("warmup_steps", 0)),
        "per_device_train_batch_size": batch_size,
        "per_device_eval_batch_size": batch_size,
        "gradient_accumulation_steps": accumulation,
        "save_strategy": "epoch",
        "logging_strategy": "steps",
        "logging_steps": 10,
        "save_total_limit": 2,
        "load_best_model_at_end": True,
        "metric_for_best_model": "eval_loss",
        "greater_is_better": False,
        "predict_with_generate": True,
        "generation_max_length": int(training["max_target_length"]),
        "report_to": [],
        "seed": seed,
        "data_seed": seed,
        "dataloader_num_workers": 0,
        "remove_unused_columns": True,
    }
    signature = inspect.signature(training_arguments_class)
    if "eval_strategy" in signature.parameters:
        values["eval_strategy"] = "epoch"
    else:
        values["evaluation_strategy"] = "epoch"
    if "use_cpu" in signature.parameters:
        values["use_cpu"] = use_cpu
    elif use_cpu and "no_cuda" in signature.parameters:
        values["no_cuda"] = True
    return training_arguments_class(**values)


def train_seq2seq_model(
    *,
    model_id: str,
    train: pd.DataFrame,
    validation: pd.DataFrame,
    output_dir: str | Path,
    seed: int = 20261103,
    batch_size: int = 1,
    cache_dir: str | Path | None = None,
    base_checkpoint: str | Path | None = None,
    epochs: float | None = None,
    learning_rate: float | None = None,
) -> dict[str, Any]:
    """Ajusta un modelo y guarda su mejor checkpoint bajo ``output_dir``."""
    if batch_size < 1:
        raise ValueError("batch_size debe ser positivo.")
    spec = get_model(model_id)
    train = validate_reference_table(train)
    validation = validate_reference_table(validation)
    train_data = _model_inputs(train, spec)
    validation_data = _model_inputs(validation, spec)
    if train_data.empty or validation_data.empty:
        raise ValueError("Train y validation deben contener al menos una fila válida.")

    try:
        import torch
        from datasets import Dataset
        from transformers import (
            DataCollatorForSeq2Seq,
            Seq2SeqTrainer,
            Seq2SeqTrainingArguments,
            set_seed,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Faltan dependencias de modelos. Instala: pip install -e '.[analysis,models,dev]'"
        ) from exc

    set_seed(seed)
    cache_kwargs = {"cache_dir": str(cache_dir)} if cache_dir else {}
    checkpoint = str(base_checkpoint or spec.base_checkpoint)
    tokenizer = load_tokenizer(checkpoint, model_id=spec.id, **cache_kwargs)
    model = load_seq2seq_model(checkpoint, model_id=spec.id, **cache_kwargs)
    configure_model(model, tokenizer, model_id=spec.id)
    max_source = int(spec.training["max_source_length"])
    max_target = int(spec.training["max_target_length"])

    def tokenize(batch: dict[str, list[str]]) -> dict[str, Any]:
        model_inputs = tokenizer(batch["input_text"], max_length=max_source, truncation=True)
        labels = tokenizer(text_target=batch["target_text"], max_length=max_target, truncation=True)
        model_inputs["labels"] = labels["input_ids"]
        return model_inputs

    train_dataset = Dataset.from_pandas(train_data, preserve_index=False).map(
        tokenize, batched=True, remove_columns=["input_text", "target_text"]
    )
    validation_dataset = Dataset.from_pandas(validation_data, preserve_index=False).map(
        tokenize, batched=True, remove_columns=["input_text", "target_text"]
    )

    model_dir = Path(output_dir).expanduser()
    model_dir.mkdir(parents=True, exist_ok=True)
    has_accelerator = available_device() != "cpu"
    arguments = _training_arguments(
        Seq2SeqTrainingArguments,
        spec=spec,
        output_dir=model_dir,
        seed=seed,
        batch_size=batch_size,
        epochs=epochs,
        learning_rate=learning_rate,
        use_cpu=not has_accelerator,
    )
    trainer_kwargs: dict[str, Any] = {
        "model": model,
        "args": arguments,
        "train_dataset": train_dataset,
        "eval_dataset": validation_dataset,
        "data_collator": DataCollatorForSeq2Seq(tokenizer=tokenizer, model=model),
    }
    if "processing_class" in inspect.signature(Seq2SeqTrainer).parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer
    trainer = Seq2SeqTrainer(**trainer_kwargs)
    trainer.train()
    best_dir = model_dir / "best"
    trainer.save_model(best_dir)
    tokenizer.save_pretrained(best_dir)

    manifest = {
        "model_id": spec.id,
        "display_name": spec.display_name,
        "base_checkpoint": checkpoint,
        "paper": spec.paper,
        "paper_url": spec.paper_url,
        "train_rows": int(len(train_data)),
        "validation_rows": int(len(validation_data)),
        "seed": seed,
        "device": available_device(),
        "training": spec.training,
        "generation": spec.generation,
    }
    (model_dir / "training_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _generation_kwargs(spec: Any) -> dict[str, Any]:
    accepted = {
        "num_beams", "max_new_tokens", "do_sample", "temperature", "top_p",
        "top_k", "no_repeat_ngram_size", "length_penalty", "early_stopping",
    }
    generation = {
        key: value for key, value in spec.generation.items()
        if key in accepted and value is not None
    }
    if not generation.get("do_sample", False):
        for key in ("temperature", "top_p", "top_k"):
            generation.pop(key, None)
    return generation


def generate_predictions(
    *,
    model_id: str,
    checkpoint: str | Path,
    candidates: pd.DataFrame,
    output_path: str | Path,
    batch_size: int = 1,
    cache_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Genera un CSV de claims locales para un conjunto de candidatos."""
    if batch_size < 1:
        raise ValueError("batch_size debe ser positivo.")
    if "record_id" not in candidates:
        raise ValueError("Los candidatos deben contener record_id.")
    if candidates.empty:
        raise ValueError("No hay candidatos para inferencia.")
    spec = get_model(model_id)
    checkpoint = Path(checkpoint).expanduser()
    if not checkpoint.is_dir():
        raise FileNotFoundError(checkpoint)

    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "Falta Torch. Instala: pip install -e '.[analysis,models,dev]'"
        ) from exc

    cache_kwargs = {"cache_dir": str(cache_dir)} if cache_dir else {}
    tokenizer = load_tokenizer(str(checkpoint), model_id=spec.id, **cache_kwargs)
    model = load_seq2seq_model(str(checkpoint), model_id=spec.id, **cache_kwargs)
    configure_model(model, tokenizer, model_id=spec.id)
    device_name = available_device()
    device = torch.device(device_name)
    model.to(device)
    model.eval()
    max_source = int(spec.training["max_source_length"])
    generation = _generation_kwargs(spec)
    records = candidates.to_dict(orient="records")
    predictions: list[dict[str, str]] = []
    for start in range(0, len(records), batch_size):
        batch = records[start : start + batch_size]
        inputs = [input_for_model(record, spec) for record in batch]
        encoded = tokenizer(
            inputs,
            max_length=max_source,
            truncation=True,
            padding=True,
            return_tensors="pt",
        ).to(device)
        with torch.inference_mode():
            output_ids = model.generate(**encoded, **generation)
        claims = tokenizer.batch_decode(output_ids, skip_special_tokens=True)
        for record, claim in zip(batch, claims, strict=True):
            predictions.append(
                {
                    "record_id": str(record["record_id"]),
                    "source_hash": str(record.get("source_hash", "")),
                    "model_id": spec.id,
                    "model_display_name": spec.display_name,
                    "checkpoint": str(checkpoint),
                    "claim": claim.strip(),
                    "device": device_name,
                }
            )
    result = pd.DataFrame(predictions)
    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(target, index=False, encoding="utf-8")
    return result


def evaluate_predictions(
    predictions: pd.DataFrame,
    references: pd.DataFrame,
    *,
    bertscore_scorer: Callable[[list[str], list[str]], list[tuple[float, float, float]]] = bertscore_prf,
    meteor_scorer: Callable[[object, object], float] = meteor,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Une predicciones con referencias y calcula métricas por modelo."""
    references = validate_reference_table(references)
    required = {"record_id", "model_id", "claim"}
    missing = sorted(required.difference(predictions.columns))
    if missing:
        raise ValueError(f"Faltan columnas en predicciones: {', '.join(missing)}")
    if predictions.duplicated(["model_id", "record_id"]).any():
        raise ValueError("Hay predicciones duplicadas para model_id + record_id.")

    joined = predictions.merge(
        references[["record_id", "reference_claim"]],
        on="record_id",
        how="left",
        validate="many_to_one",
    )
    summary_rows: list[dict[str, object]] = []
    detail_rows: list[dict[str, object]] = []
    for model_id, group in joined.groupby("model_id", sort=True):
        records = group.to_dict(orient="records")
        operational = operational_diagnostics(records)
        reference_rows = [
            row for row in records if str(row.get("reference_claim", "")).strip()
        ]
        summary: dict[str, object] = {
            "model_id": model_id,
            "model_display_name": str(group["model_display_name"].iloc[0])
            if "model_display_name" in group else model_id,
            "prediction_n": int(len(records)),
            "reference_n": int(len(reference_rows)),
            "empty_rate": operational["empty_rate"],
            "shorter_than_5_tokens_rate": operational["shorter_than_5_tokens_rate"],
            "mean_prediction_tokens_operational": operational["mean_prediction_tokens"],
            "duplicate_output_rate": operational["duplicate_output_rate"],
            "meteor": None,
            "rouge_l_precision": None,
            "rouge_l_recall": None,
            "rouge_l_f1": None,
            "bertscore_precision": None,
            "bertscore_recall": None,
            "bertscore_f1": None,
            "bertscore_model": BERTSCORE_MODEL_TYPE,
            "mean_prediction_tokens_reference": None,
        }
        if reference_rows:
            metrics_rows, metric_summary = reference_metrics(
                reference_rows,
                bertscore_scorer=bertscore_scorer,
                meteor_scorer=meteor_scorer,
            )
            detail_rows.extend(metrics_rows)
            summary.update(
                {
                    "meteor": metric_summary["meteor"],
                    "rouge_l_precision": metric_summary["rouge_l_precision"],
                    "rouge_l_recall": metric_summary["rouge_l_recall"],
                    "rouge_l_f1": metric_summary["rouge_l_f1"],
                    "bertscore_precision": metric_summary["bertscore_precision"],
                    "bertscore_recall": metric_summary["bertscore_recall"],
                    "bertscore_f1": metric_summary["bertscore_f1"],
                    "mean_prediction_tokens_reference": metric_summary["mean_prediction_tokens"],
                }
            )
        summary_rows.append(summary)
    return pd.DataFrame(summary_rows), pd.DataFrame(detail_rows)


def utc_now() -> str:
    """Marca temporal de procedencia para manifiestos locales, no para nombres."""
    return datetime.now(timezone.utc).isoformat()
