#!/usr/bin/env python3
"""Descarga pesos base de las cuatro familias a la caché local de Hugging Face."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.registry import get_model, model_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Descarga las cuatro familias.")
    group.add_argument("--model", choices=model_ids(), help="Descarga una familia.")
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Directorio de caché opcional. Por defecto se usa la caché de Hugging Face.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("Instala las dependencias: make install") from exc

    selected = model_ids() if args.all else [args.model]
    downloaded = []
    cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else None
    for model_id in selected:
        spec = get_model(model_id)
        location = snapshot_download(
            repo_id=spec.base_checkpoint,
            cache_dir=str(cache_dir) if cache_dir else None,
        )
        downloaded.append(
            {
                "model_id": model_id,
                "base_checkpoint": spec.base_checkpoint,
                "cache_snapshot": location,
                "status": "downloaded",
            }
        )

    print(json.dumps({"models": downloaded}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
