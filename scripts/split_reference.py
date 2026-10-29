#!/usr/bin/env python3
"""Divide referencias anotadas de forma determinista y sin duplicar record_id."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table, validate_reference_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="CSV/JSONL anotado.")
    parser.add_argument("--train-output", required=True)
    parser.add_argument("--validation-output", required=True)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20261029)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0 < args.validation_fraction < 1:
        raise ValueError("--validation-fraction debe estar entre 0 y 1.")
    frame = validate_reference_table(read_table(Path(args.input).expanduser()))
    if len(frame) < 2:
        raise ValueError("Se necesitan al menos dos referencias anotadas.")
    order = frame["record_id"].map(
        lambda value: hashlib.sha256(f"{args.seed}:{value}".encode("utf-8")).hexdigest()
    )
    ordered = frame.assign(_order=order).sort_values("_order").drop(columns="_order")
    validation_n = max(1, round(len(ordered) * args.validation_fraction))
    validation = ordered.head(validation_n)
    train = ordered.iloc[validation_n:]
    if train.empty:
        raise ValueError("La partición dejó train vacío; anota más filas.")

    train_path = Path(args.train_output).expanduser()
    validation_path = Path(args.validation_output).expanduser()
    train_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    train.to_csv(train_path, index=False)
    validation.to_csv(validation_path, index=False)
    print(
        json.dumps(
            {
                "input_rows": len(frame),
                "train_rows": len(train),
                "validation_rows": len(validation),
                "validation_fraction": args.validation_fraction,
                "seed": args.seed,
                "train_output": str(train_path),
                "validation_output": str(validation_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

