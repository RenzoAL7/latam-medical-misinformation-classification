"""Extract auditable claims from the Silver/Body backlog in bounded batches.

This Function is deliberately a candidate-generation step.  It does not decide
whether a claim is true or false.  Rows whose body extraction failed, is too
short, or is missing are excluded before the LLM call.  Several valid rows are
sent in one Gemini request to reduce API calls and rate-limit pressure.  The
structured responses are written to a deterministic CSV under
``silver/claims/``. A previous failed attempt remains retryable, while a
successful LLM result is terminal for that ``record_id``.
"""

from __future__ import annotations

import csv
import base64
import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import oci
from fdk import response


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

PROMPT_VERSION = "claim-extraction-v3-english-pubmed"
QUERY_PROMPT_VERSION = "pubmed-query-v1"
DEFAULT_MODEL_ID = "google.gemini-2.5-flash"
DEFAULT_GOOGLE_MODEL_ID = "gemini-3.5-flash-lite"
CLAIM_TYPES = {"tratamiento", "prevención", "diagnóstico", "riesgo", "causa", "síntoma", "otro"}
CLAIMS_SCHEMA_VERSION = "claims.v1"
CLAIMS_MANIFEST_SCHEMA_VERSION = "claims.manifest.v1"

BRONZE_FIELDS = [
    "record_id",
    "source_name",
    "canonical_url",
    "published_at",
    "title",
    "subtitle_or_bajada",
    "topic",
    "selection_status",
    "retrieved_at",
    "country",
]

BODY_FIELDS = [
    "body",
    "http_status",
    "extraction_method",
    "extraction_status",
    "body_char_count",
    "body_word_count",
    "extracted_at",
    "source_bronze_object",
    "extraction_run_id",
    "extraction_error",
]

CLAIM_FIELDS = [
    "claim_text",
    "claim_text_en",
    "pubmed_query_en",
    "query_status",
    "query_prompt_version",
    "query_enriched_at",
    "query_enrichment_run_id",
    "is_medical",
    "is_claim_eligible",
    "claim_type",
    "llm_reason",
    "needs_human_review",
    "llm_status",
    "llm_error",
    "llm_raw_json",
    "model_id",
    "llm_key_slot",
    "prompt_version",
    "llm_processed_at",
    "source_silver_object",
    "claim_run_id",
]

OUTPUT_FIELDS = BRONZE_FIELDS + BODY_FIELDS + CLAIM_FIELDS


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _object_storage_client() -> Any:
    signer = oci.auth.signers.get_resource_principals_signer()
    return oci.object_storage.ObjectStorageClient(config={}, signer=signer)


def _generative_ai_client(region: str, request_timeout: float) -> Any:
    signer = oci.auth.signers.get_resource_principals_signer()
    endpoint = os.getenv(
        "GENAI_ENDPOINT",
        f"https://inference.generativeai.{region}.oci.oraclecloud.com",
    ).strip()
    return oci.generative_ai_inference.GenerativeAiInferenceClient(
        config={"region": region},
        signer=signer,
        service_endpoint=endpoint,
        timeout=(5.0, request_timeout),
        retry_strategy=oci.retry.NoneRetryStrategy(),
    )


def _vault_secret_value(secret_id: str) -> str:
    """Read and decode a current OCI Vault secret using the Function identity."""

    signer = oci.auth.signers.get_resource_principals_signer()
    client = oci.secrets.SecretsClient(config={}, signer=signer)
    bundle = client.get_secret_bundle(secret_id=secret_id, stage="CURRENT").data
    content = bundle.secret_bundle_content.content
    if not content:
        raise RuntimeError("The configured Vault secret has no content")
    if isinstance(content, bytes):
        encoded = content.decode("ascii")
    else:
        encoded = str(content)
    try:
        value = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        # Keep local/dev compatibility with a plain-text secret if the service
        # returns it without Base64 wrapping.
        value = encoded
    value = value.strip()
    if not value:
        raise RuntimeError("The configured Vault secret is empty")
    return value


def _secret_ids(list_name: str, legacy_name: str) -> list[str]:
    """Return deduplicated Vault secret OCIDs from a CSV config value."""

    configured = os.getenv(list_name, "").strip()
    if configured:
        values = [value.strip() for value in configured.split(",") if value.strip()]
    else:
        legacy = os.getenv(legacy_name, "").strip()
        values = [legacy] if legacy else []
    return list(dict.fromkeys(values))


def _google_key_switchable(error: Exception) -> bool:
    """Whether another project key can plausibly recover this Gemini call."""

    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "http 401",
            "http 403",
            "http 429",
            "quota",
            "rate limit",
            "api key",
            "permission",
            "network error",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "service unavailable",
        )
    )


def _call_with_google_key_failover(
    api_keys: list[str],
    start_index: int,
    operation: Any,
) -> tuple[Any, int]:
    """Run one request and fail over to the next project key on quota/auth errors."""

    if not api_keys:
        raise RuntimeError("No usable Google Gemini API key was loaded")
    last_error: Exception | None = None
    for offset in range(len(api_keys)):
        slot = (start_index + offset) % len(api_keys)
        try:
            return operation(api_keys[slot]), slot
        except Exception as exc:  # noqa: BLE001 - try another configured project
            last_error = exc
            if offset == len(api_keys) - 1 or not _google_key_switchable(exc):
                raise
            LOGGER.warning("Gemini key slot %s failed; trying the next slot", slot + 1)
    raise RuntimeError(f"All Gemini key slots failed: {last_error}") from last_error


def _list_csv_objects(client: Any, namespace: str, bucket: str, prefix: str) -> list[str]:
    list_prefix = f"{prefix.strip('/')}/" if prefix.strip("/") else ""
    names: list[str] = []
    start: str | None = None
    while True:
        result = client.list_objects(
            namespace_name=namespace,
            bucket_name=bucket,
            prefix=list_prefix,
            start=start,
            fields="name",
            limit=1000,
        )
        for item in result.data.objects:
            name = str(item.name)
            if name.endswith(".csv"):
                names.append(name)
        start = result.data.next_start_with
        if not start:
            break
    return sorted(set(names))


def _read_csv_object(client: Any, namespace: str, bucket: str, object_name: str) -> list[dict[str, str]]:
    result = client.get_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
    )
    content = result.data.content
    if hasattr(content, "read"):
        content = content.read()
    if not isinstance(content, (bytes, bytearray)):
        raise RuntimeError(f"Object Storage returned non-byte content for {object_name}")
    reader = csv.DictReader(io.StringIO(bytes(content).decode("utf-8-sig", errors="replace")))
    return [
        {str(key): str(value or "").strip() for key, value in row.items() if key}
        for row in reader
        if row
    ]


def _stable_token(object_name: str) -> str:
    stem = object_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:72] or "source"
    digest = hashlib.sha256(object_name.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}"


def _claims_output_name(prefix: str, source_object: str) -> str:
    return f"{prefix.strip('/')}/claims_current_{_stable_token(source_object)}.csv"


def _claim_row_is_terminal(row: dict[str, str]) -> bool:
    """Only a completed LLM response closes a claims row."""

    return row.get("llm_status", "").strip().upper() == "OK"


def _existing_claim_rows(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
) -> dict[str, dict[str, str]]:
    rows_by_id: dict[str, dict[str, str]] = {}
    for object_name in _list_csv_objects(client, namespace, bucket, prefix):
        for row in _read_csv_object(client, namespace, bucket, object_name):
            record_id = row.get("record_id", "").strip()
            if record_id:
                previous = rows_by_id.get(record_id)
                if previous is None or (
                    _claim_row_is_terminal(row) and not _claim_row_is_terminal(previous)
                ) or (_claim_row_is_terminal(row) == _claim_row_is_terminal(previous)):
                    rows_by_id[record_id] = row
    return rows_by_id


def _event_object_name(data: io.BytesIO | None, bucket: str, prefix: str) -> str:
    """Read and validate the single Object Storage object in an OCI Event."""

    if data is None:
        raise RuntimeError("Object Storage event payload is required")
    raw = data.read()
    if not raw:
        raise RuntimeError("Object Storage event payload is empty")
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Object Storage event payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Object Storage event payload must be an object")

    event_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    details = event_data.get("additionalDetails", {})
    if not isinstance(details, dict):
        details = {}
    event_bucket = str(
        details.get("bucketName") or event_data.get("bucketName") or payload.get("bucketName") or ""
    ).strip()
    if event_bucket and event_bucket != bucket:
        raise RuntimeError(f"Event bucket {event_bucket!r} does not match configured bucket {bucket!r}")

    expected_prefix = f"{prefix.strip('/')}/"
    for candidate in (
        event_data.get("resourceName"),
        details.get("objectName"),
        details.get("resourceName"),
        event_data.get("objectName"),
        payload.get("source_object"),
        payload.get("object_name"),
    ):
        object_name = unquote(str(candidate or "")).lstrip("/")
        if object_name.startswith(expected_prefix) and object_name.endswith(".csv"):
            return object_name
    raise RuntimeError(f"Event does not identify a CSV under {expected_prefix}")


def _event_object_name_or_none(
    data: io.BytesIO | None, bucket: str, prefix: str
) -> str | None:
    """Return an event object, or ``None`` for an explicit drain invocation."""

    if data is None:
        return None
    raw = data.read()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    stripped = raw.strip()
    if not stripped or stripped in {b"{}", b"null"}:
        return None
    return _event_object_name(io.BytesIO(raw), bucket, prefix)


def _event_claim_rows(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
    claims_prefix: str,
    max_rows: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Select retryable claims only from the Body object in this event."""

    existing = _existing_claim_rows(client, namespace, bucket, claims_prefix)
    source_rows = _read_csv_object(client, namespace, bucket, object_name)
    selected: list[dict[str, str]] = []
    pending_rows = 0
    skipped_no_body = 0
    skipped_extraction_error = 0
    skipped_completed = 0
    seen_ids: set[str] = set()
    for source_row in source_rows:
        record_id = source_row.get("record_id", "").strip()
        body = source_row.get("body", "").strip()
        extraction_status = source_row.get("extraction_status", "").strip().upper()
        if not record_id or not body:
            skipped_no_body += 1
            continue
        if extraction_status != "OK":
            skipped_extraction_error += 1
            continue
        if record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        previous = existing.get(record_id)
        if previous is not None and _claim_row_is_terminal(previous):
            skipped_completed += 1
            continue
        pending_rows += 1
        selected.append(
            {
                field: source_row.get(field, "").strip()
                for field in BRONZE_FIELDS + BODY_FIELDS
            }
            | {"source_silver_object": object_name}
        )

    if max_rows > 0:
        selected = selected[:max_rows]
    return selected, {
        "source_object": object_name,
        "body_objects_scanned": 1,
        "body_rows_seen": len(source_rows),
        "skipped_no_body": skipped_no_body,
        "skipped_extraction_error": skipped_extraction_error,
        "skipped_completed": skipped_completed,
        "pending_rows": pending_rows,
        "selected": len(selected),
        "selected_source_object": object_name,
    }


def _pending_rows(
    client: Any,
    namespace: str,
    bucket: str,
    body_prefix: str,
    claims_prefix: str,
    max_rows: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Select one bounded Body source and count all pending claim rows."""

    existing = _existing_claim_rows(client, namespace, bucket, claims_prefix)
    body_objects = _list_csv_objects(client, namespace, bucket, body_prefix)
    selected: list[dict[str, str]] = []
    pending_ids: set[str] = set()
    selected_source = ""
    stats = Counter(
        {
            "body_objects_scanned": 0,
            "body_rows_seen": 0,
            "skipped_no_body": 0,
            "skipped_extraction_error": 0,
            "skipped_duplicate": 0,
            "skipped_completed": 0,
            "claims_record_ids": len(existing),
        }
    )
    for object_name in body_objects:
        source_pending: list[dict[str, str]] = []
        for source_row in _read_csv_object(client, namespace, bucket, object_name):
            stats["body_rows_seen"] += 1
            record_id = source_row.get("record_id", "").strip()
            body = source_row.get("body", "").strip()
            extraction_status = source_row.get("extraction_status", "").strip().upper()
            if not record_id or not body:
                stats["skipped_no_body"] += 1
                continue
            if extraction_status != "OK":
                stats["skipped_extraction_error"] += 1
                continue
            previous = existing.get(record_id)
            if previous is not None and _claim_row_is_terminal(previous):
                stats["skipped_completed"] += 1
                continue
            if record_id in pending_ids:
                stats["skipped_duplicate"] += 1
                continue
            pending_ids.add(record_id)
            source_pending.append(
                {
                    field: source_row.get(field, "").strip()
                    for field in BRONZE_FIELDS + BODY_FIELDS
                }
                | {"source_silver_object": object_name}
            )

        if source_pending and not selected_source:
            selected_source = object_name
            selected = source_pending[:max_rows] if max_rows > 0 else source_pending

        stats["body_objects_scanned"] += 1

    stats["pending_rows"] = len(pending_ids)
    stats["selected"] = len(selected)
    stats["selected_source_object"] = selected_source
    return selected, dict(stats)


def _build_batch_prompt(rows: list[dict[str, str]], max_body_chars: int) -> str:
    """Build one deterministic prompt for a small group of articles.

    The record ID is included in both the input and the required output so a
    response cannot be silently assigned to the wrong CSV row.  The caller
    still verifies the returned IDs before writing anything.
    """

    articles: list[str] = []
    for index, row in enumerate(rows, start=1):
        body = row.get("body", "")[:max_body_chars]
        record_id = row.get("record_id", "")
        title = row.get("title", "")
        subtitle = row.get("subtitle_or_bajada", "")
        articles.append(
            f"""ARTÍCULO {index}
record_id (copia exactamente): {record_id}
Título: {title}
Subtítulo: {subtitle}
Texto:
<<<
{body}
>>>"""
        )

    return f"""Eres un extractor de afirmaciones médicas para un corpus académico en español.

Analiza todos los artículos que aparecen abajo. Devuelve únicamente un objeto
JSON válido con esta forma exacta:
{{
  "items": [
    {{
      "record_id": "el record_id de entrada",
      "is_medical": true,
      "is_claim_eligible": true,
      "claim_text": "una sola afirmación médica concreta",
      "claim_text_en": "faithful English translation of claim_text",
      "pubmed_query_en": "concise PubMed query in English using Boolean terms",
      "claim_type": "tratamiento|prevención|diagnóstico|riesgo|causa|síntoma|otro",
      "reason": "explicación breve basada en el texto, máximo 160 caracteres",
      "needs_human_review": true
    }}
  ]
}}

Reglas obligatorias:
- Devuelve exactamente un elemento en items por cada artículo de entrada y no agregues otros.
- Conserva cada record_id exactamente; no lo inventes ni lo cambies.
- Mantén el mismo orden de los artículos.
- Extrae la afirmación principal sin inventar datos ni completar información ausente.
- No decidas si la afirmación es verdadera o falsa; esa decisión requiere evidencia y revisión humana.
- is_medical es false si el texto no trata sobre salud, enfermedad, medicina o bienestar.
- is_claim_eligible es false y claim_text debe ser "" si no existe una afirmación médica concreta que pueda verificarse.
- Mantén claim_text en español y en una sola oración.
- Para una afirmación elegible, traduce claim_text fielmente a inglés en claim_text_en sin añadir hechos.
- Para una afirmación elegible, genera pubmed_query_en en inglés con términos biomédicos y operadores AND/OR.
- pubmed_query_en debe buscar solamente la afirmación presentada; no incluy conclusiones ni términos que no estén sustentados.
- Si la afirmación no es elegible, claim_text_en y pubmed_query_en deben ser cadenas vacías.
- Mantén claim_text por debajo de 350 caracteres y reason por debajo de 160 caracteres.
- Mantén claim_text_en por debajo de 500 caracteres y pubmed_query_en por debajo de 600 caracteres.
- needs_human_review debe ser true para cualquier afirmación elegible.

Artículos:
{chr(10).join(articles)}
"""


def _to_plain(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:  # noqa: BLE001
            pass
    # OCI SDK model objects expose ``swagger_types`` and private backing
    # attributes rather than a public ``to_dict`` method in this base image.
    if hasattr(value, "swagger_types"):
        return {
            name: _to_plain(getattr(value, name, None))
            for name in value.swagger_types
        }
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def _find_response_text(value: Any) -> str:
    """Find assistant text across SDK response versions."""

    value = _to_plain(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = _find_response_text(item)
            if text:
                return text
        return ""
    if not isinstance(value, dict):
        return ""
    for key in (
        "text",
        "content",
        "chat_response",
        "choices",
        "candidates",
        "parts",
        "message",
        "response",
        "data",
    ):
        if key in value:
            text = _find_response_text(value[key])
            if text:
                return text
    return ""


def _json_document(text: str) -> Any:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(cleaned[start : end + 1])
    return payload


def _ordered_claim_items(document: Any, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Validate and order a batched LLM response before normalization."""

    if isinstance(document, dict):
        items = document.get("items", document.get("results"))
        if items is None and len(rows) == 1:
            items = [document]
    else:
        items = document
    if not isinstance(items, list) or len(items) != len(rows):
        raise ValueError(
            f"LLM returned {len(items) if isinstance(items, list) else 0} items for {len(rows)} articles"
        )
    if not all(isinstance(item, dict) for item in items):
        raise ValueError("LLM batch contains a non-object item")

    expected_ids = [row.get("record_id", "").strip() for row in rows]
    returned_ids = [str(item.get("record_id", "") or "").strip() for item in items]
    if returned_ids != expected_ids:
        # A model may omit IDs while still respecting the requested order.  In
        # that case order-based matching is safe; mismatched non-empty IDs are
        # rejected to prevent cross-row corruption.
        if any(returned_ids) or len(set(expected_ids)) != len(expected_ids):
            raise ValueError("LLM batch record_id values do not match input order")
    return items


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "sí", "si", "yes"}
    return default


def _normalize_claim(payload: dict[str, Any]) -> dict[str, str]:
    claim_type = str(payload.get("claim_type", "otro")).strip().lower()
    if claim_type not in CLAIM_TYPES:
        claim_type = "otro"
    claim_text = re.sub(r"\s+", " ", str(payload.get("claim_text", "") or "")).strip()
    claim_text_en = re.sub(r"\s+", " ", str(payload.get("claim_text_en", "") or "")).strip()
    pubmed_query_en = re.sub(r"\s+", " ", str(payload.get("pubmed_query_en", "") or "")).strip()
    reason = re.sub(r"\s+", " ", str(payload.get("reason", "") or "")).strip()
    is_medical = _as_bool(payload.get("is_medical"))
    eligible = _as_bool(payload.get("is_claim_eligible")) and bool(claim_text)
    if not is_medical or not eligible:
        claim_text = ""
        claim_text_en = ""
        pubmed_query_en = ""
        eligible = False
        query_status = "SKIPPED_NOT_ELIGIBLE"
    else:
        query_status = "OK" if claim_text_en and pubmed_query_en else "ERROR"
    return {
        "claim_text": claim_text[:2000],
        "claim_text_en": claim_text_en[:1000],
        "pubmed_query_en": pubmed_query_en[:1000],
        "query_status": query_status,
        "query_prompt_version": QUERY_PROMPT_VERSION,
        "query_enriched_at": "",
        "query_enrichment_run_id": "",
        "is_medical": str(is_medical).lower(),
        "is_claim_eligible": str(eligible).lower(),
        "claim_type": claim_type,
        "llm_reason": reason[:2000],
        "needs_human_review": str(_as_bool(payload.get("needs_human_review"), default=eligible)).lower(),
    }


def _chat_request(model_id: str, prompt: str, max_tokens: int, temperature: float, top_p: float) -> Any:
    models = oci.generative_ai_inference.models
    return models.ChatDetails(
        compartment_id=_required_env("GENAI_COMPARTMENT_ID"),
        serving_mode=models.OnDemandServingMode(model_id=model_id),
        chat_request=models.GenericChatRequest(
            api_format="GENERIC",
            messages=[
                models.UserMessage(
                    role="USER",
                    content=[models.TextContent(type="TEXT", text=prompt)],
                )
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            response_format=models.JsonObjectResponseFormat(type="JSON_OBJECT"),
        ),
    )


def _google_gemini_request(
    api_key: str,
    model_id: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    request_timeout: float,
) -> Any:
    """Call the public Gemini API without placing the API key in the payload."""

    base_url = os.getenv(
        "GOOGLE_GEMINI_ENDPOINT",
        "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")
    encoded_model = urllib.parse.quote(model_id, safe="")
    url = f"{base_url}/models/{encoded_model}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "topP": top_p,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as result:
            content = result.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini network error: {exc.reason}") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid JSON") from exc


def _call_llm_batch(
    client: Any,
    rows: list[dict[str, str]],
    model_id: str,
    max_body_chars: int,
    max_tokens: int,
    temperature: float,
    top_p: float,
    retries: int,
    deadline: float,
    provider: str = "oci",
    google_api_key: str = "",
    request_timeout: float = 30.0,
) -> list[tuple[dict[str, str], dict[str, str], str]]:
    prompt = _build_batch_prompt(rows, max_body_chars)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if time.monotonic() >= deadline:
            raise TimeoutError("LLM batch time budget reached")
        try:
            if provider == "google":
                result = _google_gemini_request(
                    google_api_key,
                    model_id,
                    prompt,
                    max_tokens,
                    temperature,
                    top_p,
                    request_timeout,
                )
            else:
                result = client.chat(
                    chat_details=_chat_request(model_id, prompt, max_tokens, temperature, top_p),
                    retry_strategy=oci.retry.NoneRetryStrategy(),
                )
            raw_text = _find_response_text(result)
            if not raw_text:
                raise ValueError(f"{provider} returned an empty response")
            document = _json_document(raw_text)
            items = _ordered_claim_items(document, rows)
            return [
                (
                    row,
                    _normalize_claim(item),
                    json.dumps(item, ensure_ascii=False),
                )
                for row, item in zip(rows, items)
            ]
        except Exception as exc:  # noqa: BLE001 - retry transient service/parse errors once
            last_error = exc
            if (
                attempt < retries
                and _retryable_llm_error(exc)
                and time.monotonic() + 1.0 < deadline
            ):
                time.sleep(1.0)
    raise RuntimeError(f"LLM call failed: {type(last_error).__name__}: {last_error}") from last_error


def _output_row(
    source_row: dict[str, str],
    claim: dict[str, str],
    raw_json: str,
    run_id: str,
    model_id: str,
    status: str,
    key_slot: str = "",
    error: str = "",
) -> dict[str, str]:
    output = {field: source_row.get(field, "") for field in BRONZE_FIELDS + BODY_FIELDS}
    output.update(claim)
    output.update(
        {
            "llm_status": status,
            "llm_error": error[:1000],
            "llm_raw_json": raw_json[:8000],
            "model_id": model_id,
            "llm_key_slot": key_slot,
            "prompt_version": PROMPT_VERSION,
            "llm_processed_at": datetime.now(timezone.utc).isoformat(),
            "source_silver_object": source_row.get("source_silver_object", ""),
            "claim_run_id": run_id,
            "query_enriched_at": datetime.now(timezone.utc).isoformat(),
            "query_enrichment_run_id": run_id,
        }
    )
    return output


def _failed_claim_row(
    source_row: dict[str, str],
    run_id: str,
    model_id: str,
    error: str,
) -> dict[str, str]:
    """Persist a terminal per-run failure instead of silently leaving a body pending."""

    empty_claim = {
        "claim_text": "",
        "claim_text_en": "",
        "pubmed_query_en": "",
        "query_status": "ERROR",
        "query_prompt_version": QUERY_PROMPT_VERSION,
        "query_enriched_at": "",
        "query_enrichment_run_id": "",
        "is_medical": "",
        "is_claim_eligible": "",
        "claim_type": "",
        "llm_reason": "",
        "needs_human_review": "true",
    }
    return _output_row(source_row, empty_claim, "", run_id, model_id, "ERROR", error=error)


def _quota_disabled(error: Exception) -> bool:
    message = str(error).lower()
    return "set to 0" in message or "tokens-per-minute" in message and "429" in message


def _retryable_llm_error(error: Exception) -> bool:
    """Avoid spending a second request on permanent API/configuration errors."""

    message = str(error).lower()
    permanent_markers = (
        "http 400",
        "http 401",
        "http 403",
        "http 404",
        "http 429",
        "quota",
        "rate limit",
        "set to 0",
        "tokens-per-minute",
    )
    permanent_markers += (
        "jsondecodeerror",
        "llm returned",
        "record_id values do not match",
    )
    return not any(marker in message for marker in permanent_markers)


def _write_claims(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
    rows: list[dict[str, str]],
    source_object: str,
) -> str:
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    object_name = _claims_output_name(prefix, source_object)
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig")),
        content_type="text/csv; charset=utf-8",
    )
    return object_name


def _handler_legacy(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        body_prefix = os.getenv("SILVER_BODY_PREFIX", "silver/body").strip("/")
        claims_prefix = os.getenv("SILVER_CLAIMS_PREFIX", "silver/claims").strip("/")
        provider = os.getenv("LLM_PROVIDER", "oci").strip().lower() or "oci"
        if provider not in {"oci", "google"}:
            raise RuntimeError("LLM_PROVIDER must be either 'oci' or 'google'")
        default_model_id = DEFAULT_GOOGLE_MODEL_ID if provider == "google" else DEFAULT_MODEL_ID
        model_id = os.getenv("LLM_MODEL_ID", default_model_id).strip() or default_model_id
        region = os.getenv("LLM_REGION", os.getenv("OCI_REGION", "us-ashburn-1")).strip()
        # A zero max means "process every eligible Body row". Production uses
        # a positive cap so a large backlog advances in repeatable batches.
        max_rows = _env_int("LLM_MAX_ROWS", 25, minimum=0)
        request_batch_size = _env_int("LLM_REQUEST_BATCH_SIZE", 10, minimum=1)
        max_seconds = _env_float("LLM_MAX_SECONDS", 150.0, minimum=1.0)
        time_buffer = _env_float("LLM_TIME_BUFFER", 10.0, minimum=0.0)
        max_body_chars = _env_int("LLM_MAX_BODY_CHARS", 24_000, minimum=500)
        max_tokens = _env_int("LLM_MAX_TOKENS", 3000, minimum=50)
        request_timeout = _env_float("LLM_REQUEST_TIMEOUT", 30.0, minimum=5.0)
        temperature = _env_float("LLM_TEMPERATURE", 0.0, minimum=0.0)
        top_p = _env_float("LLM_TOP_P", 0.9, minimum=0.0)
        retries = _env_int("LLM_MAX_RETRIES", 0, minimum=0)
        if time_buffer >= max_seconds:
            raise RuntimeError("LLM_TIME_BUFFER must be smaller than LLM_MAX_SECONDS")

        storage = _object_storage_client()
        event_object = _event_object_name_or_none(data, bucket, body_prefix)
        if event_object:
            pending, scan_meta = _event_claim_rows(
                storage,
                namespace,
                bucket,
                event_object,
                claims_prefix,
                max_rows,
            )
            source_object = event_object
            invocation_mode = "event"
        else:
            pending, scan_meta = _pending_rows(
                storage,
                namespace,
                bucket,
                body_prefix,
                claims_prefix,
                max_rows,
            )
            source_object = str(scan_meta.get("selected_source_object", ""))
            invocation_mode = "drain"
        deadline = started + max_seconds - time_buffer
        output_rows: list[dict[str, str]] = []
        statuses: Counter[str] = Counter()
        last_llm_error = ""
        consecutive_errors = 0
        google_api_keys: list[str] = []
        google_key_cursor = 0
        llm_requests = 0
        if pending:
            if provider == "google":
                secret_ids = _secret_ids("GOOGLE_GEMINI_SECRET_OCIDS", "GOOGLE_GEMINI_SECRET_OCID")
                if not secret_ids:
                    raise RuntimeError(
                        "GOOGLE_GEMINI_SECRET_OCIDS or GOOGLE_GEMINI_SECRET_OCID is required"
                    )
                for slot, secret_id in enumerate(secret_ids, start=1):
                    try:
                        value = _vault_secret_value(secret_id)
                    except Exception as exc:  # noqa: BLE001 - keep other project keys usable
                        LOGGER.warning("Unable to read Gemini secret slot %s: %s", slot, type(exc).__name__)
                        continue
                    if value and value not in google_api_keys:
                        google_api_keys.append(value)
                if not google_api_keys:
                    raise RuntimeError("No usable Google Gemini Vault secret was found")
                llm = None
            else:
                llm = _generative_ai_client(region, request_timeout)
            for offset in range(0, len(pending), request_batch_size):
                if time.monotonic() >= deadline:
                    for source_row in pending[offset:]:
                        output_rows.append(
                            _failed_claim_row(source_row, run_id, model_id, "LLM_TIME_BUDGET")
                        )
                    statuses["ERROR"] += len(pending) - offset
                    break
                request_rows = pending[offset : offset + request_batch_size]
                try:
                    def invoke_batch(key: str) -> list[tuple[dict[str, str], dict[str, str], str]]:
                        return _call_llm_batch(
                            llm,
                            request_rows,
                            model_id,
                            max_body_chars,
                            max_tokens,
                            temperature,
                            top_p,
                            retries,
                            deadline,
                            provider=provider,
                            google_api_key=key,
                            request_timeout=request_timeout,
                        )

                    if provider == "google":
                        batch_results, used_slot = _call_with_google_key_failover(
                            google_api_keys,
                            google_key_cursor,
                            invoke_batch,
                        )
                        google_key_cursor = (used_slot + 1) % len(google_api_keys)
                        key_slot = str(used_slot + 1)
                    else:
                        batch_results = invoke_batch("")
                        key_slot = "oci"
                    llm_requests += 1
                    for source_row, claim, raw_json in batch_results:
                        output_rows.append(
                            _output_row(source_row, claim, raw_json, run_id, model_id, "OK", key_slot)
                        )
                    statuses["OK"] += len(batch_results)
                    consecutive_errors = 0
                except TimeoutError:
                    for source_row in pending[offset:]:
                        output_rows.append(
                            _failed_claim_row(source_row, run_id, model_id, "LLM_TIME_BUDGET")
                        )
                    statuses["ERROR"] += len(pending) - offset
                    break
                except Exception as exc:  # noqa: BLE001 - preserve terminal per-run errors
                    LOGGER.warning(
                        "Claim extraction failed for %s: %s",
                        ",".join(row.get("record_id", "") for row in request_rows),
                        exc,
                    )
                    error = f"{type(exc).__name__}: {exc}"[:2000]
                    for source_row in request_rows:
                        output_rows.append(_failed_claim_row(source_row, run_id, model_id, error))
                    statuses["ERROR"] += len(request_rows)
                    last_llm_error = error
                    consecutive_errors += 1
                    # A disabled quota cannot be fixed in this invocation;
                    # preserve every remaining row with an explicit outcome.
                    if _quota_disabled(exc) or consecutive_errors >= 3:
                        for source_row in pending[offset + len(request_rows) :]:
                            output_rows.append(
                                _failed_claim_row(source_row, run_id, model_id, "LLM_NOT_ATTEMPTED")
                            )
                        statuses["ERROR"] += len(pending) - offset - len(request_rows)
                        break

        object_name = ""
        if output_rows and source_object:
            canonical_name = _claims_output_name(claims_prefix, source_object)
            existing_rows = (
                _read_csv_object(storage, namespace, bucket, canonical_name)
                if canonical_name in _list_csv_objects(storage, namespace, bucket, claims_prefix)
                else []
            )
            merged = {
                row.get("record_id", ""): row
                for row in existing_rows
                if row.get("record_id", "")
            }
            merged.update({row["record_id"]: row for row in output_rows})
            object_name = _write_claims(
                storage,
                namespace,
                bucket,
                claims_prefix,
                sorted(merged.values(), key=lambda row: row.get("record_id", "")),
                source_object,
            )
        result_status = "ok"
        if statuses.get("ERROR", 0) and not output_rows:
            result_status = "error"
        elif statuses.get("ERROR", 0):
            result_status = "partial"
        terminal_written = sum(1 for row in output_rows if _claim_row_is_terminal(row))
        pending_total = int(scan_meta.get("pending_rows", 0))
        result = {
            "status": result_status,
            "mode": "claims",
            "invocation_mode": invocation_mode,
            "run_id": run_id,
            "bucket": bucket,
            "object_name": object_name,
            "source_object": source_object,
            "row_count": str(len(output_rows)),
            "ok_rows": str(statuses.get("OK", 0)),
            "error_rows": str(statuses.get("ERROR", 0)),
            "rows_left_for_next_run": str(max(0, pending_total - terminal_written)),
            "llm_error": last_llm_error,
            "model_id": model_id,
            "llm_provider": provider,
            "llm_requests": str(llm_requests),
            "llm_key_slots_configured": str(len(google_api_keys)) if provider == "google" else "oci",
            "elapsed_seconds": f"{time.monotonic() - started:.2f}",
            "max_rows": "all" if max_rows == 0 else str(max_rows),
            "request_batch_size": str(request_batch_size),
            "scan": scan_meta,
        }
        LOGGER.info("Silver claims batch written: %s (%s rows)", object_name or "none", len(output_rows))
        return response.Response(
            ctx,
            response_data=json.dumps(result, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - expose a useful invocation error
        LOGGER.exception("extract-claims failed")
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "run_id": run_id, "message": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )


# ---------------------------------------------------------------------------
# JSONL v1 multi-claim contract
# ---------------------------------------------------------------------------


def _claims_v1_object_names(prefix: str, run_id: str) -> tuple[str, str]:
    """Return immutable Claims data and manifest names for one invocation."""

    base = f"{prefix.strip('/')}/run_id={run_id}"
    return f"{base}/claims.jsonl", f"{base}/manifest.json"


def _write_jsonl_object(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
    records: list[dict[str, Any]],
) -> None:
    payload = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(payload.encode("utf-8")),
        content_type="application/x-ndjson; charset=utf-8",
    )


def _write_json_object(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
    document: dict[str, Any],
) -> None:
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")),
        content_type="application/json; charset=utf-8",
    )


def _multiclaim_prompt(rows: list[dict[str, str]], max_body_chars: int) -> str:
    """Ask for every distinct, verifiable medical claim in each article.

    The article remains the JSONL line.  Claims belong in an array so no
    assertion is silently overwritten when an article contains more than one.
    """

    articles: list[str] = []
    for index, row in enumerate(rows, start=1):
        articles.append(
            f"""ARTÍCULO {index}
record_id (copia exactamente): {row.get('record_id', '')}
Título: {row.get('title', '')}
Subtítulo: {row.get('subtitle_or_bajada', '')}
Texto:
<<<
{row.get('body', '')[:max_body_chars]}
>>>"""
        )

    return f"""Eres un extractor de afirmaciones médicas para un corpus académico en español.

Analiza todos los artículos. Devuelve únicamente JSON válido con esta forma:
{{
  "items": [
    {{
      "record_id": "el record_id de entrada",
      "is_medical": true,
      "reason": "explicación breve, máximo 160 caracteres",
      "claims": [
        {{
          "claim_text": "una afirmación médica concreta en español",
          "claim_text_en": "faithful English translation",
          "pubmed_query_en": "concise PubMed query in English using Boolean terms",
          "claim_type": "tratamiento|prevención|diagnóstico|riesgo|causa|síntoma|otro",
          "is_claim_eligible": true,
          "needs_human_review": true
        }}
      ]
    }}
  ]
}}

Reglas obligatorias:
- Devuelve exactamente un item por artículo de entrada, mantiene el orden y conserva cada record_id.
- claims debe incluir TODAS las afirmaciones médicas distintas, concretas y verificables que aparezcan en el artículo; no elijas solo la principal ni impongas un máximo.
- Si no hay afirmaciones verificables, devuelve "claims": [].
- No inventes datos, no completes información ausente y no decidas si una afirmación es verdadera o falsa.
- Un claim debe ser una sola oración y no debe fusionar dos afirmaciones independientes.
- Para cada claim elegible, claim_text_en es una traducción fiel y pubmed_query_en solo busca esa afirmación; ambos van en inglés.
- Si no hay claim elegible, no agregues objetos vacíos dentro de claims.
- claim_text máximo 350 caracteres; claim_text_en 500; pubmed_query_en 600; reason 160.

Artículos:
{chr(10).join(articles)}
"""


def _ordered_multiclaim_items(document: Any, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Validate the one-article-per-item envelope before expanding claims."""

    items = _ordered_claim_items(document, rows)
    for item in items:
        claims = item.get("claims", [])
        if not isinstance(claims, list) or not all(isinstance(claim, dict) for claim in claims):
            raise ValueError("LLM item must contain a claims array of objects")
    return items


def _should_split_multiclaim_batch_error(error: Exception) -> bool:
    """Return whether a malformed multi-item response can be isolated safely.

    A response-format mistake for one item must not turn every article in a
    successful request into an ``ERROR`` row.  The singleton retry preserves
    the strict schema validation above while isolating the offending article;
    quota, transport, and configuration failures remain batch-level errors.
    """

    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "claims array",
            "jsondecodeerror",
            "invalid json",
            "llm returned",
            "batch contains a non-object item",
            "record_id values do not match",
        )
    )


def _claim_identifier(record_id: str, index: int, claim_text: str) -> str:
    digest = hashlib.sha256(f"{record_id}\n{index}\n{claim_text}".encode("utf-8")).hexdigest()[:10]
    return f"{record_id}_c{index:02d}_{digest}"


def _normalize_multiclaim_item(
    payload: dict[str, Any],
    record_id: str,
    max_claims: int,
) -> tuple[list[dict[str, Any]], bool, str, bool]:
    """Normalize every returned claim while retaining query failures for audit."""

    article_is_medical = _as_bool(payload.get("is_medical"), default=bool(payload.get("claims")))
    article_reason = re.sub(r"\s+", " ", str(payload.get("reason", "") or "")).strip()[:2000]
    raw_claims = payload.get("claims", [])
    if not article_is_medical:
        raw_claims = []

    normalized_claims: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, dict):
            continue
        candidate = dict(raw_claim)
        candidate.setdefault("is_medical", article_is_medical)
        candidate.setdefault("is_claim_eligible", True)
        candidate.setdefault("needs_human_review", True)
        normalized = _normalize_claim(candidate)
        claim_text = normalized["claim_text"]
        if not claim_text:
            continue
        duplicate_key = re.sub(r"\s+", " ", claim_text).casefold()
        if duplicate_key in seen_texts:
            continue
        seen_texts.add(duplicate_key)
        index = len(normalized_claims) + 1
        query_status = normalized["query_status"]
        normalized_claims.append(
            {
                "claim_id": _claim_identifier(record_id, index, claim_text),
                "claim_index": index,
                "claim_text": claim_text,
                "claim_text_en": normalized["claim_text_en"],
                "pubmed_query_en": normalized["pubmed_query_en"],
                "query_status": query_status,
                "query_prompt_version": QUERY_PROMPT_VERSION,
                "is_medical": normalized["is_medical"] == "true",
                "is_claim_eligible": normalized["is_claim_eligible"] == "true",
                "claim_type": normalized["claim_type"],
                "needs_human_review": normalized["needs_human_review"] == "true",
                "claim_status": (
                    "PENDING_EVIDENCE"
                    if normalized["is_claim_eligible"] == "true" and query_status == "OK"
                    else "QUERY_ERROR"
                    if normalized["is_claim_eligible"] == "true"
                    else "NOT_ELIGIBLE"
                ),
            }
        )

    truncated = max_claims > 0 and len(normalized_claims) > max_claims
    if truncated:
        normalized_claims = normalized_claims[:max_claims]
    return normalized_claims, truncated, article_reason, article_is_medical


def _call_multiclaim_llm_batch(
    client: Any,
    rows: list[dict[str, str]],
    model_id: str,
    max_body_chars: int,
    max_tokens: int,
    temperature: float,
    top_p: float,
    retries: int,
    deadline: float,
    provider: str,
    google_api_key: str,
    request_timeout: float,
) -> list[tuple[dict[str, str], dict[str, Any], str]]:
    prompt = _multiclaim_prompt(rows, max_body_chars)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if time.monotonic() >= deadline:
            raise TimeoutError("LLM batch time budget reached")
        try:
            if provider == "google":
                result = _google_gemini_request(
                    google_api_key,
                    model_id,
                    prompt,
                    max_tokens,
                    temperature,
                    top_p,
                    request_timeout,
                )
            else:
                result = client.chat(
                    chat_details=_chat_request(model_id, prompt, max_tokens, temperature, top_p),
                    retry_strategy=oci.retry.NoneRetryStrategy(),
                )
            raw_text = _find_response_text(result)
            if not raw_text:
                raise ValueError(f"{provider} returned an empty response")
            document = _json_document(raw_text)
            items = _ordered_multiclaim_items(document, rows)
            return [(row, item, json.dumps(item, ensure_ascii=False)) for row, item in zip(rows, items)]
        except Exception as exc:  # noqa: BLE001 - preserve the final row-level error
            last_error = exc
            if attempt < retries and _retryable_llm_error(exc) and time.monotonic() + 1.0 < deadline:
                time.sleep(1.0)
    raise RuntimeError(f"LLM call failed: {type(last_error).__name__}: {last_error}") from last_error


def _article_metadata(source_row: dict[str, str]) -> dict[str, str]:
    """Keep metadata in Claims but leave the full body in its source object."""

    return {
        field: source_row.get(field, "")
        for field in BRONZE_FIELDS + BODY_FIELDS
        if field not in {"record_id", "body"}
    }


def _claims_v1_record(
    source_row: dict[str, str],
    payload: dict[str, Any],
    raw_json: str,
    run_id: str,
    model_id: str,
    key_slot: str,
    max_claims: int,
) -> dict[str, Any]:
    record_id = source_row.get("record_id", "").strip()
    claims, truncated, reason, is_medical = _normalize_multiclaim_item(payload, record_id, max_claims)
    processed_at = datetime.now(timezone.utc).isoformat()
    body = source_row.get("body", "")
    return {
        "schema_version": CLAIMS_SCHEMA_VERSION,
        "run_id": run_id,
        "record_id": record_id,
        "source_body_object": source_row.get("source_silver_object", ""),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "article": _article_metadata(source_row),
        "llm_status": "OK",
        "llm_error": "",
        "llm_raw_json": raw_json[:16000],
        "model_id": model_id,
        "llm_provider": os.getenv("LLM_PROVIDER", "oci").strip().lower() or "oci",
        "llm_key_slot": key_slot,
        "prompt_version": PROMPT_VERSION,
        "llm_processed_at": processed_at,
        "is_medical": is_medical,
        "llm_reason": reason,
        "claims_count": len(claims),
        "claims_truncated": truncated,
        "claims": claims,
    }


def _failed_claims_v1_record(
    source_row: dict[str, str],
    run_id: str,
    model_id: str,
    error: str,
) -> dict[str, Any]:
    body = source_row.get("body", "")
    return {
        "schema_version": CLAIMS_SCHEMA_VERSION,
        "run_id": run_id,
        "record_id": source_row.get("record_id", "").strip(),
        "source_body_object": source_row.get("source_silver_object", ""),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "article": _article_metadata(source_row),
        "llm_status": "ERROR",
        "llm_error": error[:2000],
        "llm_raw_json": "",
        "model_id": model_id,
        "llm_provider": os.getenv("LLM_PROVIDER", "oci").strip().lower() or "oci",
        "llm_key_slot": "",
        "prompt_version": PROMPT_VERSION,
        "llm_processed_at": datetime.now(timezone.utc).isoformat(),
        "is_medical": None,
        "llm_reason": "",
        "claims_count": 0,
        "claims_truncated": False,
        "claims": [],
    }


def _v1_body_rows(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Select every valid article from the triggering Body object, once."""

    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    stats: Counter[str] = Counter()
    for source_row in _read_csv_object(client, namespace, bucket, object_name):
        stats["body_rows_seen"] += 1
        record_id = source_row.get("record_id", "").strip()
        if not record_id or not source_row.get("body", "").strip():
            stats["skipped_no_body"] += 1
            continue
        if source_row.get("extraction_status", "").strip().upper() != "OK":
            stats["skipped_extraction_error"] += 1
            continue
        if record_id in seen:
            stats["skipped_duplicate"] += 1
            continue
        seen.add(record_id)
        selected.append(
            {
                field: source_row.get(field, "").strip()
                for field in BRONZE_FIELDS + BODY_FIELDS
            }
            | {"source_silver_object": object_name}
        )
    stats["selected"] = len(selected)
    return selected, dict(stats)


def _claims_input_metrics(rows: list[dict[str, str]], max_body_chars: int) -> dict[str, int]:
    """Describe the text actually sent to the extraction model.

    ``LLM_MAX_BODY_CHARS`` is a safety limit, not a sampling mechanism.  These
    counters make any omitted tail text visible in the immutable run manifest
    so that future budget changes can be based on measured data rather than an
    assumed token cost.
    """

    available = [len(row.get("body", "")) for row in rows]
    sent = [min(length, max_body_chars) for length in available]
    return {
        "articles": len(rows),
        "body_chars_available": sum(available),
        "body_chars_sent": sum(sent),
        "body_chars_omitted_by_limit": sum(length - sent_length for length, sent_length in zip(available, sent)),
        "articles_truncated_by_body_limit": sum(length > max_body_chars for length in available),
    }


def _handler_v1(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    """Extract all claims from one Body event and write immutable JSONL output."""

    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        body_prefix = os.getenv("SILVER_BODY_PREFIX", "silver/body").strip("/")
        claims_prefix = os.getenv("SILVER_CLAIMS_PREFIX", "silver/claims").strip("/")
        provider = os.getenv("LLM_PROVIDER", "oci").strip().lower() or "oci"
        if provider not in {"oci", "google"}:
            raise RuntimeError("LLM_PROVIDER must be either 'oci' or 'google'")
        configured_max_rows = _env_int("LLM_MAX_ROWS", 0, minimum=0)
        if configured_max_rows:
            raise RuntimeError("LLM_MAX_ROWS must be 0 in claims.v1; one event must process every valid article")
        max_claims = _env_int("MAX_CLAIMS_PER_ARTICLE", 0, minimum=0)
        default_model_id = DEFAULT_GOOGLE_MODEL_ID if provider == "google" else DEFAULT_MODEL_ID
        model_id = os.getenv("LLM_MODEL_ID", default_model_id).strip() or default_model_id
        region = os.getenv("LLM_REGION", os.getenv("OCI_REGION", "us-ashburn-1")).strip()
        request_batch_size = _env_int("LLM_REQUEST_BATCH_SIZE", 5, minimum=1)
        max_seconds = _env_float("LLM_MAX_SECONDS", 270.0, minimum=30.0)
        time_buffer = _env_float("LLM_TIME_BUFFER", 20.0, minimum=1.0)
        max_body_chars = _env_int("LLM_MAX_BODY_CHARS", 24_000, minimum=500)
        max_tokens = _env_int("LLM_MAX_TOKENS", 8_000, minimum=500)
        request_timeout = _env_float("LLM_REQUEST_TIMEOUT", 45.0, minimum=5.0)
        temperature = _env_float("LLM_TEMPERATURE", 0.0, minimum=0.0)
        top_p = _env_float("LLM_TOP_P", 0.9, minimum=0.0)
        retries = _env_int("LLM_MAX_RETRIES", 0, minimum=0)
        if time_buffer >= max_seconds:
            raise RuntimeError("LLM_TIME_BUFFER must be smaller than LLM_MAX_SECONDS")

        storage = _object_storage_client()
        source_object = _event_object_name(data, bucket, body_prefix)
        pending, scan = _v1_body_rows(storage, namespace, bucket, source_object)
        deadline = started + max_seconds - time_buffer
        records: list[dict[str, Any]] = []
        statuses: Counter[str] = Counter()
        last_error = ""
        llm_requests = 0
        split_recovery_batches = 0
        split_recovery_articles = 0
        google_api_keys: list[str] = []
        google_key_cursor = 0

        if pending:
            if provider == "google":
                secret_ids = _secret_ids("GOOGLE_GEMINI_SECRET_OCIDS", "GOOGLE_GEMINI_SECRET_OCID")
                if not secret_ids:
                    raise RuntimeError("GOOGLE_GEMINI_SECRET_OCIDS or GOOGLE_GEMINI_SECRET_OCID is required")
                for secret_id in secret_ids:
                    try:
                        value = _vault_secret_value(secret_id)
                    except Exception as exc:  # noqa: BLE001 - retain other usable keys
                        LOGGER.warning("Unable to read Gemini claim secret: %s", type(exc).__name__)
                        continue
                    if value and value not in google_api_keys:
                        google_api_keys.append(value)
                if not google_api_keys:
                    raise RuntimeError("No usable Google Gemini Vault secret was found")
                llm = None
            else:
                llm = _generative_ai_client(region, request_timeout)

            def _run_request(
                rows: list[dict[str, str]],
            ) -> tuple[list[tuple[dict[str, str], dict[str, Any], str]], str]:
                """Execute one logical request and retain the key slot used."""

                nonlocal google_key_cursor

                def invoke_batch(key: str) -> list[tuple[dict[str, str], dict[str, Any], str]]:
                    return _call_multiclaim_llm_batch(
                        llm,
                        rows,
                        model_id,
                        max_body_chars,
                        max_tokens,
                        temperature,
                        top_p,
                        retries,
                        deadline,
                        provider,
                        key,
                        request_timeout,
                    )

                if provider == "google":
                    batch_results, used_slot = _call_with_google_key_failover(
                        google_api_keys, google_key_cursor, invoke_batch
                    )
                    google_key_cursor = (used_slot + 1) % len(google_api_keys)
                    return batch_results, str(used_slot + 1)
                return invoke_batch(""), "oci"

            def _append_successful_records(
                batch_results: list[tuple[dict[str, str], dict[str, Any], str]],
                key_slot: str,
            ) -> None:
                for source_row, payload, raw_json in batch_results:
                    records.append(
                        _claims_v1_record(
                            source_row, payload, raw_json, run_id, model_id, key_slot, max_claims
                        )
                    )
                    statuses["OK"] += 1

            for offset in range(0, len(pending), request_batch_size):
                request_rows = pending[offset : offset + request_batch_size]
                if time.monotonic() >= deadline:
                    for source_row in pending[offset:]:
                        records.append(_failed_claims_v1_record(source_row, run_id, model_id, "LLM_TIME_BUDGET"))
                        statuses["ERROR"] += 1
                    break
                try:
                    llm_requests += 1
                    batch_results, key_slot = _run_request(request_rows)
                    _append_successful_records(batch_results, key_slot)
                except Exception as exc:  # noqa: BLE001 - all input rows receive an auditable outcome
                    if len(request_rows) > 1 and _should_split_multiclaim_batch_error(exc):
                        split_recovery_batches += 1
                        LOGGER.warning(
                            "Malformed multi-claim response; retrying %s articles individually: %s",
                            len(request_rows),
                            type(exc).__name__,
                        )
                        for source_row in request_rows:
                            try:
                                llm_requests += 1
                                batch_results, key_slot = _run_request([source_row])
                                _append_successful_records(batch_results, key_slot)
                                split_recovery_articles += 1
                            except Exception as recovery_exc:  # noqa: BLE001 - retain the isolated failure
                                error = f"{type(recovery_exc).__name__}: {recovery_exc}"[:2000]
                                last_error = error
                                LOGGER.warning(
                                    "Multi-claim singleton recovery failed for %s: %s",
                                    source_row.get("record_id", ""),
                                    error,
                                )
                                records.append(_failed_claims_v1_record(source_row, run_id, model_id, error))
                                statuses["ERROR"] += 1
                        continue
                    error = f"{type(exc).__name__}: {exc}"[:2000]
                    last_error = error
                    LOGGER.warning("Multi-claim extraction failed for batch: %s", error)
                    for source_row in request_rows:
                        records.append(_failed_claims_v1_record(source_row, run_id, model_id, error))
                        statuses["ERROR"] += 1
                    if _quota_disabled(exc):
                        for source_row in pending[offset + len(request_rows) :]:
                            records.append(_failed_claims_v1_record(source_row, run_id, model_id, "LLM_NOT_ATTEMPTED"))
                            statuses["ERROR"] += 1
                        break

        claims_object, manifest_object = _claims_v1_object_names(claims_prefix, run_id)
        _write_jsonl_object(storage, namespace, bucket, claims_object, records)
        article_statuses = Counter(str(record.get("llm_status", "")) for record in records)
        claims_total = sum(int(record.get("claims_count", 0) or 0) for record in records)
        truncated_articles = sum(bool(record.get("claims_truncated")) for record in records)
        manifest_status = "COMPLETE"
        if article_statuses.get("ERROR"):
            manifest_status = "FAILED" if article_statuses.get("OK", 0) == 0 else "PARTIAL"
        manifest = {
            "schema_version": CLAIMS_MANIFEST_SCHEMA_VERSION,
            "stage": "claims",
            "run_id": run_id,
            "status": manifest_status,
            "source_object": source_object,
            "claims_object": claims_object,
            "started_at": now.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "articles_processed": len(records),
            "articles_with_claims": sum(int(record.get("claims_count", 0) or 0) > 0 for record in records),
            "articles_without_claims": sum(int(record.get("claims_count", 0) or 0) == 0 for record in records),
            "claims_total": claims_total,
            "articles_with_truncated_claims": truncated_articles,
            "llm_status_counts": dict(article_statuses),
            "recovery": {
                "malformed_batches_split": split_recovery_batches,
                "articles_retried_individually": split_recovery_articles,
            },
            "scan": scan,
            "configuration": {
                "llm_provider": provider,
                "model_id": model_id,
                "request_batch_size": request_batch_size,
                "max_body_chars": max_body_chars,
                "max_claims_per_article": max_claims,
                "max_rows": "all",
            },
            "input": _claims_input_metrics(pending, max_body_chars),
        }
        _write_json_object(storage, namespace, bucket, manifest_object, manifest)
        result = {
            "status": manifest_status.lower(),
            "mode": "claims.v1",
            "run_id": run_id,
            "source_object": source_object,
            "object_name": claims_object,
            "manifest_object": manifest_object,
            "articles_processed": len(records),
            "claims_total": claims_total,
            "llm_requests": llm_requests,
            "llm_error": last_error,
            "elapsed_seconds": f"{time.monotonic() - started:.2f}",
        }
        LOGGER.info("Claims JSONL written: %s (%s articles, %s claims)", claims_object, len(records), claims_total)
        return response.Response(
            ctx,
            response_data=json.dumps(result, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - make configuration failure visible to OCI Events
        LOGGER.exception("extract-claims v1 failed")
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "run_id": run_id, "message": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )


def handler(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    """Dispatch explicitly between the retired CSV contract and claims.v1."""

    schema = os.getenv("PIPELINE_SCHEMA", "v1").strip().lower()
    if schema in {"v1", "claims.v1", "jsonl"}:
        return _handler_v1(ctx, data)
    if schema in {"legacy", "csv"}:
        return _handler_legacy(ctx, data)
    raise RuntimeError("PIPELINE_SCHEMA must be v1/jsonl or legacy/csv")
