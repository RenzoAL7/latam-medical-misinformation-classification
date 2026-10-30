#!/usr/bin/env python3
"""Evalúa predicciones contra claims de referencia y reporta diagnósticos operativos."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import read_table, validate_reference_table
from claim_extraction.metrics import operational_diagnostics, reference_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", required=True, help="Predicciones JSONL o CSV.")
    parser.add_argument("--references", required=True, help="CSV/JSONL con reference_claim.")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    predictions = read_table(Path(args.predictions).expanduser())
    references = validate_reference_table(
        read_table(Path(args.references).expanduser()),
        allow_missing_reference_claims=True,
    )
    required = {"record_id", "model_id", "claim"}
    missing = sorted(required.difference(predictions.columns))
    if missing:
        raise ValueError(f"Faltan columnas en predicciones: {', '.join(missing)}")
    if predictions.duplicated(["model_id", "record_id"]).any():
        raise ValueError("Hay predicciones duplicadas para model_id + record_id.")

    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_cols = ["record_id", "reference_claim"]
    joined = predictions.merge(
        references[reference_cols],
        on="record_id",
        how="left",
        validate="many_to_one",
    )
    summary_rows: list[dict[str, object]] = []
    detail_rows: list[dict[str, object]] = []
    for model_id, group in joined.groupby("model_id", sort=True):
        group_rows = group.to_dict(orient="records")
        operational = operational_diagnostics(group_rows)
        ref_rows = [
            row for row in group_rows if str(row.get("reference_claim", "")).strip()
        ]
        summary: dict[str, object] = {
            "model_id": model_id,
            "model_display_name": group["model_display_name"].iloc[0]
            if "model_display_name" in group
            else model_id,
            "prediction_n": int(len(group_rows)),
            "reference_n": int(len(ref_rows)),
            "empty_rate": operational["empty_rate"],
            "shorter_than_5_tokens_rate": operational["shorter_than_5_tokens_rate"],
            "mean_prediction_tokens_operational": operational["mean_prediction_tokens"],
            "duplicate_output_rate": operational["duplicate_output_rate"],
            "exact_match": None,
            "meteor": None,
            "rouge_l_f1": None,
            "mean_prediction_tokens_reference": None,
        }
        if ref_rows:
            metrics_rows, reference_summary = reference_metrics(ref_rows)
            detail_rows.extend(metrics_rows)
            summary.update(
                {
                    "exact_match": reference_summary["exact_match"],
                    "meteor": reference_summary["meteor"],
                    "rouge_l_f1": reference_summary["rouge_l_f1"],
                    "mean_prediction_tokens_reference": reference_summary[
                        "mean_prediction_tokens"
                    ],
                }
            )
        summary_rows.append(summary)

    pd.DataFrame(summary_rows).to_csv(output_dir / "summary.csv", index=False)
    pd.DataFrame(detail_rows).to_csv(output_dir / "per_prediction.csv", index=False)
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                "predictions": str(Path(args.predictions).expanduser()),
                "references": str(Path(args.references).expanduser()),
                "models": summary_rows,
                "note": "Las métricas de referencia solo se calculan donde existe reference_claim anotado.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"models": len(summary_rows), "output_dir": str(output_dir)},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
