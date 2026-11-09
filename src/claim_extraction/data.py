"""Lectura y validación de tablas locales no versionadas."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from .text import clean_text


REQUIRED_REFERENCE_COLUMNS = {"record_id", "source_text", "reference_claim"}


def read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _shingles(text: str, size: int) -> set[str]:
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if len(words) < size:
        return {" ".join(words)} if words else set()
    return {" ".join(words[index : index + size]) for index in range(len(words) - size + 1)}


def _similar(left: set[str], right: set[str], threshold: float) -> bool:
    # Si los tamaños ya impiden alcanzar el umbral, se evita calcular la intersección.
    if not left or not right or min(len(left), len(right)) < threshold * max(len(left), len(right)):
        return False
    common = len(left & right)
    return common / (len(left) + len(right) - common) >= threshold


def near_duplicate_groups(
    sources: Sequence[str],
    claims: Sequence[str],
    *,
    source_threshold: float = 0.5,
    claim_threshold: float = 0.5,
) -> list[int]:
    """Devuelve un grupo por fila; las casi-duplicadas comparten grupo.

    Dos noticias se enlazan si sus textos comparten trigramas de palabras o sus
    claims comparten bigramas (Jaccard mayor o igual al umbral). Captura notas
    de agencia reproducidas con cambios menores, que el hash exacto no detecta.
    """
    if len(sources) != len(claims):
        raise ValueError("sources y claims deben tener la misma longitud.")
    source_sets = [_shingles(text, 3) for text in sources]
    claim_sets = [_shingles(text, 2) for text in claims]
    parents = list(range(len(sources)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for left in range(len(sources)):
        for right in range(left + 1, len(sources)):
            if _similar(source_sets[left], source_sets[right], source_threshold) or _similar(
                claim_sets[left], claim_sets[right], claim_threshold
            ):
                root_left, root_right = find(left), find(right)
                parents[max(root_left, root_right)] = min(root_left, root_right)
    return [find(index) for index in range(len(sources))]


def _require_columns(frame: pd.DataFrame, expected: set[str], label: str) -> None:
    missing = sorted(expected.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} no contiene las columnas requeridas: {', '.join(missing)}")


def validate_reference_table(
    frame: pd.DataFrame,
    *,
    allow_missing_reference_claims: bool = False,
) -> pd.DataFrame:
    """Valida una tabla de referencias.

    El entrenamiento y la partición requieren ``reference_claim`` para todas
    las filas. La evaluación también puede consumir una plantilla todavía
    pendiente: en ese caso conserva los diagnósticos operativos y deja las
    métricas de referencia sin calcular para esas filas.
    """
    _require_columns(frame, REQUIRED_REFERENCE_COLUMNS, "El conjunto de referencia")
    work = frame.copy()
    work["source_text"] = work["source_text"].map(clean_text)
    work["reference_claim"] = work["reference_claim"].map(clean_text)
    invalid = work[work["source_text"] == ""]
    if not invalid.empty:
        raise ValueError(
            "El conjunto de referencia contiene source_text vacío."
        )
    if not allow_missing_reference_claims:
        missing_claims = work[work["reference_claim"] == ""]
        if not missing_claims.empty:
            raise ValueError(
                "El conjunto de referencia contiene reference_claim vacío. "
                "No es válido entrenar, dividir ni calcular METEOR/ROUGE para esas filas."
            )
    if work["record_id"].duplicated().any():
        raise ValueError("El conjunto de referencia contiene record_id duplicados.")
    return work
