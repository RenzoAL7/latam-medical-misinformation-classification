#!/usr/bin/env python3
"""Escribe los splits fijos 240/30/30 del Gold de Seminario 1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table, validate_reference_table


EXPECTED = {"train": 240, "validation": 30, "test": 30}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Gold con la columna split.")
    parser.add_argument("--train-output", required=True)
    parser.add_argument("--validation-output", required=True)
    parser.add_argument("--test-output", required=True)
    parser.add_argument("--expected-train", type=int, default=EXPECTED["train"])
    parser.add_argument("--expected-validation", type=int, default=EXPECTED["validation"])
    parser.add_argument("--expected-test", type=int, default=EXPECTED["test"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    frame = validate_reference_table(read_table(Path(args.input).expanduser()))
    if "split" not in frame:
        raise ValueError("El Gold no contiene la columna split.")
    expected = {
        "train": args.expected_train,
        "validation": args.expected_validation,
        "test": args.expected_test,
    }
    observed = frame["split"].value_counts().to_dict()
    invalid = sorted(set(observed).difference(expected))
    if invalid:
        raise ValueError("El Gold contiene splits inválidos: " + ", ".join(invalid))
    mismatched = {
        split: {"expected": total, "observed": int(observed.get(split, 0))}
        for split, total in expected.items()
        if int(observed.get(split, 0)) != total
    }
    if mismatched:
        raise ValueError(
            "Los tamaños del Gold no coinciden con el diseño 240/30/30: "
            + json.dumps(mismatched, ensure_ascii=False)
        )
    if "source_hash" in frame:
        split_per_source = frame.groupby("source_hash")["split"].nunique()
        leaked_sources = split_per_source[split_per_source.gt(1)]
        if not leaked_sources.empty:
            raise ValueError(
                "Hay textos fuente repetidos en más de un split: "
                + ", ".join(leaked_sources.index.astype(str).tolist()[:10])
            )

    output_paths = {
        "train": Path(args.train_output).expanduser(),
        "validation": Path(args.validation_output).expanduser(),
        "test": Path(args.test_output).expanduser(),
    }
    for split, output in output_paths.items():
        output.parent.mkdir(parents=True, exist_ok=True)
        frame.loc[frame["split"].eq(split)].to_csv(output, index=False, encoding="utf-8-sig")

    print(
        json.dumps(
            {
                "input_rows": len(frame),
                "split_counts": {split: int(observed.get(split, 0)) for split in expected},
                "outputs": {split: str(path) for split, path in output_paths.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
