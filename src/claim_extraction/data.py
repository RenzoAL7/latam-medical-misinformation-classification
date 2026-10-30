"""Lectura, filtrado y serialización de corpus locales no versionados."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from .text import build_claimgen_transfer_input, build_source_text, clean_text


REQUIRED_NEWS_COLUMNS = {"record_id", "title", "body"}
REQUIRED_REFERENCE_COLUMNS = {"record_id", "source_text", "reference_claim"}


def read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return pd.DataFrame(rows)
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def write_jsonl(rows: Iterable[dict], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _require_columns(frame: pd.DataFrame, expected: set[str], label: str) -> None:
    missing = sorted(expected.difference(frame.columns))
    if missing:
        raise ValueError(f"{label} no contiene las columnas requeridas: {', '.join(missing)}")


def prepare_news_candidates(
    frame: pd.DataFrame,
    *,
    min_body_chars: int = 400,
    sample_size: int | None = None,
    seed: int = 20261029,
) -> pd.DataFrame:
    _require_columns(frame, REQUIRED_NEWS_COLUMNS, "El CSV de noticias")
    work = frame.copy()

    if "body_available" in work:
        work = work[work["body_available"].astype(str).str.upper().eq("SI")]
    if "extraction_status" in work:
        work = work[work["extraction_status"].astype(str).str.upper().eq("OK")]

    work["title"] = work["title"].map(clean_text)
    work["body"] = work["body"].map(clean_text)
    work = work[(work["title"] != "") & (work["body"].str.len() >= min_body_chars)]
    work["source_text"] = [
        build_source_text(title, body)
        for title, body in zip(work["title"], work["body"], strict=True)
    ]
    work["claimgen_input"] = [
        build_claimgen_transfer_input(title, body)
        for title, body in zip(work["title"], work["body"], strict=True)
    ]
    work["source_hash"] = work["source_text"].map(
        lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    )
    work["source_char_count"] = work["source_text"].str.len()

    columns = [
        column
        for column in [
            "record_id",
            "country",
            "published_at",
            "source_name",
            "topic",
            "title",
            "body",
            "source_text",
            "claimgen_input",
            "source_hash",
            "source_char_count",
        ]
        if column in work.columns
    ]
    work = work.loc[:, columns].drop_duplicates(subset=["record_id"]).copy()

    if sample_size is not None:
        if sample_size < 1:
            raise ValueError("sample_size debe ser mayor que cero.")
        order = work["record_id"].map(
            lambda value: hashlib.sha256(f"{seed}:{value}".encode("utf-8")).hexdigest()
        )
        work = work.assign(_order=order).sort_values("_order").head(sample_size).drop(columns="_order")

    return work.reset_index(drop=True)


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
