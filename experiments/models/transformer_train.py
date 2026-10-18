"""Fine-tune a Hugging Face sequence classifier on title + article body."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.models.common import run_cli


def _add_transformer_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-name", default=None, help="Hugging Face checkpoint ID or local directory")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--patience", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)


def _fit_transformer(
    model_id: str,
    default_checkpoint: str,
    prepared: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
):
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "Missing PyTorch/Transformers. Install with: "
            "python3 -m pip install -e '.[training,transformers]'"
        ) from exc

    if args.batch_size < 1 or args.epochs < 1 or args.patience < 1 or args.max_length < 8:
        raise ValueError("batch size, epochs, patience must be positive and max length at least 8")
    if args.learning_rate <= 0:
        raise ValueError("learning rate must be positive")

    from experiments.models.torch_utils import predict_loader, resolve_device, set_seed, train_classifier

    set_seed(torch, args.seed)
    device = resolve_device(torch, args.device)
    checkpoint_name = args.model_name or default_checkpoint
    baseline = prepared["baseline"]
    try:
        tokenizer = AutoTokenizer.from_pretrained(checkpoint_name)
        model = AutoModelForSequenceClassification.from_pretrained(
            checkpoint_name,
            num_labels=2,
            id2label={0: "REFUTADA", 1: "RESPALDADA"},
            label2id={"REFUTADA": 0, "RESPALDADA": 1},
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not load checkpoint {checkpoint_name!r}. Check the model ID, local cache, "
            "and Hugging Face connectivity; no checkpoint was downloaded by setup."
        ) from exc

    class EncodedArticles(torch.utils.data.Dataset):
        def __init__(self, texts: list[str], labels: list[int]):
            self.encoded = tokenizer(
                texts,
                truncation=True,
                max_length=args.max_length,
                padding="max_length",
                return_tensors="pt",
            )
            self.labels = torch.tensor(labels, dtype=torch.long)

        def __len__(self) -> int:
            return len(self.labels)

        def __getitem__(self, index: int):
            item = {key: value[index] for key, value in self.encoded.items()}
            item["labels"] = self.labels[index]
            return item

    labels_by_split = {
        split: [baseline.LABEL_TO_ID[row["label"]] for row in rows]
        for split, rows in prepared["by_split"].items()
    }
    texts_by_split = {
        split: [baseline.build_text(row) for row in rows]
        for split, rows in prepared["by_split"].items()
    }
    generator = torch.Generator().manual_seed(args.seed)
    train_loader = torch.utils.data.DataLoader(
        EncodedArticles(texts_by_split["train"], labels_by_split["train"]),
        batch_size=args.batch_size,
        shuffle=True,
        generator=generator,
    )
    validation_loader = torch.utils.data.DataLoader(
        EncodedArticles(texts_by_split["validation"], labels_by_split["validation"]),
        batch_size=args.batch_size,
        shuffle=False,
    )
    model.to(device)

    def forward_batch(current_model: Any, batch: dict[str, Any], current_device: Any):
        current_batch = {key: value.to(current_device) for key, value in batch.items()}
        targets = current_batch.pop("labels")
        return current_model(**current_batch).logits, targets

    history = train_classifier(
        torch,
        model,
        train_loader,
        validation_loader,
        labels_by_split["train"],
        device,
        args.learning_rate,
        args.epochs,
        args.patience,
        forward_batch,
    )

    def predict(texts: list[str]):
        loader = torch.utils.data.DataLoader(
            EncodedArticles(texts, [0] * len(texts)),
            batch_size=args.batch_size,
            shuffle=False,
        )
        return predict_loader(torch, model, loader, device, forward_batch)

    checkpoint_dir = run_dir / "checkpoint"
    model.save_pretrained(checkpoint_dir)
    tokenizer.save_pretrained(checkpoint_dir)
    (run_dir / "training_history.json").write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    return predict, {
        "type": "Hugging Face AutoModelForSequenceClassification fine-tuned",
        "checkpoint": checkpoint_name,
        "max_length": args.max_length,
        "batch_size": args.batch_size,
        "learning_rate": args.learning_rate,
        "device": str(device),
        "best_epoch": int(min(history, key=lambda item: item["validation_loss"])["epoch"]),
        "epochs_trained": len(history),
    }


def main(model_id: str, default_checkpoint: str) -> int:
    description = f"Fine-tune {model_id} for binary medical-news classification using title + body."

    def fit(prepared: dict[str, Any], args: argparse.Namespace, run_dir: Path):
        return _fit_transformer(model_id, default_checkpoint, prepared, args, run_dir)

    return run_cli(model_id, description, fit, _add_transformer_arguments)
