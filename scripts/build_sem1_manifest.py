#!/usr/bin/env python3
"""Construye y congela el manifiesto 240/30/30 desde un export de anotaciones.

Las 700 tareas son un pool de cribado. Este script toma únicamente las tareas
con un claim central apto, excluye textos duplicados y selecciona 300 con una
semilla reproducible. El CSV resultante es la lista cerrada que entra al Gold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
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
    return completed[-1] if completed else None


def _result_choice(results: list[dict[str, Any]], names: tuple[str, ...]) -> str:
    for result in results:
        if result.get("from_name") not in names:
            continue
        choices = (result.get("value") or {}).get("choices")
        if isinstance(choices, list) and choices:
            return _clean(choices[0])
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Export JSON de las tareas anotadas.")
    parser.add_argument("--output", required=True, help="Manifiesto CSV que quedará congelado.")
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--train", type=int, default=240)
    parser.add_argument("--validation", type=int, default=30)
    parser.add_argument("--test", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    expected_total = args.train + args.validation + args.test
    if min(args.train, args.validation, args.test) <= 0:
        raise ValueError("Los tamaños train, validation y test deben ser positivos.")

    output = Path(args.output).expanduser()
    if output.exists():
        raise FileExistsError(
            f"El manifiesto ya existe: {output}. No se reemplaza para preservar el split congelado."
        )

    payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    tasks = _tasks_from_export(payload)
    seen_record_ids: set[str] = set()
    unique_by_source: dict[str, dict[str, str]] = {}
    excluded = Counter()

    for task in tasks:
        data = task.get("data") or {}
        record_id = _clean(data.get("record_id"))
        if not record_id:
            raise ValueError("Una tarea exportada no contiene data.record_id.")
        if record_id in seen_record_ids:
            raise ValueError(f"record_id duplicado en el export: {record_id}")
        seen_record_ids.add(record_id)

        annotation = _latest_completed_annotation(task)
        if annotation is None:
            excluded["sin_anotacion_completa"] += 1
            continue
        results = annotation.get("result", [])
        eligibility = _result_choice(results, ("news_eligibility", "news_utility"))
        claim = _result_text(results, "reference_claim")
        if not eligibility and claim:
            eligibility = "APTA_CON_CLAIM_LEGACY"
        if eligibility not in APTA:
            excluded[eligibility or "sin_elegibilidad"] += 1
            continue
        if not claim:
            excluded["claim_vacio"] += 1
            continue

        source_hash = _source_hash(data)
        if not source_hash:
            excluded["sin_texto_fuente"] += 1
            continue
        candidate = {
            "record_id": record_id,
            "source_hash": source_hash,
            "country": _clean(data.get("country")),
            "published_at": _clean(data.get("published_at")),
        }
        existing = unique_by_source.get(source_hash)
        if existing is None or record_id < existing["record_id"]:
            if existing is not None:
                excluded["texto_fuente_duplicado"] += 1
            unique_by_source[source_hash] = candidate
        else:
            excluded["texto_fuente_duplicado"] += 1

    candidates = sorted(unique_by_source.values(), key=lambda row: row["record_id"])
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
    fieldnames = ["record_id", "split", "sample_role", "source_hash", "country", "published_at"]
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
                "excluded": dict(sorted(excluded.items())),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
