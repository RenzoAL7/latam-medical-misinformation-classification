"""Métricas con referencia y diagnósticos operativos sin referencia."""

from __future__ import annotations

import re
from collections import Counter
from statistics import mean
from typing import Iterable


def tokenize(text: object) -> list[str]:
    return re.findall(r"\w+", str(text).lower(), flags=re.UNICODE)


def normalize_for_match(text: object) -> str:
    return " ".join(tokenize(text))


def lcs_length(left: list[str], right: list[str]) -> int:
    previous = [0] * (len(right) + 1)
    for token_left in left:
        current = [0]
        for index, token_right in enumerate(right, start=1):
            if token_left == token_right:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def rouge_l_f1(reference: object, prediction: object) -> float:
    ref_tokens = tokenize(reference)
    pred_tokens = tokenize(prediction)
    if not ref_tokens or not pred_tokens:
        return 0.0
    common = lcs_length(ref_tokens, pred_tokens)
    precision = common / len(pred_tokens)
    recall = common / len(ref_tokens)
    if not precision + recall:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def meteor(reference: object, prediction: object) -> float:
    """Calcula METEOR con NLTK; exige wordnet y omw-1.4 instalados."""
    try:
        from nltk.translate.meteor_score import meteor_score
    except ImportError as exc:
        raise RuntimeError("Instala el extra models para calcular METEOR.") from exc

    ref_tokens = tokenize(reference)
    pred_tokens = tokenize(prediction)
    if not ref_tokens or not pred_tokens:
        return 0.0
    try:
        return float(meteor_score([ref_tokens], pred_tokens))
    except LookupError as exc:
        raise RuntimeError(
            "Faltan recursos NLTK. Ejecuta: .venv/bin/python -m nltk.downloader wordnet omw-1.4"
        ) from exc


def reference_metrics(rows: Iterable[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, float]]:
    per_row: list[dict[str, object]] = []
    for row in rows:
        reference = str(row["reference_claim"])
        prediction = str(row["claim"])
        per_row.append(
            {
                **row,
                "exact_match": float(normalize_for_match(reference) == normalize_for_match(prediction)),
                "meteor": meteor(reference, prediction),
                "rouge_l_f1": rouge_l_f1(reference, prediction),
                "prediction_tokens": len(tokenize(prediction)),
            }
        )

    if not per_row:
        raise ValueError("No hay filas emparejadas para evaluar.")

    summary = {
        "n": float(len(per_row)),
        "exact_match": mean(float(row["exact_match"]) for row in per_row),
        "meteor": mean(float(row["meteor"]) for row in per_row),
        "rouge_l_f1": mean(float(row["rouge_l_f1"]) for row in per_row),
        "mean_prediction_tokens": mean(float(row["prediction_tokens"]) for row in per_row),
    }
    return per_row, summary


def operational_diagnostics(rows: Iterable[dict[str, object]]) -> dict[str, float]:
    claims = [str(row.get("claim", "")).strip() for row in rows]
    count = len(claims)
    if not count:
        raise ValueError("No hay predicciones para diagnosticar.")

    normalized = [normalize_for_match(claim) for claim in claims]
    frequencies = Counter(item for item in normalized if item)
    duplicate_outputs = sum(amount - 1 for amount in frequencies.values() if amount > 1)
    token_counts = [len(tokenize(claim)) for claim in claims]

    return {
        "n": float(count),
        "empty_rate": sum(not claim for claim in claims) / count,
        "shorter_than_5_tokens_rate": sum(tokens < 5 for tokens in token_counts) / count,
        "mean_prediction_tokens": mean(token_counts),
        "duplicate_output_rate": duplicate_outputs / count,
    }
