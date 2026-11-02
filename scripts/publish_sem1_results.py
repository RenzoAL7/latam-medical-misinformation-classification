#!/usr/bin/env python3
"""Publica solamente tablas y figuras agregadas de una corrida Sem1."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "01_distribucion_particiones",
    "02_longitudes_palabras",
    "03_distribucion_pais_territorio",
    "04_checkpoints_seleccionados",
    "05_metricas_principales",
    "06_desglose_precision_recall_f1",
    "07_diagnosticos_generacion",
)
FIGURES = (
    "01_histogramas_longitud_palabras",
    "02_curvas_perdida_entrenamiento_validacion",
    "03_comparacion_metricas",
    "04_distribucion_metricas_por_noticia",
)
MODEL_ORDER = ("T5S", "BARTO", "mBART-50", "uMT5-Base")
TRAINING_KEYS = (
    "epochs", "learning_rate", "weight_decay", "warmup_steps",
    "effective_batch_size", "max_source_length", "max_target_length",
)
GENERATION_KEYS = ("num_beams", "max_new_tokens")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", default=None)
    return parser.parse_args()


def shared_values(configurations: dict[str, object], section: str, keys: tuple[str, ...]) -> dict[str, object]:
    if not configurations:
        raise ValueError("La corrida no registra configuraciones de modelos.")
    values = list(configurations.values())
    first = values[0]
    if not isinstance(first, dict) or not isinstance(first.get(section), dict):
        raise ValueError(f"Falta la sección {section} en la corrida.")
    expected = {key: first[section][key] for key in keys}
    for config in values[1:]:
        if not isinstance(config, dict) or not isinstance(config.get(section), dict):
            raise ValueError(f"Falta la sección {section} en la corrida.")
        if {key: config[section][key] for key in keys} != expected:
            raise ValueError(f"La corrida no usa un {section} común; no se publica como controlada.")
    return expected


def copy_required(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def render_readme(run_id: str, metrics: pd.DataFrame, training: dict[str, object]) -> str:
    required = {"Modelo", "N", "METEOR", "ROUGE-L F1", "BERTScore F1"}
    if missing := sorted(required - set(metrics.columns)):
        raise ValueError(f"La tabla de métricas no contiene: {', '.join(missing)}")
    rows = metrics.set_index("Modelo").reindex(MODEL_ORDER)
    if rows[["N", "METEOR", "ROUGE-L F1", "BERTScore F1"]].isna().any().any():
        raise ValueError("La tabla de métricas no contiene los cuatro modelos.")
    table_rows = [
        "| {model} | {n:d} | {meteor:.4f} | {rouge:.4f} | {bert:.4f} |".format(
            model=model,
            n=int(row["N"]),
            meteor=float(row["METEOR"]),
            rouge=float(row["ROUGE-L F1"]),
            bert=float(row["BERTScore F1"]),
        )
        for model, row in rows.iterrows()
    ]
    lines = [
        "# Resultados Sem1 — extracción de afirmaciones médicas centrales",
        "",
        f"Corrida controlada: `{run_id}`. Los cuatro modelos usan el mismo conjunto de 400 noticias: 320 de entrenamiento, 40 de validación y 40 de prueba.",
        "",
        f"La configuración común fija {int(training['epochs'])} épocas como máximo, batch efectivo {int(training['effective_batch_size'])}, learning rate {float(training['learning_rate']):g}, weight decay {float(training['weight_decay']):g}, warmup {int(training['warmup_steps'])} y cuatro beams. El checkpoint se elige por menor pérdida de validación dentro de ese presupuesto y se evalúa una sola vez sobre el test.",
        "",
        "## Métricas principales",
        "",
        "Media por noticia en escala de 0 a 1. METEOR es la métrica principal; ROUGE-L F1 y BERTScore F1 son complementarias.",
        "",
        "| Modelo | Noticias de prueba | METEOR | ROUGE-L F1 | BERTScore F1 |",
        "| --- | ---: | ---: | ---: | ---: |",
        *table_rows,
        "",
        "## Tablas exportadas",
        "",
        "Los CSV y HTML de las siete tablas se encuentran en `tables/`. Las cuatro figuras se encuentran en `figures/` en PNG y PDF.",
        "",
        "## Alcance",
        "",
        "La entrega mide similitud entre el claim central generado y la referencia anotada. No demuestra veracidad médica ni incluye extracción multiclaim, traducción, generación de consultas, recuperación PubMed o ranking. El corpus, las anotaciones, las predicciones individuales y los pesos permanecen locales.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else ROOT / "docs" / "results" / run_dir.name
    status_path = run_dir / "run_status.json"
    if not status_path.is_file():
        raise FileNotFoundError(status_path)
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if status.get("status") != "completed":
        raise ValueError("Solo se publican corridas completadas.")
    training = shared_values(status.get("model_configurations", {}), "training", TRAINING_KEYS)
    shared_values(status.get("model_configurations", {}), "generation", GENERATION_KEYS)
    paper_results = run_dir / "evaluation" / "paper_results"
    metrics = pd.read_csv(paper_results / "tables" / "05_metricas_principales.csv")
    for stem in TABLES:
        for extension in ("csv", "html"):
            copy_required(paper_results / "tables" / f"{stem}.{extension}", output_dir / "tables" / f"{stem}.{extension}")
    for stem in FIGURES:
        for extension in ("png", "pdf"):
            copy_required(paper_results / "figures" / f"{stem}.{extension}", output_dir / "figures" / f"{stem}.{extension}")
    copy_required(paper_results / "manifest.json", output_dir / "manifest.json")
    (output_dir / "README.md").write_text(render_readme(run_dir.name, metrics, training), encoding="utf-8")
    print(json.dumps({"run_id": run_dir.name, "published": str(output_dir)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
