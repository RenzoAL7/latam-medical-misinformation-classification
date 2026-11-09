"""Prepara un lote importable en Label Studio desde un CSV Silver Body.

Un evento de Object Storage entrega un `silver/body/*.csv`. La función elige hasta
`LABELSTUDIO_TARGET_TASKS` noticias con cuerpo OK, en round robin por país, y guarda
en `silver/labelstudio/<run_id>/` las tareas, las filas no usadas y un manifiesto.
Solo selecciona y separa filas: no extrae claims ni evalúa veracidad.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import unicodedata
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)
csv.field_size_limit(10_000_000)

TASK_COLUMNS = [
    "record_id", "title", "body", "canonical_url", "source_name",
    "published_at", "country", "subtitle_or_bajada",
]
UNUSED_REASON_COLUMN = "motivo_no_usada"
SURPLUS_REASON = "EXCEDENTE_OK"
FINGERPRINTS_FILE = "_proyecto9_huellas.jsonl"


def title_key(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title or "")
    ascii_title = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", ascii_title.lower()).strip()


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def title_fingerprint(title: str) -> str:
    key = title_key(title)
    return fingerprint(key) if key else ""


def body_fingerprint(body: str) -> str:
    return fingerprint(title_key(body)[:600]) if len((body or "").strip()) > 200 else ""


def empty_history() -> dict[str, set[str]]:
    return {"ids": set(), "titles": set(), "bodies": set()}


def add_to_history(history: dict[str, set[str]], row: dict[str, str]) -> None:
    history["ids"].add((row.get("record_id") or "").strip())
    history["titles"].add(title_fingerprint(row.get("title", "")))
    history["bodies"].add(body_fingerprint(row.get("body", "")))
    for values in history.values():
        values.discard("")


def load_history(
    client: Any, namespace: str, bucket: str, prefix: str, current_run: str
) -> dict[str, set[str]]:
    """Reúne lo ya enviado a Label Studio según los archivos del bucket."""
    import oci

    history = empty_history()
    objects = oci.pagination.list_call_get_all_results(
        client.list_objects, namespace, bucket, prefix=f"{prefix}/", fields="name"
    ).data.objects
    for item in objects:
        if item.name == f"{prefix}/{FINGERPRINTS_FILE}":
            content = client.get_object(namespace, bucket, item.name).data.content.decode("utf-8")
            for line in filter(None, content.splitlines()):
                entry = json.loads(line)
                history["ids"].add(entry.get("rid") or "")
                history["titles"].add(entry.get("t") or "")
                history["bodies"].add(entry.get("b") or "")
        elif item.name.endswith("/labelstudio_tasks.csv") and f"/{current_run}/" not in item.name:
            content = client.get_object(namespace, bucket, item.name).data.content.decode("utf-8")
            for row in csv.DictReader(io.StringIO(content, newline="")):
                add_to_history(history, row)
    for values in history.values():
        values.discard("")
    return history


def select_rows(
    rows: list[dict[str, str]], target: int, history: dict[str, set[str]] | None = None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Devuelve (elegidas, no usadas); ninguna fila de entrada se pierde."""
    history = history or empty_history()
    unused: list[dict[str, str]] = []
    pools: dict[str, deque[dict[str, str]]] = defaultdict(deque)
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    seen_bodies: set[str] = set()

    for row in rows:
        status = (row.get("extraction_status") or "SIN_ESTADO").strip()
        record_id = (row.get("record_id") or "").strip()
        title_fp = title_fingerprint(row.get("title", ""))
        body_fp = body_fingerprint(row.get("body", ""))
        if status != "OK":
            reason = status
        elif not record_id or not title_fp or not (row.get("body") or "").strip():
            reason = "CAMPOS_INCOMPLETOS"
        elif record_id in history["ids"] or title_fp in history["titles"] or body_fp in history["bodies"]:
            reason = "DUPLICADO_HISTORICO"
        elif record_id in seen_ids:
            reason = "DUPLICADO_RECORD_ID"
        elif title_fp in seen_titles:
            reason = "DUPLICADO_TITULO"
        elif body_fp and body_fp in seen_bodies:
            reason = "DUPLICADO_CUERPO"
        else:
            seen_ids.add(record_id)
            seen_titles.add(title_fp)
            seen_bodies.add(body_fp)
            pools[(row.get("country") or "").strip().lower()].append(row)
            continue
        unused.append({**row, UNUSED_REASON_COLUMN: reason})

    selected: list[dict[str, str]] = []
    countries = sorted(pools)
    while len(selected) < target and any(pools[c] for c in countries):
        for country in countries:
            if pools[country] and len(selected) < target:
                selected.append(pools[country].popleft())

    for country in countries:
        unused.extend({**row, UNUSED_REASON_COLUMN: SURPLUS_REASON} for row in pools[country])
    return selected, unused


def run_id_for(object_name: str) -> str:
    match = re.search(r"run_\d{8}T\d{6}Z_[0-9a-f]+", object_name)
    if match:
        return match.group(0)
    return re.sub(r"[^A-Za-z0-9_-]+", "_", object_name.rsplit("/", 1)[-1].removesuffix(".csv"))


def to_csv(rows: list[dict[str, str]], columns: list[str]) -> bytes:
    # Sin BOM: con BOM, Label Studio leería la primera columna con otro nombre.
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def build_manifest(
    source_object: str, run_id: str, target: int,
    selected: list[dict[str, str]], unused: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "source_object": source_object,
        "run_id": run_id,
        "target_tasks": target,
        "selected_tasks": len(selected),
        "status": "COMPLETE" if len(selected) >= target else "PARTIAL",
        "unused_rows": len(unused),
        "unused_by_reason": dict(sorted(Counter(r[UNUSED_REASON_COLUMN] for r in unused).items())),
        "selected_by_country": dict(sorted(Counter(r.get("country", "") for r in selected).items())),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def event_object_name(payload: bytes, bucket: str, body_prefix: str) -> str:
    event = json.loads(payload.decode("utf-8"))
    data = event["data"]
    name = data["resourceName"]
    event_bucket = data.get("additionalDetails", {}).get("bucketName")
    if event_bucket != bucket or not name.startswith(f"{body_prefix}/") or not name.endswith(".csv"):
        raise ValueError(f"Evento fuera de alcance: {event_bucket}/{name}")
    return name


def handler(ctx: Any, data: io.BytesIO | None = None) -> Any:
    import oci
    from fdk import response

    namespace = os.environ["OBJECT_STORAGE_NAMESPACE"]
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    body_prefix = os.getenv("SILVER_BODY_PREFIX", "silver/body").rstrip("/")
    output_prefix = os.getenv("LABELSTUDIO_PREFIX", "silver/labelstudio").rstrip("/")
    target = int(os.getenv("LABELSTUDIO_TARGET_TASKS", "50"))

    source_object = event_object_name(data.getvalue(), bucket, body_prefix)
    client = oci.object_storage.ObjectStorageClient(
        config={}, signer=oci.auth.signers.get_resource_principals_signer()
    )
    raw = client.get_object(namespace, bucket, source_object).data.content
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")))

    run_id = run_id_for(source_object)
    history = load_history(client, namespace, bucket, output_prefix, run_id)
    selected, unused = select_rows(rows, target, history)
    manifest = build_manifest(source_object, run_id, target, selected, unused)
    folder = f"{output_prefix}/{run_id}"
    unused_columns = list(rows[0].keys()) + [UNUSED_REASON_COLUMN] if rows else [UNUSED_REASON_COLUMN]

    # El manifiesto va al final: su presencia indica que el lote quedó completo.
    outputs = [
        (f"{folder}/no_usadas.csv", to_csv(unused, unused_columns), "text/csv"),
        (f"{folder}/labelstudio_tasks.csv", to_csv(selected, TASK_COLUMNS), "text/csv"),
        (f"{folder}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"),
    ]
    for name, content, content_type in outputs:
        client.put_object(namespace, bucket, name, content, content_type=content_type)

    LOGGER.info("Lote %s: %s tareas, %s no usadas", run_id, len(selected), len(unused))
    return response.Response(
        ctx, response_data=json.dumps(manifest), headers={"Content-Type": "application/json"}
    )
