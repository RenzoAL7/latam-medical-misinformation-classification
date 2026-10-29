"""Registro explícito de familias de modelos y sus parámetros de referencia."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    base_checkpoint: str
    input_adapter: str
    paper: str
    paper_url: str
    training: dict[str, Any]
    generation: dict[str, Any]
    notes: str


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def registry_path() -> Path:
    return repository_root() / "configs" / "models.json"


def load_registry() -> dict[str, ModelSpec]:
    payload = json.loads(registry_path().read_text(encoding="utf-8"))
    specs = [ModelSpec(**item) for item in payload["models"]]
    return {spec.id: spec for spec in specs}


def get_model(model_id: str) -> ModelSpec:
    registry = load_registry()
    try:
        return registry[model_id]
    except KeyError as exc:
        available = ", ".join(sorted(registry))
        raise ValueError(f"Modelo desconocido: {model_id}. Disponibles: {available}") from exc


def model_ids() -> list[str]:
    return sorted(load_registry())
