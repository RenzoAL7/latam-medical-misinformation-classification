#!/usr/bin/env python3
"""Genera claims con un checkpoint local de Transformers, sin APIs externas."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table
from claim_extraction.models import load_seq2seq_model, load_tokenizer
from claim_extraction.registry import get_model
from claim_extraction.text import input_for_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Id definido en configs/models.json.")
    parser.add_argument("--checkpoint", required=True, help="Ruta local del modelo ajustado.")
    parser.add_argument("--input", required=True, help="Candidatos JSONL o CSV.")
    parser.add_argument("--output", required=True, help="Predicciones JSONL.")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def _device() -> torch.device:
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def main() -> None:
    args = parse_args()
    spec = get_model(args.model)
    frame = read_table(Path(args.input).expanduser())
    required = {"record_id", "source_text"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Faltan columnas en candidatos: {', '.join(missing)}")
    if args.limit is not None:
        frame = frame.head(args.limit)
    if frame.empty:
        raise ValueError("No hay candidatos para inferencia.")

    try:
        import transformers  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Faltan dependencias. Ejecuta make setup antes de inferir.") from exc

    cache_kwargs = {"cache_dir": args.cache_dir} if args.cache_dir else {}
    checkpoint = str(Path(args.checkpoint).expanduser())
    tokenizer = load_tokenizer(checkpoint, **cache_kwargs)
    model = load_seq2seq_model(checkpoint, model_id=spec.id, **cache_kwargs)
    device = _device()
    model.to(device)
    model.eval()
    max_source = int(spec.training["max_source_length"])
    generation = {
        key: value
        for key, value in spec.generation.items()
        if key in {
            "num_beams",
            "max_new_tokens",
            "do_sample",
            "temperature",
            "top_p",
            "top_k",
            "no_repeat_ngram_size",
            "length_penalty",
            "early_stopping",
        }
    }
    if not generation.get("do_sample", False):
        generation.pop("temperature", None)
        generation.pop("top_p", None)
        generation.pop("top_k", None)

    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    rows = frame.to_dict(orient="records")
    with output_path.open("w", encoding="utf-8") as handle:
        for start in range(0, len(rows), max(1, args.batch_size)):
            batch = rows[start : start + max(1, args.batch_size)]
            inputs = [input_for_model(row, spec) for row in batch]
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
            for row, input_text, claim in zip(batch, inputs, claims, strict=True):
                handle.write(
                    json.dumps(
                        {
                            "record_id": row["record_id"],
                            "source_hash": row.get("source_hash", ""),
                            "model_id": spec.id,
                            "model_display_name": spec.display_name,
                            "checkpoint": checkpoint,
                            "input_text": input_text,
                            "claim": claim.strip(),
                            "device": str(device),
                            "generated_at": generated_at,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    print(
        json.dumps(
            {
                "model_id": spec.id,
                "rows": len(rows),
                "device": str(device),
                "output": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
