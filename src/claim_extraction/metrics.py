"""Métricas con referencia y diagnósticos operativos sin referencia."""

from __future__ import annotations

import re
from collections import Counter
from statistics import mean
from typing import Any, Callable, Iterable


BERTSCORE_MODEL_TYPE = "xlm-roberta-base"
BERTSCORE_LANGUAGE = "es"
_BERTSCORER: Any | None = None


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


def rouge_l_prf(reference: object, prediction: object) -> tuple[float, float, float]:
    """Devuelve precision, recall y F1 de ROUGE-L para un par de textos."""
    ref_tokens = tokenize(reference)
    pred_tokens = tokenize(prediction)
    if not ref_tokens or not pred_tokens:
        return 0.0, 0.0, 0.0
    common = lcs_length(ref_tokens, pred_tokens)
    precision = common / len(pred_tokens)
    recall = common / len(ref_tokens)
    if not precision + recall:
        return precision, recall, 0.0
    return precision, recall, 2 * precision * recall / (precision + recall)


def rouge_l_f1(reference: object, prediction: object) -> float:
    """Conserva el acceso directo al F1 de ROUGE-L para código existente."""
    return rouge_l_prf(reference, prediction)[2]


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


def _bertscore_device() -> str:
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _get_bertscorer() -> Any:
    """Carga una vez el encoder para no recargarlo por cada modelo evaluado."""
    global _BERTSCORER
    if _BERTSCORER is not None:
        return _BERTSCORER
    try:
        from bert_score import BERTScorer
    except ImportError as exc:
        raise RuntimeError("Instala el extra models para calcular BERTScore F1.") from exc
    _BERTSCORER = BERTScorer(
        lang=BERTSCORE_LANGUAGE,
        model_type=BERTSCORE_MODEL_TYPE,
        batch_size=8,
        device=_bertscore_device(),
        rescale_with_baseline=False,
    )
    return _BERTSCORER


def bertscore_prf(
    references: list[str], predictions: list[str]
) -> list[tuple[float, float, float]]:
    """Calcula precision, recall y F1 de BERTScore en lote para español."""
    if len(references) != len(predictions):
        raise ValueError("BERTScore requiere la misma cantidad de referencias y predicciones.")

    scores = [(0.0, 0.0, 0.0)] * len(predictions)
    nonempty = [
        index
        for index, (reference, prediction) in enumerate(zip(references, predictions, strict=True))
        if tokenize(reference) and tokenize(prediction)
    ]
    if not nonempty:
        return scores

    precision, recall, f1 = _get_bertscorer().score(
        cands=[predictions[index] for index in nonempty],
        refs=[references[index] for index in nonempty],
    )
    for index, score_precision, score_recall, score_f1 in zip(
        nonempty, precision, recall, f1, strict=True
    ):
        scores[index] = (float(score_precision), float(score_recall), float(score_f1))
    return scores


def bertscore_f1(references: list[str], predictions: list[str]) -> list[float]:
    """Conserva el acceso directo al F1 de BERTScore para código existente."""
    return [score_f1 for _, _, score_f1 in bertscore_prf(references, predictions)]


def reference_metrics(
    rows: Iterable[dict[str, object]],
    *,
    bertscore_scorer: Callable[
        [list[str], list[str]], list[tuple[float, float, float]]
    ] = bertscore_prf,
    meteor_scorer: Callable[[object, object], float] = meteor,
) -> tuple[list[dict[str, object]], dict[str, float]]:
    paired_rows = list(rows)
    if not paired_rows:
        raise ValueError("No hay filas emparejadas para evaluar.")

    references = [str(row["reference_claim"]) for row in paired_rows]
    predictions = [str(row["claim"]) for row in paired_rows]
    bertscore_scores = bertscore_scorer(references, predictions)
    if len(bertscore_scores) != len(paired_rows):
        raise ValueError("El evaluador BERTScore devolvió una cantidad inválida de resultados.")

    per_row: list[dict[str, object]] = []
    for row, reference, prediction, bertscore in zip(
        paired_rows, references, predictions, bertscore_scores, strict=True
    ):
        rouge_precision, rouge_recall, rouge_f1 = rouge_l_prf(reference, prediction)
        bertscore_precision, bertscore_recall, bertscore_f1_score = bertscore
        per_row.append(
            {
                **row,
                "meteor": float(meteor_scorer(reference, prediction)),
                "rouge_l_precision": rouge_precision,
                "rouge_l_recall": rouge_recall,
                "rouge_l_f1": rouge_f1,
                "bertscore_precision": float(bertscore_precision),
                "bertscore_recall": float(bertscore_recall),
                "bertscore_f1": float(bertscore_f1_score),
                "prediction_tokens": len(tokenize(prediction)),
            }
        )

    summary = {
        "n": float(len(per_row)),
        "meteor": mean(float(row["meteor"]) for row in per_row),
        "rouge_l_precision": mean(float(row["rouge_l_precision"]) for row in per_row),
        "rouge_l_recall": mean(float(row["rouge_l_recall"]) for row in per_row),
        "rouge_l_f1": mean(float(row["rouge_l_f1"]) for row in per_row),
        "bertscore_precision": mean(float(row["bertscore_precision"]) for row in per_row),
        "bertscore_recall": mean(float(row["bertscore_recall"]) for row in per_row),
        "bertscore_f1": mean(float(row["bertscore_f1"]) for row in per_row),
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
