"""Frozen multilingual SBERT embeddings followed by a train-only classifier."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from experiments.models.common import run_cli


DEFAULT_CHECKPOINT = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-name", default=DEFAULT_CHECKPOINT, help="Sentence-Transformers model ID or local path")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda", "mps"), default="auto")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-iter", type=int, default=1_000)


def _fit_sbert(prepared: dict[str, Any], args: argparse.Namespace, run_dir: Path):
    try:
        import joblib
        import torch
        from sentence_transformers import SentenceTransformer
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:
        raise RuntimeError(
            "Missing SBERT/Transformers or training dependencies. Install with: "
            "python3 -m pip install -e '.[training,transformers]'"
        ) from exc
    if args.batch_size < 1 or args.max_iter < 1:
        raise ValueError("batch size and max iterations must be positive")

    if args.device == "auto":
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    else:
        device = args.device
    try:
        encoder = SentenceTransformer(args.model_name, device=device)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Could not load SBERT checkpoint {args.model_name!r}. Check the model ID, local cache, "
            "and Hugging Face connectivity."
        ) from exc

    baseline = prepared["baseline"]
    train_rows = prepared["by_split"]["train"]
    train_texts = [baseline.build_text(row) for row in train_rows]
    train_labels = [baseline.LABEL_TO_ID[row["label"]] for row in train_rows]

    def encode(texts: list[str]):
        if not texts:
            return []
        return encoder.encode(
            texts,
            batch_size=args.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

    train_embeddings = encode(train_texts)
    classifier = LogisticRegression(
        class_weight="balanced",
        max_iter=args.max_iter,
        random_state=args.seed,
        solver="liblinear",
    )
    classifier.fit(train_embeddings, train_labels)
    joblib.dump(classifier, run_dir / "classifier.joblib")

    def predict(texts: list[str]):
        embeddings = encode(texts)
        predictions = classifier.predict(embeddings)
        scores = classifier.predict_proba(embeddings)[:, 1]
        return predictions, scores

    return predict, {
        "type": "Frozen multilingual SBERT embeddings + LogisticRegression",
        "checkpoint": args.model_name,
        "classifier": "LogisticRegression(class_weight='balanced', solver='liblinear')",
        "embedding_normalization": True,
        "batch_size": args.batch_size,
        "embedding_dimension": int(train_embeddings.shape[1]),
        "device": device,
    }


def main() -> int:
    return run_cli(
        "sbert",
        "Encode title + body with frozen multilingual SBERT and train LogisticRegression on train embeddings.",
        _fit_sbert,
        _add_arguments,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
