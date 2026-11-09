"""Incertidumbre y desgloses de las métricas por noticia del notebook 04."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .metrics import meteor_spanish_stem


DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_SEED = 20261103


def with_spanish_meteor(per_prediction: pd.DataFrame) -> pd.DataFrame:
    """Añade ``meteor_stem_es`` a la tabla de métricas por predicción."""
    work = per_prediction.copy()
    work["meteor_stem_es"] = [
        meteor_spanish_stem(reference, claim)
        for reference, claim in zip(work["reference_claim"], work["claim"], strict=True)
    ]
    return work


def _interval(samples: np.ndarray) -> tuple[float, float]:
    low, high = np.percentile(samples, [2.5, 97.5], axis=0)
    return float(low), float(high)


def bootstrap_uncertainty(
    per_prediction: pd.DataFrame,
    *,
    metric: str = "meteor",
    n_boot: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Calcula IC 95 % por modelo y diferencias pareadas contra el de mayor media.

    Se remuestrean noticias, no predicciones: todos los modelos usan las mismas
    noticias en cada réplica, de modo que la comparación queda pareada.
    """
    scores = per_prediction.pivot_table(
        index="record_id", columns="model_id", values=metric, aggfunc="first"
    )
    if scores.isna().any().any():
        raise ValueError("Cada modelo debe tener una métrica para las mismas noticias.")
    names = (
        per_prediction.drop_duplicates("model_id").set_index("model_id")["model_display_name"].to_dict()
        if "model_display_name" in per_prediction
        else {model_id: model_id for model_id in scores.columns}
    )
    resamples = np.random.default_rng(seed).integers(0, len(scores), (n_boot, len(scores)))
    means = scores.mean().sort_values(ascending=False)
    top_model = str(means.index[0])

    summary_rows = []
    difference_rows = []
    for model_id in means.index:
        values = scores[model_id].to_numpy(dtype=float)
        low, high = _interval(values[resamples].mean(axis=1))
        summary_rows.append(
            {
                "metric": metric,
                "model_id": model_id,
                "model_display_name": names[model_id],
                "n": len(values),
                "mean": float(values.mean()),
                "ci95_low": low,
                "ci95_high": high,
            }
        )
        if model_id == top_model:
            continue
        difference = scores[top_model].to_numpy(dtype=float) - values
        low, high = _interval(difference[resamples].mean(axis=1))
        difference_rows.append(
            {
                "metric": metric,
                "model_mayor_media": names[top_model],
                "modelo_comparado": names[model_id],
                "n": len(values),
                "diferencia_media": float(difference.mean()),
                "ci95_low": low,
                "ci95_high": high,
                "ic_excluye_cero": bool(low > 0 or high < 0),
            }
        )
    return pd.DataFrame(summary_rows), pd.DataFrame(difference_rows)


def metrics_by_topic(
    per_prediction: pd.DataFrame,
    references: pd.DataFrame,
    *,
    n_boot: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> pd.DataFrame:
    """Resume las métricas por modelo y por el ``tema`` anotado de cada noticia."""
    if "tema" not in references:
        raise ValueError("Las referencias no contienen la columna tema.")
    joined = per_prediction.merge(
        references[["record_id", "tema"]], on="record_id", how="left", validate="many_to_one"
    )
    if joined["tema"].isna().any() or joined["tema"].eq("").any():
        raise ValueError("Hay predicciones cuya noticia no tiene tema.")
    rng = np.random.default_rng(seed)
    rows = []
    for (model_id, tema), group in joined.groupby(["model_id", "tema"], sort=True):
        values = group["meteor"].to_numpy(dtype=float)
        resamples = rng.integers(0, len(values), (n_boot, len(values)))
        low, high = _interval(values[resamples].mean(axis=1))
        rows.append(
            {
                "model_id": model_id,
                "model_display_name": group["model_display_name"].iloc[0]
                if "model_display_name" in group
                else model_id,
                "tema": tema,
                "n": len(values),
                "meteor": float(values.mean()),
                "meteor_ci95_low": low,
                "meteor_ci95_high": high,
                "rouge_l_f1": float(group["rouge_l_f1"].astype(float).mean()),
                "bertscore_f1": float(group["bertscore_f1"].astype(float).mean()),
            }
        )
    return pd.DataFrame(rows)
