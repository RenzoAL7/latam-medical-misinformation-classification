#!/usr/bin/env python3
"""Descarga localmente un inventario de noticias desde NewsData API.

La clave se toma de --api-key o NEWSDATA_API_KEY y nunca se escribe en los
artefactos ni en la salida estándar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.collection import normalize_newsdata_articles


DEFAULT_QUERY = "(salud OR medicina OR médico OR enfermedad OR vacuna OR tratamiento)"
INVENTORY_COLUMNS = [
    "record_id", "source_name", "canonical_url", "published_at", "title",
    "subtitle_or_bajada", "topic", "selection_status", "retrieved_at", "country",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="CSV local del inventario de noticias.")
    parser.add_argument("--api-key", default=os.getenv("NEWSDATA_API_KEY", ""))
    parser.add_argument("--countries", required=True, help="ISO-2 separados por coma, p. ej. pe,ec,co.")
    parser.add_argument("--endpoint", default="https://newsdata.io/api/1/latest")
    parser.add_argument("--language", default="es")
    parser.add_argument("--category", default="health")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=1.0)
    return parser.parse_args()


def request_pages(
    *, api_key: str, endpoint: str, country: str, language: str, category: str,
    query: str, size: int, max_pages: int, timeout: float, delay: float,
) -> list[object]:
    articles: list[object] = []
    page: str | None = None
    for index in range(max_pages):
        if index and delay:
            time.sleep(delay)
        params = {
            "apikey": api_key,
            "language": language,
            "category": category,
            "country": country,
            "q": query,
            "size": str(size),
            "removeduplicate": "1",
            "video": "0",
        }
        if page:
            params["page"] = page
        request = Request(
            f"{endpoint}?{urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": "mednews-local/1.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"NewsData devolvió HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"No se pudo consultar NewsData: {exc}") from exc
        if not isinstance(payload, dict) or str(payload.get("status", "")).lower() in {"error", "failed"}:
            raise RuntimeError(f"Respuesta inválida de NewsData: {str(payload)[:500]}")
        page_results = payload.get("results", [])
        if isinstance(page_results, list):
            articles.extend(page_results)
        page = str(payload.get("nextPage") or "").strip() or None
        if not page:
            break
    return articles


def main() -> None:
    args = parse_args()
    api_key = args.api_key.strip()
    if not api_key:
        raise RuntimeError("Define NEWSDATA_API_KEY o proporciona --api-key.")
    if not (1 <= args.size <= 10 and args.max_pages >= 1 and args.timeout > 0 and args.delay >= 0):
        raise ValueError("size debe estar entre 1 y 10; max-pages >= 1; timeout/delay no negativos.")
    countries = [item.strip().lower() for item in args.countries.split(",") if item.strip()]
    if not countries or any(len(country) != 2 for country in countries):
        raise ValueError("countries debe contener códigos ISO-2 separados por coma.")

    retrieved_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, str]] = []
    for country in countries:
        articles = request_pages(
            api_key=api_key, endpoint=args.endpoint, country=country,
            language=args.language, category=args.category, query=args.query,
            size=args.size, max_pages=args.max_pages, timeout=args.timeout, delay=args.delay,
        )
        rows.extend(normalize_newsdata_articles(articles, retrieved_at=retrieved_at, requested_country=country))

    deduplicated = {row["record_id"]: row for row in rows}
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(deduplicated.values(), columns=INVENTORY_COLUMNS).to_csv(output, index=False, encoding="utf-8")
    print(json.dumps({"countries": countries, "inventory_rows": len(deduplicated), "output": str(output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
