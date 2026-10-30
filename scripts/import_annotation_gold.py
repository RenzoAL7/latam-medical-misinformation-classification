#!/usr/bin/env python3
"""Convierte un export JSON de anotaciones al Gold de Seminario 1.

La fase solo usa un claim central por noticia. El manifiesto final define las
300 noticias que entran al Gold; por eso una exportación de las 700 tareas no
incorpora las 400 de reserva por accidente.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any


ALLOWED_SPLITS = {"train", "validation", "test"}
APTA = {"APTA_CON_CLAIM", "APTA_CON_CLAIM_LEGACY"}


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _tasks_from_export(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return [item for item in payload["tasks"] if isinstance(item, dict)]
    raise ValueError(
        "El export debe ser un JSON de tareas descargado desde la herramienta de anotación."
    )


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
        value = (result.get("value") or {}).get("text")
        if isinstance(value, list):
            text = " ".join(_clean(item) for item in value if _clean(item))
            if text:
                return text
        elif _clean(value):
            return _clean(value)
    return ""


def _read_manifest(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"record_id", "split"}
    missing = required.difference(rows[0].keys() if rows else set())
    if missing:
        raise ValueError("El manifiesto no contiene: " + ", ".join(sorted(missing)))

    manifest: dict[str, dict[str, str]] = {}
    for row in rows:
        record_id = _clean(row.get("record_id"))
        split = _clean(row.get("split"))
        if not record_id:
            raise ValueError("El manifiesto contiene un record_id vacío.")
        if split not in ALLOWED_SPLITS:
            raise ValueError(f"Split inválido para {record_id}: {split or '(vacío)'}.")
        if record_id in manifest:
            raise ValueError(f"record_id duplicado en el manifiesto: {record_id}")
        manifest[record_id] = {key: _clean(value) for key, value in row.items()}
    if not manifest:
        raise ValueError("El manifiesto está vacío.")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        required=True,
        help="Export JSON descargado desde la herramienta de anotación.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="CSV final de las noticias que forman el Gold, con record_id y split.",
    )
    parser.add_argument("--output", required=True, help="CSV Gold de salida.")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="Falla si alguna noticia del manifiesto no tiene un claim apto y completo.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = _read_manifest(Path(args.manifest).expanduser())
    payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    tasks = _tasks_from_export(payload)

    tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        record_id = _clean((task.get("data") or {}).get("record_id"))
        if not record_id:
            raise ValueError("Una tarea exportada no contiene data.record_id.")
        if record_id in tasks_by_id:
            raise ValueError(f"record_id duplicado en el export: {record_id}")
        tasks_by_id[record_id] = task

    rows: list[dict[str, str]] = []
    pending: list[str] = []
    for record_id, manifest_row in manifest.items():
        task = tasks_by_id.get(record_id)
        if task is None:
            pending.append(f"{record_id}: no aparece en el export")
            continue
        annotation = _latest_completed_annotation(task)
        results = annotation.get("result", []) if annotation else []
        eligibility = _result_choice(results, ("news_eligibility", "news_utility"))
        claim = _result_text(results, "reference_claim")
        if not eligibility and claim:
            eligibility = "APTA_CON_CLAIM_LEGACY"
        if eligibility not in APTA:
            pending.append(f"{record_id}: elegibilidad={eligibility or 'pendiente'}")
            continue
        if not claim:
            pending.append(f"{record_id}: reference_claim vacío")
            continue

        data = task.get("data") or {}
        title = _clean(data.get("title"))
        body = _clean(data.get("body"))
        source_text = "\n\n".join(part for part in (title, body) if part)
        if not source_text:
            pending.append(f"{record_id}: título y cuerpo vacíos")
            continue
        rows.append(
            {
                "record_id": record_id,
                "country": _clean(data.get("country")),
                "published_at": _clean(data.get("published_at")),
                "source_name": _clean(data.get("source_name")),
                "topic": _clean(data.get("topic")),
                "title": title,
                "body": body,
                "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16],
                "source_text": source_text,
                "reference_claim": claim,
                "news_eligibility": eligibility,
                "split": manifest_row["split"],
                "sample_role": manifest_row.get("sample_role", "primary") or "primary",
            }
        )

    if args.require_complete and pending:
        preview = "; ".join(pending[:10])
        suffix = "" if len(pending) <= 10 else f"; y {len(pending) - 10} más"
        raise ValueError(
            "El Gold no está completo para el manifiesto final: " + preview + suffix
        )
    if not rows:
        raise ValueError("No se encontró ningún claim apto y completo para exportar.")

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    split_counts = {split: sum(row["split"] == split for row in rows) for split in sorted(ALLOWED_SPLITS)}
    print(
        json.dumps(
            {
                "exported_tasks": len(tasks),
                "manifest_rows": len(manifest),
                "gold_rows": len(rows),
                "pending_or_excluded_manifest_rows": len(pending),
                "split_counts": split_counts,
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
