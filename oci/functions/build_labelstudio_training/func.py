"""Build one auditable training CSV from a Label Studio export in Object Storage.

Label Studio is private on PUB_VM, so this Function deliberately does not call
its API.  The VM-side exporter places the raw JSON export in Silver; this
Function resolves the Evidence and Body objects referenced by that export and
publishes one CSV in Gold.  An empty scheduled payload means "use the latest
export"; an Object Storage event can identify one export explicitly.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import oci
from fdk import response

import dataset_builder


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
LIMA_TZ = ZoneInfo("America/Lima")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _prefix(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().strip("/")
    if not value:
        raise RuntimeError(f"{name} cannot be empty")
    return value


def _object_storage_client() -> Any:
    signer = oci.auth.signers.get_resource_principals_signer()
    return oci.object_storage.ObjectStorageClient(config={}, signer=signer)


def _list_objects(client: Any, namespace: str, bucket: str, prefix: str) -> list[Any]:
    response_data = client.list_objects(
        namespace_name=namespace,
        bucket_name=bucket,
        prefix=f"{prefix.rstrip('/')}/",
        fields="name,size,timeCreated,timeModified",
    ).data
    objects = getattr(response_data, "objects", None)
    if objects is None and isinstance(response_data, list):
        objects = response_data
    return list(objects or [])


def _object_name(item: Any) -> str:
    return str(getattr(item, "name", "") or (item.get("name", "") if isinstance(item, dict) else ""))


def _object_time(item: Any) -> str:
    value = getattr(item, "time_modified", None) or getattr(item, "time_created", None)
    if value is None and isinstance(item, dict):
        value = item.get("time-modified") or item.get("time-created") or item.get("timeModified")
    return str(value or "")


def _read_payload(data: io.BytesIO | None) -> dict[str, Any]:
    if data is None:
        return {}
    raw = data.read()
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invocation payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Invocation payload must be a JSON object")
    return payload


def _event_export_name(payload: dict[str, Any], bucket: str, prefix: str) -> str | None:
    event_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    details = event_data.get("additionalDetails", {}) if isinstance(event_data, dict) else {}
    if not isinstance(details, dict):
        details = {}
    event_bucket = str(details.get("bucketName") or event_data.get("bucketName") or "").strip()
    if event_bucket and event_bucket != bucket:
        raise RuntimeError(f"Event bucket {event_bucket!r} does not match configured bucket")
    name = str(
        event_data.get("resourceName")
        or event_data.get("objectName")
        or payload.get("resourceName")
        or ""
    ).lstrip("/")
    expected = f"{prefix}/"
    suffix = name[len(expected) :] if name.startswith(expected) else ""
    if not suffix or "/" in suffix or not suffix.lower().endswith((".json", ".jsonl")):
        raise RuntimeError(f"Event does not identify a flat Label Studio export under {expected}")
    return name


def _latest_export(client: Any, namespace: str, bucket: str, prefix: str) -> str:
    candidates = [
        item
        for item in _list_objects(client, namespace, bucket, prefix)
        if _object_name(item).lower().endswith((".json", ".jsonl"))
        and "/" not in _object_name(item)[len(prefix) + 1 :]
    ]
    if not candidates:
        raise RuntimeError(f"No Label Studio export found under {prefix}/")
    return _object_name(max(candidates, key=lambda item: (_object_time(item), _object_name(item))))


def _get_object(client: Any, namespace: str, bucket: str, object_name: str) -> bytes:
    content = client.get_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
    ).data.content
    if not isinstance(content, (bytes, bytearray)):
        raise RuntimeError(f"Object Storage returned invalid content for {object_name}")
    return bytes(content)


def _put_object(client: Any, namespace: str, bucket: str, object_name: str, content: bytes, content_type: str) -> None:
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(content),
        content_type=content_type,
    )


def _evidence_object_names(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
    run_ids: set[str],
) -> list[str]:
    names = [_object_name(item) for item in _list_objects(client, namespace, bucket, prefix)]
    names = [name for name in names if name.lower().endswith(".jsonl") and not name.endswith(".manifest.json")]
    if not run_ids:
        return sorted(names)
    selected: list[str] = []
    for run_id in sorted(run_ids):
        exact = [
            name
            for name in names
            if name == f"{prefix}/evidence_{run_id}.jsonl"
            or name == f"{prefix}/run_id={run_id}/evidence.jsonl"
        ]
        if not exact:
            raise RuntimeError(f"Evidence JSONL for {run_id} was not found under {prefix}/")
        selected.append(sorted(exact)[-1])
    return selected


def _body_object_names(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
    evidence_blobs: list[tuple[str, bytes]],
) -> list[str]:
    referenced: set[str] = set()
    for object_name, raw in evidence_blobs:
        for row in dataset_builder._json_records(raw, object_name):
            source = str(row.get("source_body_object", "")).strip()
            if source:
                referenced.add(source)
    if referenced:
        return sorted(referenced)
    names = [_object_name(item) for item in _list_objects(client, namespace, bucket, prefix)]
    return sorted(name for name in names if name.lower().endswith(".csv"))


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=dataset_builder.TRAINING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n").encode("utf-8")


def _run_stamp() -> str:
    """Use one stable object name per Lima calendar day."""

    return datetime.now(LIMA_TZ).strftime("%Y%m%d")


def _write_error(
    client: Any | None,
    namespace: str,
    bucket: str,
    rejected_prefix: str,
    stamp: str,
    exc: Exception,
) -> str | None:
    if client is None:
        return None
    object_name = f"{rejected_prefix}/training_{stamp}.error.jsonl"
    record = {
        "stage": "training",
        "status": "ERROR",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _put_object(client, namespace, bucket, object_name, _jsonl_bytes([record]), "application/jsonl")
    except Exception:  # noqa: BLE001 - preserve original failure
        LOGGER.exception("Unable to write training rejection artifact")
        return None
    return object_name


def handler(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    client: Any | None = None
    namespace = ""
    bucket = ""
    rejected_prefix = _prefix("SILVER_REJECTED_PREFIX", "silver/rejected/training")
    stamp = _run_stamp()
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        export_prefix = _prefix("LABELSTUDIO_EXPORT_PREFIX", "silver/annotations/labelstudio")
        evidence_prefix = _prefix("SILVER_EVIDENCE_PREFIX", "silver/evidence")
        body_prefix = _prefix("SILVER_BODY_PREFIX", "silver/body")
        gold_prefix = _prefix("GOLD_TRAINING_PREFIX", "gold/training")
        payload = _read_payload(data)
        client = _object_storage_client()
        export_name = _event_export_name(payload, bucket, export_prefix) if payload else _latest_export(client, namespace, bucket, export_prefix)
        export_raw = _get_object(client, namespace, bucket, export_name)
        export_tasks = dataset_builder._json_records(export_raw, export_name)
        run_ids = {
            str(task.get("data", {}).get("evidence_run_id", "")).strip()
            for task in export_tasks
            if isinstance(task.get("data"), dict) and str(task["data"].get("evidence_run_id", "")).strip()
        }
        evidence_names = _evidence_object_names(client, namespace, bucket, evidence_prefix, run_ids)
        evidence_blobs = [(name, _get_object(client, namespace, bucket, name)) for name in evidence_names]
        body_names = _body_object_names(client, namespace, bucket, body_prefix, evidence_blobs)
        body_blobs = [(name, _get_object(client, namespace, bucket, name)) for name in body_names]
        result = dataset_builder.build_training(export_raw, export_name, evidence_blobs, body_blobs)
        training_name = f"{gold_prefix}/training_{stamp}.csv"
        rejected_name = ""
        _put_object(client, namespace, bucket, training_name, _csv_bytes(result.pop("training")), "text/csv; charset=utf-8")
        if result.get("rejected"):
            rejected_name = f"{rejected_prefix}/training_{stamp}.jsonl"
            _put_object(client, namespace, bucket, rejected_name, _jsonl_bytes(result["rejected"]), "application/jsonl")
        result.update({
            "source_export": export_name,
            "evidence_objects": evidence_names,
            "body_objects": body_names,
            "training_object": training_name,
            "rejected_object": rejected_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
        LOGGER.info("Training CSV written: %s (%s records)", training_name, result["training_records"])
        return response.Response(ctx, response_data=json.dumps(result, ensure_ascii=False), headers={"Content-Type": "application/json"})
    except Exception as exc:  # noqa: BLE001 - persist a rejected artifact for operational diagnosis
        LOGGER.exception("Label Studio training build failed")
        rejected_name = _write_error(client, namespace, bucket, rejected_prefix, stamp, exc)
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "message": str(exc), "rejected_object": rejected_name}, ensure_ascii=False),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )
