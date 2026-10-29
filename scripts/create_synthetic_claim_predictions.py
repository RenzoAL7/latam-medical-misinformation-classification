#!/usr/bin/env python3
"""Genera predicciones sintéticas para probar la fase posterior a la extracción.

El archivo generado imita la salida JSONL de ``run_inference.py``. No contiene
noticias reales ni resultados de modelos y nunca debe usarse como evaluación
de tesis.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


MODELS = [
    ("umt5_base_es", "uMT5-Base fine-tuned (es)"),
    ("t5_small", "T5-Small fine-tuned"),
    ("pegasus_large", "Pegasus-Large fine-tuned"),
    ("claimgen_bart_transfer", "CLAIMGEN-BART transfer"),
]

TOPICS = [
    ("hipertensión arterial", "una dieta con menor contenido de sodio", "la presión arterial sistólica"),
    ("diabetes tipo 2", "un programa de actividad física moderada", "la glucosa en ayunas"),
    ("asma persistente", "un plan educativo de autocuidado", "la frecuencia de las exacerbaciones"),
    ("insomnio en adultos", "una intervención de higiene del sueño", "la duración del sueño"),
    ("dolor lumbar crónico", "un programa de ejercicios supervisados", "la intensidad del dolor"),
    ("obesidad en adultos", "una intervención nutricional estructurada", "el peso corporal"),
    ("ansiedad leve", "una intervención breve basada en respiración", "los síntomas de ansiedad"),
    ("rinitis alérgica", "un tratamiento preventivo hipotético", "la congestión nasal"),
    ("enfermedad renal crónica", "un seguimiento clínico periódico", "la función renal"),
    ("artritis de rodilla", "un programa de fortalecimiento muscular", "la movilidad"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=100)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/synthetic/claims_predictions_100.jsonl"),
    )
    return parser.parse_args()


def source_text(index: int, condition: str, intervention: str, outcome: str, weeks: int) -> str:
    return (
        f"Noticia sintética {index}: en adultos con {condition}, un escenario hipotético "
        f"evaluó {intervention} durante {weeks} semanas y observó cambios en {outcome}."
    )


def claim_text(
    model_id: str,
    condition: str,
    intervention: str,
    outcome: str,
    weeks: int,
    index: int,
) -> str:
    if model_id == "umt5_base_es":
        return (
            f"En adultos con {condition}, {intervention} se asoció con cambios en "
            f"{outcome} durante {weeks} semanas; esta asociación no demuestra causalidad."
        )
    if model_id == "t5_small":
        return (
            f"Un escenario hipotético en adultos con {condition} relacionó {intervention} "
            f"con cambios en {outcome} después de {weeks} semanas, sin demostrar causalidad."
        )
    if model_id == "pegasus_large":
        return (
            f"En adultos con {condition}, una intervención de {weeks} semanas basada en "
            f"{intervention} fue relacionada con {outcome}; el resultado es hipotético."
        )
    return (
        f"El escenario sintético {index} plantea que, en adultos con {condition}, "
        f"{intervention} podría modificar {outcome} en {weeks} semanas, pero no prueba "
        "un efecto causal."
    )


def build_rows(records: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(1, records + 1):
        condition, intervention, outcome = TOPICS[(index - 1) % len(TOPICS)]
        weeks = 4 + ((index - 1) % 9)
        record_id = f"synthetic-record-{index:04d}"
        source = source_text(index, condition, intervention, outcome, weeks)
        for model_id, display_name in MODELS:
            rows.append(
                {
                    "record_id": record_id,
                    "source_hash": f"synthetic-{index:04d}",
                    "model_id": model_id,
                    "model_display_name": display_name,
                    "checkpoint": "synthetic-fixture",
                    "input_text": source,
                    "claim": claim_text(
                        model_id, condition, intervention, outcome, weeks, index
                    ),
                    "device": "synthetic",
                    "generated_at": "2026-10-29T00:00:00+00:00",
                    "synthetic": True,
                    "split": "synthetic_demo",
                }
            )
    return rows


def main() -> None:
    args = parse_args()
    if args.records < 1:
        raise ValueError("--records debe ser mayor que cero.")
    rows = build_rows(args.records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "records": args.records,
                "models": len(MODELS),
                "rows": len(rows),
                "synthetic": True,
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
