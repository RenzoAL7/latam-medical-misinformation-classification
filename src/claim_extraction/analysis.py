"""Desglose por tema de las métricas por noticia del notebook 04."""

from __future__ import annotations

import pandas as pd


def metrics_by_topic(per_prediction: pd.DataFrame, references: pd.DataFrame) -> pd.DataFrame:
    """Resume las métricas por modelo y por el ``tema`` anotado de cada noticia."""
    if "tema" not in references:
        raise ValueError("Las referencias no contienen la columna tema.")
    joined = per_prediction.merge(
        references[["record_id", "tema"]], on="record_id", how="left", validate="many_to_one"
    )
    if joined["tema"].isna().any() or joined["tema"].eq("").any():
        raise ValueError("Hay predicciones cuya noticia no tiene tema.")
    rows = []
    for (model_id, tema), group in joined.groupby(["model_id", "tema"], sort=True):
        rows.append(
            {
                "model_id": model_id,
                "model_display_name": group["model_display_name"].iloc[0]
                if "model_display_name" in group
                else model_id,
                "tema": tema,
                "n": len(group),
                "meteor": float(group["meteor"].astype(float).mean()),
                "rouge_l_f1": float(group["rouge_l_f1"].astype(float).mean()),
                "bertscore_f1": float(group["bertscore_f1"].astype(float).mean()),
            }
        )
    return pd.DataFrame(rows)
