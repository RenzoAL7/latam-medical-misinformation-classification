#!/usr/bin/env python3
"""Construye una hoja ciega opcional para revisar calidad de claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Candidatos JSONL o CSV.")
    parser.add_argument("--predictions", required=True, help="Predicciones JSONL o CSV.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261029)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates = read_table(Path(args.input).expanduser())
    predictions = read_table(Path(args.predictions).expanduser())
    for frame, name, required in [
        (candidates, "candidatos", {"record_id", "source_text"}),
        (predictions, "predicciones", {"record_id", "model_id", "claim"}),
    ]:
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError(f"Faltan columnas en {name}: {', '.join(missing)}")
    if args.sample_size < 1:
        raise ValueError("--sample-size debe ser mayor que cero.")
    if predictions.duplicated(["model_id", "record_id"]).any():
        raise ValueError("Hay predicciones duplicadas para model_id + record_id.")

    model_ids = sorted(predictions["model_id"].astype(str).unique())
    counts = predictions.groupby("record_id")["model_id"].nunique()
    complete_ids = set(counts[counts == len(model_ids)].index)
    candidates = candidates[candidates["record_id"].isin(complete_ids)].copy()
    if candidates.empty:
        raise ValueError("No existe una muestra con predicciones para todos los modelos.")
    order = candidates["record_id"].map(
        lambda value: hashlib.sha256(f"{args.seed}:{value}".encode("utf-8")).hexdigest()
    )
    sample = candidates.assign(_order=order).sort_values("_order").head(args.sample_size)
    selected_ids = set(sample["record_id"])
    pred = predictions[predictions["record_id"].isin(selected_ids)].copy()
    model_codes = {
        model_id: f"M{index:02d}" for index, model_id in enumerate(model_ids, start=1)
    }
    pred["model_code"] = pred["model_id"].map(model_codes)
    pred = pred.merge(
        sample[["record_id", "country", "published_at", "source_hash", "source_text"]],
        on="record_id",
        how="left",
        validate="many_to_one",
    )
    pred = pred.sample(frac=1.0, random_state=args.seed).reset_index(drop=True)
    pred.insert(0, "blind_id", [f"B{index:04d}" for index in range(1, len(pred) + 1)])
    output = pred[
        [
            "blind_id",
            "record_id",
            "country",
            "published_at",
            "source_hash",
            "source_text",
            "model_code",
            "claim",
        ]
    ].copy()
    for column in [
        "atomicity",
        "fluency",
        "decontextualization",
        "faithfulness",
        "reviewer_notes",
    ]:
        output[column] = ""

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_dir / "review_sheet.csv", index=False)
    (output_dir / "model_key.json").write_text(
        json.dumps(model_codes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "sample_size_requested": args.sample_size,
                "news_rows": len(sample),
                "review_rows": len(output),
                "models": model_ids,
                "seed": args.seed,
                "note": "Conservar model_key.json separado de la hoja entregada al revisor.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "news_rows": len(sample),
                "review_rows": len(output),
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
