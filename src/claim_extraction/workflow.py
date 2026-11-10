"""Funciones locales reutilizables para los notebooks de Sem1.

El repositorio no ofrece comandos CLI para el flujo operativo. Los notebooks
usan estas funciones para mantener validaciones y comportamiento consistentes
sin ocultar la secuencia metodológica fuera de ellos.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from .data import read_table, validate_reference_table
from .metrics import BERTSCORE_MODEL_TYPE, bertscore_prf, meteor, operational_diagnostics, reference_metrics
from .models import configure_model, load_seq2seq_model, load_tokenizer
from .registry import get_model
from .text import input_for_model


# Conjuntos cerrados admitidos: cada notebook elige uno y valida sus tamaños.
DATASET_SPLIT_SIZES: dict[str, dict[str, int]] = {
    "claims_500": {"train": 400, "validation": 50, "test": 50},
    "claims_1500": {"train": 1200, "validation": 150, "test": 150},
}
_UNIQUE_COLUMNS = ("record_id", "source_hash", "claim_hash")


# Variantes del experimento. Cada una tiene su propia carpeta de corrida.
# Épocas: 5 (paper 99), 10 (paper 100) y 20 (paper 80, T5). LoRA: r=8 (papers 75 y 80),
# alpha=32 y dropout=0.1 (paper 75), con 10 épocas (paper 75), aplicado a T5 en el paper 100.
EXPERIMENT_VARIANTS: dict[str, dict[str, Any]] = {
    "epocas_5": {"epochs": 5, "lora": None},
    "epocas_10": {"epochs": 10, "lora": None},
    "epocas_20": {"epochs": 20, "lora": None},
    "lora": {
        "epochs": 10,
        # Coincide con el paper 97; en BARTO, 3e-5 apenas aprendía en validación y 3e-4 sí.
        "learning_rate": 3e-4,
        "lora": {"r": 8, "alpha": 32, "dropout": 0.1},
    },
}
# Capas de atención con nombres T5/uMT5 (q, k, v, o) y BART/mBART (*_proj).
LORA_TARGET_MODULES = ["q", "k", "v", "o", "q_proj", "k_proj", "v_proj", "out_proj"]
# Carpetas ya usadas con claims_500; epocas_5 reutiliza claims_500_v1 porque con
# paciencia 3 su mejor checkpoint no cambia. Los conjuntos nuevos no reutilizan nada.
_CLAIMS_500_RUN_NAMES = {
    "epocas_5": "claims_500_v1",
    "epocas_10": "claims_500_v2_epocas_10",
    "epocas_20": "claims_500_v2_epocas_20",
    "lora": "claims_500_v2_lora",
}
# La primera semilla es la base. El número de repeticiones se fija en
# docs/EXPERIMENT_PROTOCOL.md, sección 4.2, antes de generar predicciones de test.
BASE_SEED = 20261103
EXPERIMENT_SEEDS = (BASE_SEED, 20261104, 20261105, 20261106, 20261107)


def run_name_for(dataset_name: str, variant: str, seed: int = BASE_SEED) -> str:
    """Nombra la corrida por conjunto, variante y semilla para que no se mezclen.

    La semilla base no añade sufijo, así las corridas ya hechas conservan su carpeta.
    """
    if dataset_name not in DATASET_SPLIT_SIZES:
        raise ValueError(f"Conjunto desconocido: {dataset_name}.")
    if variant not in EXPERIMENT_VARIANTS:
        raise ValueError(f"Variante desconocida: {variant}.")
    name = (
        _CLAIMS_500_RUN_NAMES[variant]
        if dataset_name == "claims_500"
        else f"{dataset_name}_{variant}"
    )
    return name if seed == BASE_SEED else f"{name}_seed{seed}"


def split_fingerprints(splits: Mapping[str, pd.DataFrame]) -> dict[str, str]:
    """Resume qué noticias forman cada split, sin guardar su contenido."""
    return {
        name: hashlib.sha256("\n".join(sorted(frame["record_id"])).encode("utf-8")).hexdigest()[:16]
        for name, frame in splits.items()
    }


def assert_run_matches(
    manifest: Mapping[str, Any],
    *,
    dataset_name: str,
    variant: str,
    fingerprints: Mapping[str, str],
    seed: int | None = None,
) -> None:
    """Impide reanudar o evaluar una corrida hecha con otros datos, variante o semilla.

    Las corridas antiguas no registran variante ni huellas; para ellas solo se
    puede comprobar el conjunto y la semilla.
    """
    expected: dict[str, Any] = {
        "dataset_name": dataset_name,
        "variant": variant,
        "split_fingerprints": dict(fingerprints),
    }
    if seed is not None:
        expected["seed"] = seed
    for key, value in expected.items():
        if key in manifest and manifest[key] != value:
            raise ValueError(
                f"La corrida no corresponde a este experimento: {key} es "
                f"{manifest[key]!r} y se esperaba {value!r}."
            )


def load_per_prediction_by_seed(
    runs_dir: str | Path,
    *,
    dataset_name: str,
    selected_variants: Mapping[str, str],
    seeds: Sequence[int],
    fingerprints: Mapping[str, str],
) -> dict[int, pd.DataFrame]:
    """Reúne por semilla las métricas de la variante elegida para cada modelo.

    Cada modelo puede venir de una variante distinta, porque se eligen por
    validación. Si falta alguna corrida evaluada, las lista todas juntas.
    """
    root = Path(runs_dir).expanduser()
    missing: list[str] = []
    collected: dict[int, list[pd.DataFrame]] = {seed: [] for seed in seeds}
    for seed in seeds:
        for model_id, variant in selected_variants.items():
            run_dir = root / run_name_for(dataset_name, variant, seed)
            path = run_dir / "evaluation" / model_id / "per_prediction.csv"
            manifest_path = run_dir / "run_manifest.json"
            if not path.is_file() or not manifest_path.is_file():
                missing.append(f"{run_dir.name}/{model_id}")
                continue
            assert_run_matches(
                json.loads(manifest_path.read_text(encoding="utf-8")),
                dataset_name=dataset_name,
                variant=variant,
                fingerprints=fingerprints,
                seed=seed,
            )
            collected[seed].append(
                pd.read_csv(path, dtype={"record_id": str}, keep_default_na=False, encoding="utf-8")
            )
    if missing:
        raise FileNotFoundError("Faltan corridas evaluadas: " + ", ".join(missing))
    return {seed: pd.concat(frames, ignore_index=True) for seed, frames in collected.items()}


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
    expected_sizes: Mapping[str, int],
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


def _model_inputs(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    work["input_text"] = [
        input_for_model(row) for row in work.to_dict(orient="records")
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
    gradient_checkpointing: bool = False,
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
    if gradient_checkpointing and "gradient_checkpointing" in signature.parameters:
        values["gradient_checkpointing"] = True
    return training_arguments_class(**values)


def _make_training_progress_callback(
    callback_base: type[Any],
    *,
    reporter: Callable[[int, int, int], None] | None,
    every_percent: int,
) -> Any | None:
    """Crea un callback ligero para mostrar hitos de entrenamiento en notebooks."""
    if not 1 <= every_percent <= 100:
        raise ValueError("progress_every_percent debe estar entre 1 y 100.")
    if reporter is None:
        return None

    class TrainingProgressCallback(callback_base):
        def __init__(self) -> None:
            self.last_reported_percent = -1

        def _report(self, state: Any) -> None:
            if not getattr(state, "is_local_process_zero", True):
                return
            total_steps = int(getattr(state, "max_steps", 0) or 0)
            if total_steps < 1:
                return
            current_steps = max(0, int(getattr(state, "global_step", 0) or 0))
            percent = min(100, int(100 * current_steps / total_steps))
            if percent == self.last_reported_percent:
                return
            crossed_interval = percent // every_percent > self.last_reported_percent // every_percent
            if percent == 100 or self.last_reported_percent < 0 or crossed_interval:
                reporter(percent, current_steps, total_steps)
                self.last_reported_percent = percent

        def on_train_begin(self, args: Any, state: Any, control: Any, **kwargs: Any) -> Any:
            self._report(state)
            return control

        def on_step_end(self, args: Any, state: Any, control: Any, **kwargs: Any) -> Any:
            self._report(state)
            return control

        def on_train_end(self, args: Any, state: Any, control: Any, **kwargs: Any) -> Any:
            self._report(state)
            return control

    return TrainingProgressCallback()


def _apply_lora(model: Any, lora: Mapping[str, Any], gradient_checkpointing: bool) -> Any:
    try:
        from peft import LoraConfig, TaskType, get_peft_model
    except ImportError as exc:
        raise RuntimeError("Falta peft. Instala: pip install -e '.[analysis,models,dev]'") from exc
    if gradient_checkpointing:
        model.enable_input_require_grads()
    return get_peft_model(
        model,
        LoraConfig(
            task_type=TaskType.SEQ_2_SEQ_LM,
            r=int(lora["r"]),
            lora_alpha=int(lora["alpha"]),
            lora_dropout=float(lora["dropout"]),
            target_modules=LORA_TARGET_MODULES,
            bias="none",
        ),
    )


def train_seq2seq_model(
    *,
    model_id: str,
    train: pd.DataFrame,
    validation: pd.DataFrame,
    output_dir: str | Path,
    seed: int = BASE_SEED,
    batch_size: int = 1,
    cache_dir: str | Path | None = None,
    base_checkpoint: str | Path | None = None,
    epochs: float | None = None,
    learning_rate: float | None = None,
    progress_reporter: Callable[[int, int, int], None] | None = None,
    progress_every_percent: int = 5,
    gradient_checkpointing: bool = False,
    early_stopping_patience: int | None = None,
    lora: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ajusta un modelo y guarda su mejor checkpoint bajo ``output_dir``."""
    if batch_size < 1:
        raise ValueError("batch_size debe ser positivo.")
    if not 1 <= progress_every_percent <= 100:
        raise ValueError("progress_every_percent debe estar entre 1 y 100.")
    spec = get_model(model_id)
    train = validate_reference_table(train)
    validation = validate_reference_table(validation)
    train_data = _model_inputs(train)
    validation_data = _model_inputs(validation)
    if train_data.empty or validation_data.empty:
        raise ValueError("Train y validation deben contener al menos una fila válida.")

    try:
        import torch
        from datasets import Dataset
        from transformers import (
            DataCollatorForSeq2Seq,
            EarlyStoppingCallback,
            Seq2SeqTrainer,
            Seq2SeqTrainingArguments,
            TrainerCallback,
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
    if gradient_checkpointing:
        if not hasattr(model, "gradient_checkpointing_enable"):
            raise RuntimeError("El checkpoint no admite gradient checkpointing.")
        model.gradient_checkpointing_enable()
        if hasattr(model.config, "use_cache"):
            model.config.use_cache = False
    if lora:
        model = _apply_lora(model, lora, gradient_checkpointing)
    parameters_total = sum(parameter.numel() for parameter in model.parameters())
    parameters_trainable = sum(
        parameter.numel() for parameter in model.parameters() if parameter.requires_grad
    )
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
        gradient_checkpointing=gradient_checkpointing,
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
    progress_callback = _make_training_progress_callback(
        TrainerCallback,
        reporter=progress_reporter,
        every_percent=progress_every_percent,
    )
    callbacks: list[Any] = [] if progress_callback is None else [progress_callback]
    if early_stopping_patience is not None:
        callbacks.append(EarlyStoppingCallback(early_stopping_patience=early_stopping_patience))
    if callbacks:
        trainer_kwargs["callbacks"] = callbacks
    trainer = Seq2SeqTrainer(**trainer_kwargs)
    trainer.train()
    best_dir = model_dir / "best"
    if lora:
        # Se fusiona el adaptador para que la inferencia cargue un checkpoint normal.
        trainer.model.merge_and_unload().save_pretrained(best_dir)
    else:
        trainer.save_model(best_dir)
    tokenizer.save_pretrained(best_dir)
    evaluations = [row for row in trainer.state.log_history if "eval_loss" in row]
    best_evaluation = min(evaluations, key=lambda row: row["eval_loss"])
    requested_epochs = arguments.num_train_epochs
    epochs_run = round(evaluations[-1]["epoch"])

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
        "gradient_checkpointing": gradient_checkpointing,
        "learning_rate": arguments.learning_rate,
        "epochs_requested": requested_epochs,
        "epochs_run": epochs_run,
        "early_stopping_patience": early_stopping_patience,
        "stopped_early": epochs_run < requested_epochs,
        "best_epoch": round(best_evaluation["epoch"]),
        "best_eval_loss": best_evaluation["eval_loss"],
        "lora": dict(lora) if lora else None,
        "parameters_total": parameters_total,
        "parameters_trainable": parameters_trainable,
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
        inputs = [input_for_model(record) for record in batch]
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
