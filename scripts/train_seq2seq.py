#!/usr/bin/env python3
"""Ajusta localmente un modelo seq2seq con pares noticia -> claim anotados."""

from __future__ import annotations

import argparse
import inspect
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table, validate_reference_table
from claim_extraction.models import load_seq2seq_model, load_tokenizer
from claim_extraction.registry import get_model
from claim_extraction.text import input_for_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Id definido en configs/models.json.")
    parser.add_argument("--train", required=True, help="CSV/JSONL con source_text y reference_claim.")
    parser.add_argument("--validation", required=True, help="CSV/JSONL de validación.")
    parser.add_argument("--output-dir", required=True, help="Directorio del checkpoint local.")
    parser.add_argument("--cache-dir", default=None, help="Cache opcional de Hugging Face.")
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--epochs", type=float, default=None)
    parser.add_argument("--learning-rate", type=float, default=None)
    parser.add_argument("--batch-size", type=int, default=1)
    return parser.parse_args()


def _model_inputs(frame: pd.DataFrame, spec) -> pd.DataFrame:
    work = frame.copy()
    work["input_text"] = [
        input_for_model(row, spec) for row in work.to_dict(orient="records")
    ]
    work["target_text"] = work["reference_claim"].map(str)
    return work[["input_text", "target_text"]]


def _training_arguments(TrainingArguments, *, spec, output_dir: Path, args, use_cpu: bool):
    training = spec.training
    effective_batch = int(training["effective_batch_size"])
    accumulation = max(1, effective_batch // max(1, args.batch_size))
    values = {
        "output_dir": str(output_dir),
        "num_train_epochs": args.epochs or float(training["epochs"]),
        "learning_rate": args.learning_rate or float(training["learning_rate"]),
        "weight_decay": float(training.get("weight_decay", 0.0)),
        "warmup_steps": int(training.get("warmup_steps", 0)),
        "per_device_train_batch_size": args.batch_size,
        "per_device_eval_batch_size": args.batch_size,
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
        "seed": args.seed,
        "data_seed": args.seed,
        "dataloader_num_workers": 0,
        "remove_unused_columns": True,
    }
    signature = inspect.signature(TrainingArguments)
    if "eval_strategy" in signature.parameters:
        values["eval_strategy"] = "epoch"
    else:
        values["evaluation_strategy"] = "epoch"
    if "use_cpu" in signature.parameters:
        values["use_cpu"] = use_cpu
    elif use_cpu and "no_cuda" in signature.parameters:
        values["no_cuda"] = True
    return TrainingArguments(**values)


def main() -> None:
    args = parse_args()
    spec = get_model(args.model)
    train = validate_reference_table(read_table(Path(args.train).expanduser()))
    validation = validate_reference_table(read_table(Path(args.validation).expanduser()))
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
            "Faltan dependencias. Ejecuta make setup antes de entrenar."
        ) from exc

    set_seed(args.seed)
    cache_kwargs = {"cache_dir": args.cache_dir} if args.cache_dir else {}
    tokenizer = load_tokenizer(spec.base_checkpoint, **cache_kwargs)
    model = load_seq2seq_model(
        spec.base_checkpoint, model_id=spec.id, **cache_kwargs
    )
    max_source = int(spec.training["max_source_length"])
    max_target = int(spec.training["max_target_length"])

    def tokenize(batch):
        return tokenizer(
            batch["input_text"],
            text_target=batch["target_text"],
            max_length=max_source,
            max_target_length=max_target,
            truncation=True,
        )

    train_dataset = Dataset.from_pandas(train_data, preserve_index=False).map(
        tokenize, batched=True, remove_columns=["input_text", "target_text"]
    )
    validation_dataset = Dataset.from_pandas(validation_data, preserve_index=False).map(
        tokenize, batched=True, remove_columns=["input_text", "target_text"]
    )

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    has_accelerator = bool(torch.cuda.is_available()) or bool(
        getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
    )
    arguments = _training_arguments(
        Seq2SeqTrainingArguments,
        spec=spec,
        output_dir=output_dir,
        args=args,
        use_cpu=not has_accelerator,
    )
    trainer_kwargs = {
        "model": model,
        "args": arguments,
        "train_dataset": train_dataset,
        "eval_dataset": validation_dataset,
        "data_collator": DataCollatorForSeq2Seq(tokenizer=tokenizer, model=model),
    }
    trainer_signature = inspect.signature(Seq2SeqTrainer)
    if "processing_class" in trainer_signature.parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer
    trainer = Seq2SeqTrainer(**trainer_kwargs)
    trainer.train()
    trainer.save_model(output_dir / "best")
    tokenizer.save_pretrained(output_dir / "best")

    manifest = {
        "model_id": spec.id,
        "display_name": spec.display_name,
        "base_checkpoint": spec.base_checkpoint,
        "paper": spec.paper,
        "paper_url": spec.paper_url,
        "train_rows": int(len(train_data)),
        "validation_rows": int(len(validation_data)),
        "seed": args.seed,
        "device": "cuda" if torch.cuda.is_available() else (
            "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
        ),
        "training": spec.training,
        "generation": spec.generation,
    }
    (output_dir / "training_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
