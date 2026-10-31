#!/usr/bin/env python3
"""Construye y congela el manifiesto 320/40/40 desde un export de anotaciones.

Las 700 tareas son un pool de cribado. Este script toma únicamente las tareas
con un claim central apto, excluye textos duplicados y selecciona 400 con una
semilla reproducible. El CSV resultante es la lista cerrada que entra al Gold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


APTA = {"APTA_CON_CLAIM", "APTA_CON_CLAIM_LEGACY"}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _tasks_from_export(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return [item for item in payload["tasks"] if isinstance(item, dict)]
    raise ValueError("El export debe ser un JSON de tareas de anotación.")


def _latest_completed_annotation(task: dict[str, Any]) -> dict[str, Any] | None:
    completed = [
        annotation
        for annotation in task.get("annotations", [])
        if not annotation.get("was_cancelled") and annotation.get("result")
    ]
    return max(completed, key=lambda annotation: str(annotation.get("created_at") or "")) if completed else None


def _result_choice(results: list[dict[str, Any]], names: tuple[str, ...]) -> str:
    # The current field takes precedence over the retired field even when an
    # older result remains in the same annotation after a configuration edit.
    for name in names:
        choices = [
            (result.get("value") or {}).get("choices")
            for result in results
            if result.get("from_name") == name
        ]
        for choice in reversed(choices):
            if isinstance(choice, list) and choice:
                return _clean(choice[0])
    return ""


def _result_text(results: list[dict[str, Any]], name: str) -> str:
    for result in results:
        if result.get("from_name") != name:
            continue
        text = (result.get("value") or {}).get("text")
        if isinstance(text, list):
            return " ".join(_clean(item) for item in text if _clean(item))
        return _clean(text)
    return ""


def _source_hash(data: dict[str, Any]) -> str:
    source_text = "\n\n".join(
        part for part in (_clean(data.get("title")), _clean(data.get("body"))) if part
    )
    if not source_text:
        return ""
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]


def _claim_hash(claim: str) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", _clean(claim).lower())
        if not unicodedata.combining(character)
    )
    normalized = "".join(character if character.isalnum() else " " for character in normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else ""


def _canonical_eligibility(eligibility: str, claim: str) -> str:
    if eligibility == "APTA":
        return "APTA_CON_CLAIM_LEGACY"
    if not eligibility and claim:
        return "APTA_CON_CLAIM_LEGACY"
    return eligibility


def _read_excluded_record_ids(path_value: str) -> set[str]:
    if not path_value:
        return set()
    path = Path(path_value).expanduser()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "record_id" not in rows[0]:
        raise ValueError("El archivo de exclusiones debe contener la columna record_id.")
    record_ids = {_clean(row.get("record_id")) for row in rows}
    record_ids.discard("")
    return record_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Export JSON de las tareas anotadas.")
    parser.add_argument("--output", required=True, help="Manifiesto CSV que quedará congelado.")
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--train", type=int, default=320)
    parser.add_argument("--validation", type=int, default=40)
    parser.add_argument("--test", type=int, default=40)
    parser.add_argument(
        "--max-claim-words",
        type=int,
        default=80,
        help="Máximo de palabras para un claim central; evita pegar el artículo entero.",
    )
    parser.add_argument(
        "--min-claim-words",
        type=int,
        default=5,
        help="Mínimo de palabras para evitar URLs o fragmentos incompletos.",
    )
    parser.add_argument(
        "--exclude-record-ids",
        default="",
        help="CSV opcional con record_id revisados manualmente que no deben entrar al Gold.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    expected_total = args.train + args.validation + args.test
    if min(args.train, args.validation, args.test) <= 0:
        raise ValueError("Los tamaños train, validation y test deben ser positivos.")
    if args.min_claim_words <= 0 or args.max_claim_words < args.min_claim_words:
        raise ValueError("Los límites de palabras para el claim no son válidos.")

    output = Path(args.output).expanduser()
    if output.exists():
        raise FileExistsError(
            f"El manifiesto ya existe: {output}. No se reemplaza para preservar el split congelado."
        )

    payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    tasks = _tasks_from_export(payload)
    excluded_record_ids = _read_excluded_record_ids(args.exclude_record_ids)
    seen_record_ids: set[str] = set()
    candidates_by_source: dict[str, dict[str, str]] = {}
    excluded = Counter()

    for task in tasks:
        data = task.get("data") or {}
        record_id = _clean(data.get("record_id"))
        if not record_id:
            raise ValueError("Una tarea exportada no contiene data.record_id.")
        if record_id in seen_record_ids:
            raise ValueError(f"record_id duplicado en el export: {record_id}")
        seen_record_ids.add(record_id)
        if record_id in excluded_record_ids:
            excluded["excluido_por_revision_manual"] += 1
            continue

        annotation = _latest_completed_annotation(task)
        if annotation is None:
            excluded["sin_anotacion_completa"] += 1
            continue
        results = annotation.get("result", [])
        eligibility = _result_choice(results, ("news_eligibility", "news_utility"))
        claim = _result_text(results, "reference_claim")
        eligibility = _canonical_eligibility(eligibility, claim)
        if eligibility not in APTA:
            excluded[eligibility or "sin_elegibilidad"] += 1
            continue
        if not claim:
            excluded["claim_vacio"] += 1
            continue
        claim_words = len(claim.split())
        if claim_words < args.min_claim_words:
            excluded["claim_menos_de_minimo_palabras"] += 1
            continue
        if claim_words > args.max_claim_words:
            excluded["claim_excede_maximo_palabras"] += 1
            continue

        source_hash = _source_hash(data)
        if not source_hash:
            excluded["sin_texto_fuente"] += 1
            continue
        candidate = {
            "record_id": record_id,
            "source_hash": source_hash,
            "claim_hash": _claim_hash(claim),
            "country": _clean(data.get("country")),
            "published_at": _clean(data.get("published_at")),
        }
        existing = candidates_by_source.get(source_hash)
        if existing is None or record_id < existing["record_id"]:
            if existing is not None:
                excluded["texto_fuente_duplicado"] += 1
            candidates_by_source[source_hash] = candidate
        else:
            excluded["texto_fuente_duplicado"] += 1

    unique_by_claim: dict[str, dict[str, str]] = {}
    for candidate in sorted(candidates_by_source.values(), key=lambda row: row["record_id"]):
        claim_hash = candidate["claim_hash"]
        existing = unique_by_claim.get(claim_hash)
        if existing is None:
            unique_by_claim[claim_hash] = candidate
        else:
            excluded["reference_claim_duplicado"] += 1

    candidates = sorted(unique_by_claim.values(), key=lambda row: row["record_id"])
    if len(candidates) < expected_total:
        raise ValueError(
            f"Solo hay {len(candidates)} tareas aptas y sin texto duplicado; "
            f"se necesitan {expected_total}."
        )

    selected = random.Random(args.seed).sample(candidates, expected_total)
    random.Random(args.seed + 1).shuffle(selected)
    for index, row in enumerate(selected):
        row["split"] = (
            "train"
            if index < args.train
            else "validation"
            if index < args.train + args.validation
            else "test"
        )
        row["sample_role"] = "primary"

    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "record_id",
        "split",
        "sample_role",
        "source_hash",
        "claim_hash",
        "country",
        "published_at",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sorted(selected, key=lambda row: (row["split"], row["record_id"])))

    print(
        json.dumps(
            {
                "exported_tasks": len(tasks),
                "eligible_unique_candidates": len(candidates),
                "selected": len(selected),
                "seed": args.seed,
                "split_counts": {
                    "train": args.train,
                    "validation": args.validation,
                    "test": args.test,
                },
                "manual_exclusions": len(excluded_record_ids),
                "min_claim_words": args.min_claim_words,
                "max_claim_words": args.max_claim_words,
                "excluded": dict(sorted(excluded.items())),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
