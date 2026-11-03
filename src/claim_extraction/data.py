"""Lectura y validación de tablas locales no versionadas."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .text import clean_text


REQUIRED_REFERENCE_COLUMNS = {"record_id", "source_text", "reference_claim"}


def read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


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
