#!/usr/bin/env python3
"""Crea una plantilla de anotación para claims de referencia sin usar APIs."""

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
    parser.add_argument("--output", required=True, help="CSV de anotación local.")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261029)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    frame = read_table(Path(args.input).expanduser())
    needed = {"record_id", "source_text"}
    missing = sorted(needed.difference(frame.columns))
    if missing:
        raise ValueError(f"Faltan columnas en candidatos: {', '.join(missing)}")
    if args.sample_size < 1:
        raise ValueError("--sample-size debe ser mayor que cero.")

    order = frame["record_id"].map(
        lambda value: hashlib.sha256(f"{args.seed}:{value}".encode("utf-8")).hexdigest()
    )
    sample = frame.assign(_order=order).sort_values("_order").head(args.sample_size).copy()
    sample["reference_claim"] = ""
    sample["annotation_status"] = "PENDING"
    sample["annotator_id"] = ""
    sample["annotation_notes"] = ""

    columns = [
        column
        for column in [
            "record_id",
            "country",
            "published_at",
            "source_hash",
            "source_text",
            "claimgen_input",
            "reference_claim",
            "annotation_status",
            "annotator_id",
            "annotation_notes",
        ]
        if column in sample.columns
    ]
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sample.loc[:, columns].to_csv(output_path, index=False)
    print(
        json.dumps(
            {
                "rows": int(len(sample)),
                "seed": args.seed,
                "output": str(output_path),
                "next_step": "Completar reference_claim antes de usar train_seq2seq.py o evaluate_claims.py.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
