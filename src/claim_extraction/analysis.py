"""Incertidumbre, pruebas pareadas y desgloses de las métricas por noticia del notebook 04."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from .metrics import meteor_spanish_stem


DEFAULT_BOOTSTRAP_SAMPLES = 10_000
DEFAULT_SEED = 20261103
# Fijados en docs/EXPERIMENT_PROTOCOL.md, sección 4.2, antes de crear claims_1500.
DEFAULT_ALPHA = 0.05
PRIMARY_METRIC = "meteor"


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


def _score_matrix(per_prediction: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Devuelve una fila por noticia y una columna por modelo."""
    scores = per_prediction.pivot_table(
        index="record_id", columns="model_id", values=metric, aggfunc="first"
    )
    if scores.isna().any().any():
        raise ValueError("Cada modelo debe tener una métrica para las mismas noticias.")
    return scores


def _display_names(per_prediction: pd.DataFrame, model_ids: Sequence[str]) -> dict[str, str]:
    if "model_display_name" not in per_prediction:
        return {model_id: model_id for model_id in model_ids}
    return per_prediction.drop_duplicates("model_id").set_index("model_id")["model_display_name"].to_dict()


def _scipy_stats() -> Any:
    try:
        from scipy import stats
    except ImportError as exc:
        raise RuntimeError("Falta scipy. Instala: pip install -e '.[analysis,models,dev]'") from exc
    return stats


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
    scores = _score_matrix(per_prediction, metric)
    names = _display_names(per_prediction, scores.columns)
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


def holm_adjust(p_values: Sequence[float]) -> np.ndarray:
    """Ajusta p-valores por comparaciones múltiples con Holm, en el orden recibido."""
    values = np.asarray(p_values, dtype=float)
    order = np.argsort(values, kind="stable")
    adjusted = np.maximum.accumulate((len(values) - np.arange(len(values))) * values[order])
    result = np.empty_like(values)
    result[order] = adjusted.clip(max=1.0)
    return result


def _wilcoxon(differences: np.ndarray) -> tuple[float, float]:
    """Devuelve p bilateral y efecto rango-biserial; los empates no cuentan."""
    nonzero = differences[differences != 0]
    if nonzero.size == 0:
        return 1.0, 0.0
    stats = _scipy_stats()
    ranks = stats.rankdata(np.abs(nonzero))
    positive, negative = ranks[nonzero > 0].sum(), ranks[nonzero < 0].sum()
    p_value = float(stats.wilcoxon(nonzero, zero_method="wilcox", alternative="two-sided").pvalue)
    return p_value, float((positive - negative) / (positive + negative))


def _friedman(scores: pd.DataFrame) -> tuple[float | None, float | None]:
    """Prueba global entre modelos; solo está definida con tres o más."""
    if scores.shape[1] < 3:
        return None, None
    # Si cada noticia recibe el mismo puntaje en todos los modelos, el estadístico es 0/0.
    if scores.round(12).nunique(axis=1).eq(1).all():
        return 0.0, 1.0
    result = _scipy_stats().friedmanchisquare(*(scores[column].to_numpy(dtype=float) for column in scores))
    return float(result.statistic), float(result.pvalue)


def _pair_reading(row: Mapping[str, Any], *, alpha: float, global_difference: bool) -> str:
    """Traduce una comparación a la frase que el protocolo permite escribir."""
    top, other = row["modelo_mayor_media"], row["modelo_comparado"]
    if global_difference and row["p_holm"] < alpha:
        if row["efecto_rango_biserial"] * row["diferencia_media"] > 0:
            return f"{top} supera a {other}"
        return f"{top} y {other}: la media y los rangos discrepan; no se concluye"
    return f"{top} y {other} no se distinguen con este test"


def model_significance_tests(
    per_prediction: pd.DataFrame,
    *,
    metric: str = PRIMARY_METRIC,
    alpha: float = DEFAULT_ALPHA,
    n_boot: int = DEFAULT_BOOTSTRAP_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Compara todos los modelos de forma pareada sobre las mismas noticias.

    Aplica Friedman como prueba global, Wilcoxon a cada par y Holm dentro de la
    métrica. Una comparación solo se lee como diferencia si Friedman la detecta
    primero. Con varias semillas debe recibir ``average_over_seeds``: la unidad
    de análisis sigue siendo la noticia.
    """
    scores = _score_matrix(per_prediction, metric)
    if scores.shape[1] < 2:
        raise ValueError("Se necesitan al menos dos modelos para compararlos.")
    names = _display_names(per_prediction, scores.columns)
    role = "principal" if metric == PRIMARY_METRIC else "complementaria"
    statistic, global_p = _friedman(scores)
    global_difference = global_p is None or global_p < alpha
    resamples = np.random.default_rng(seed).integers(0, len(scores), (n_boot, len(scores)))
    ordered = list(scores.mean().sort_values(ascending=False, kind="stable").index)

    rows = []
    for position, top in enumerate(ordered):
        for other in ordered[position + 1 :]:
            # Redondear evita que dos promedios iguales cuenten como diferencia por error de coma flotante.
            differences = np.round(scores[top].to_numpy(dtype=float) - scores[other].to_numpy(dtype=float), 12)
            low, high = _interval(differences[resamples].mean(axis=1))
            p_value, effect = _wilcoxon(differences)
            rows.append(
                {
                    "metric": metric,
                    "rol": role,
                    "modelo_mayor_media": names[top],
                    "modelo_comparado": names[other],
                    "n": len(differences),
                    "diferencia_media": float(differences.mean()),
                    "ci95_low": low,
                    "ci95_high": high,
                    "p_wilcoxon": p_value,
                    "efecto_rango_biserial": effect,
                    "noticias_gana_mayor_media": int((differences > 0).sum()),
                    "noticias_gana_comparado": int((differences < 0).sum()),
                    "empates": int((differences == 0).sum()),
                }
            )
    table = pd.DataFrame(rows)
    table["p_holm"] = holm_adjust(table["p_wilcoxon"].to_numpy())
    table["lectura"] = [
        _pair_reading(row, alpha=alpha, global_difference=global_difference)
        for row in table.to_dict(orient="records")
    ]
    friedman = {
        "metric": metric,
        "rol": role,
        "n": len(scores),
        "modelos": scores.shape[1],
        "estadistico": statistic,
        "p": global_p,
        "alpha": alpha,
        "hay_diferencia_global": global_difference,
    }
    return friedman, table


def _checked_seed_frames(
    per_prediction_by_seed: Mapping[int, pd.DataFrame],
) -> Mapping[int, pd.DataFrame]:
    """Exige que cada semilla evalúe exactamente los mismos modelos y noticias."""
    if not per_prediction_by_seed:
        raise ValueError("Se necesita al menos una semilla.")
    expected: set[tuple[str, str]] | None = None
    for frame in per_prediction_by_seed.values():
        if frame.duplicated(["model_id", "record_id"]).any():
            raise ValueError("Hay predicciones duplicadas para model_id + record_id.")
        pairs = set(zip(frame["model_id"].astype(str), frame["record_id"].astype(str), strict=True))
        if expected is not None and pairs != expected:
            raise ValueError("Cada semilla debe evaluar los mismos modelos y noticias.")
        expected = pairs
    return per_prediction_by_seed


def average_over_seeds(
    per_prediction_by_seed: Mapping[int, pd.DataFrame],
    *,
    metrics: Sequence[str] = (PRIMARY_METRIC, "rouge_l_f1", "bertscore_f1"),
) -> pd.DataFrame:
    """Promedia entre semillas el puntaje de cada modelo en cada noticia."""
    frames = _checked_seed_frames(per_prediction_by_seed)
    keys = ["record_id", "model_id"]
    if all("model_display_name" in frame for frame in frames.values()):
        keys.append("model_display_name")
    combined = pd.concat([frame[[*keys, *metrics]] for frame in frames.values()], ignore_index=True)
    averaged = combined.groupby(keys, sort=True, as_index=False)[list(metrics)].mean()
    averaged["n_semillas"] = len(frames)
    return averaged


def seed_stability(
    per_prediction_by_seed: Mapping[int, pd.DataFrame],
    *,
    metric: str = PRIMARY_METRIC,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Resume cuánto cambia cada modelo y su posición al repetir el entrenamiento."""
    frames = _checked_seed_frames(per_prediction_by_seed)
    rows = []
    for seed, frame in frames.items():
        means = frame.groupby("model_id")[metric].mean()
        names = _display_names(frame, means.index)
        positions = means.rank(ascending=False, method="min").astype(int)
        for model_id, value in means.items():
            rows.append(
                {
                    "seed": seed,
                    "model_id": model_id,
                    "model_display_name": names[model_id],
                    "metric": metric,
                    "media": float(value),
                    "posicion": int(positions[model_id]),
                }
            )
    per_seed = pd.DataFrame(rows)
    summary = (
        per_seed.groupby(["model_id", "model_display_name", "metric"], as_index=False)
        .agg(
            n_semillas=("seed", "nunique"),
            media=("media", "mean"),
            desviacion=("media", "std"),
            minimo=("media", "min"),
            maximo=("media", "max"),
            veces_primero=("posicion", lambda positions: int(positions.eq(1).sum())),
        )
        .sort_values("media", ascending=False, kind="stable")
        .reset_index(drop=True)
    )
    return per_seed, summary


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
