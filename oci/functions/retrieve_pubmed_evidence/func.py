"""Retrieve PubMed evidence from the Claims backlog in bounded batches.

The function is deliberately an evidence-retrieval step. It does not decide
whether a news claim is true or false. It drains one bounded source batch,
queries PubMed with ESearch, ranks the candidates with OCI Cohere Embed 4,
translates all retained abstract excerpts to Spanish, and writes accepted and
rejected rows to deterministic output objects. A transparent Spanish TF-IDF
score is kept as a diagnostic and fallback.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

import oci
from fdk import response


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

PUBMED_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
COSINE_METHOD = "tfidf_v1_es"
EMBEDDING_METHOD = "oci_cohere_embed_v4"
EVIDENCE_SCHEMA_VERSION = "evidence.v1"
EVIDENCE_MANIFEST_SCHEMA_VERSION = "evidence.manifest.v1"
RETRIEVAL_JOB_SCHEMA_VERSION = "evidence.retrieval.job.v1"
RETRIEVAL_CONTINUATION_STAGE = "retrieval_continuation"

OUTPUT_FIELDS = [
    "record_id",
    "claim_id",
    "claim_index",
    "llm_status",
    "source_body_object",
    "country",
    "source_name",
    "canonical_url",
    "published_at",
    "title",
    "subtitle_or_bajada",
    "topic",
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
    "claim_text",
    "claim_text_en",
    "claim_type",
    "pubmed_query_en",
    "evidence_status",
    "evidence_error",
    "pubmed_result_count",
    "best_pmid",
    "best_embedding_similarity",
    "best_tfidf_similarity",
    "pubmed_results_json",
    "source_claims_object",
    "claim_run_id",
    "evidence_run_id",
    "cosine_method",
    "ranking_method",
    "embedding_model",
    "embedding_status",
    "embedding_query_field",
    "translation_status",
    "translation_error",
    "translation_model",
    "translation_key_slot",
    "translation_prompt_version",
    "translation_run_id",
    "retrieved_at",
]
REJECTED_FIELDS = OUTPUT_FIELDS + ["rejection_reason"]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}")
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


def _generative_ai_client(region: str, timeout: float) -> Any:
    """Create an OCI Generative AI Inference client with the Function identity."""

    signer = oci.auth.signers.get_resource_principals_signer()
    endpoint = os.getenv(
        "GENAI_INFERENCE_ENDPOINT",
        f"https://inference.generativeai.{region}.oci.oraclecloud.com",
    ).strip()
    return oci.generative_ai_inference.GenerativeAiInferenceClient(
        config={"region": region},
        signer=signer,
        service_endpoint=endpoint,
        retry_strategy=oci.retry.NoneRetryStrategy(),
        timeout=(5, max(5, int(timeout))),
    )


def _plain_value(value: Any) -> Any:
    """Convert OCI SDK model objects to JSON-like values defensively."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain_value(to_dict())
        except Exception:  # noqa: BLE001 - keep response parsing best effort
            pass
    if hasattr(value, "__dict__"):
        return {
            str(key): _plain_value(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def _embedding_values(value: Any) -> list[float]:
    """Read a float vector from the different OCI SDK response shapes."""

    value = _plain_value(value)
    if isinstance(value, list) and all(isinstance(item, (int, float)) for item in value):
        return [float(item) for item in value]
    if isinstance(value, dict):
        for key in ("values", "embedding", "float", "float_embedding"):
            if key in value:
                vector = _embedding_values(value[key])
                if vector:
                    return vector
    return []


def _embed_texts(
    client: Any,
    texts: list[str],
    compartment_id: str,
    model_id: str,
    input_type: str,
    output_dimensions: int,
    timeout: float,
) -> list[list[float]]:
    """Embed a batch with OCI Cohere Embed 4 and return one vector per input."""

    if not texts:
        return []
    models = oci.generative_ai_inference.models
    serving_mode = models.OnDemandServingMode(model_id=model_id)
    details_kwargs: dict[str, Any] = {
        "compartment_id": compartment_id,
        "serving_mode": serving_mode,
        "inputs": texts,
        "input_type": input_type,
        "output_dimensions": output_dimensions,
        "embedding_types": ["float"],
        "truncate": "END",
    }
    try:
        details = models.EmbedTextDetails(**details_kwargs)
    except TypeError:
        # Older SDK builds used the Embed 4 `embed_contents` shape instead of
        # accepting `inputs`. Keep the fallback so a base image with a recent
        # but slightly different SDK remains deployable.
        content_model = getattr(models, "EmbedContent", None) or getattr(
            models, "EmbedTextContent", None
        )
        if content_model is None:
            raise
        details_kwargs.pop("inputs", None)
        details_kwargs["embed_contents"] = [content_model(text=text) for text in texts]
        details = models.EmbedTextDetails(**details_kwargs)

    result = client.embed_text(
        embed_text_details=details,
        retry_strategy=oci.retry.NoneRetryStrategy(),
    )
    data_object = getattr(result, "data", result)
    data = _plain_value(data_object)
    # OCI SDK 2.187 exposes Embed 4 vectors through model attributes.  The
    # JSON-compatible conversion intentionally omits the SDK's private
    # backing fields, so inspect the typed object before using the fallback
    # dictionary shape.
    raw_embeddings = getattr(data_object, "embeddings", None)
    if not isinstance(raw_embeddings, list) or not raw_embeddings:
        embeddings_by_type = getattr(data_object, "embeddings_by_type", None)
        if isinstance(embeddings_by_type, dict):
            raw_embeddings = embeddings_by_type.get("float")
    if (not isinstance(raw_embeddings, list) or not raw_embeddings) and isinstance(data, dict):
        raw_embeddings = data.get("embeddings")
        if (not isinstance(raw_embeddings, list) or not raw_embeddings):
            embeddings_by_type = data.get("embeddings_by_type")
            if isinstance(embeddings_by_type, dict):
                raw_embeddings = embeddings_by_type.get("float")
    if not isinstance(raw_embeddings, list):
        raise RuntimeError("OCI Embed 4 returned no embeddings")
    vectors = [_embedding_values(item) for item in raw_embeddings]
    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise RuntimeError(
            f"OCI Embed 4 returned {len(vectors)} valid vectors for {len(texts)} inputs"
        )
    return vectors


def _vector_cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "y", "si", "sí"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    raise RuntimeError(f"{name} must be a boolean")


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


def _claim_identity(row: dict[str, Any]) -> str:
    """Return the identity that is unique at claim level, not article level."""

    return str(row.get("claim_id") or row.get("record_id") or "").strip()


def _row_complete(row: dict[str, str]) -> bool:
    llm_status = row.get("llm_status", "").strip().upper()
    return (
        (not llm_status or llm_status == "OK")
        and row.get("query_status", "").strip().upper() == "OK"
        and bool(row.get("claim_text_en", "").strip())
        and bool(row.get("pubmed_query_en", "").strip())
    )


def _row_priority(row: dict[str, str]) -> tuple[int, str]:
    timestamp = row.get("query_enriched_at", "") or row.get("llm_processed_at", "")
    return (1 if _row_complete(row) else 0, timestamp)


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
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    rows_by_id: dict[str, dict[str, str]] = {}
    rows_seen = 0
    for source_row in _read_csv_object(client, namespace, bucket, object_name):
        rows_seen += 1
        record_id = source_row.get("record_id", "").strip()
        if not record_id:
            continue
        row = dict(source_row)
        row["source_claims_object"] = object_name
        previous = rows_by_id.get(record_id)
        if previous is None or _row_priority(row) >= _row_priority(previous):
            rows_by_id[record_id] = row
    return rows_by_id, {
        "source_object": object_name,
        "claims_objects_scanned": 1,
        "claims_rows_seen": rows_seen,
        "unique_claims": len(rows_by_id),
    }


def _stable_token(object_name: str) -> str:
    stem = object_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:72] or "source"
    digest = hashlib.sha256(object_name.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}"


def _evidence_output_name(prefix: str, source_object: str, rejected: bool = False) -> str:
    """Return the one canonical Evidence object for a Claims source."""

    token = _stable_token(source_object)
    if rejected:
        return f"{prefix.strip('/')}/rejected_evidence_current_{token}.csv"
    # Keep the historical evidence_run_ prefix so current object consumers
    # continue to find the file, while zz_current sorts after legacy runs.
    return f"{prefix.strip('/')}/evidence_run_zz_current_{token}.csv"


def _evidence_row_is_terminal(row: dict[str, str]) -> bool:
    """Return whether an evidence outcome is final for automatic retrieval."""

    status = row.get("evidence_status", "").strip().upper()
    if status in {"OK", "NO_RESULTS", "NO_ABSTRACT"}:
        return True
    if status != "ERROR":
        return False
    error = row.get("evidence_error", "").lower()
    transient_markers = (
        "time_budget",
        "timeout",
        "timed out",
        "network error",
        "temporarily",
        "temporarily unavailable",
        "http 408",
        "http 425",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "connection",
    )
    return bool(error) and not any(marker in error for marker in transient_markers)


def _latest_claim_rows(
    client: Any,
    namespace: str,
    bucket: str,
    claims_prefix: str,
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Read all Claims objects and keep one auditable row per record_id."""

    rows_by_id: dict[str, dict[str, str]] = {}
    objects = _list_csv_objects(client, namespace, bucket, claims_prefix)
    rows_seen = 0
    for object_name in objects:
        for source_row in _read_csv_object(client, namespace, bucket, object_name):
            rows_seen += 1
            record_id = source_row.get("record_id", "").strip()
            if not record_id:
                continue
            row = dict(source_row)
            row["source_claims_object"] = object_name
            previous = rows_by_id.get(record_id)
            if previous is None or _row_priority(row) >= _row_priority(previous):
                rows_by_id[record_id] = row
    return rows_by_id, {
        "claims_objects_scanned": len(objects),
        "claims_rows_seen": rows_seen,
        "unique_claims": len(rows_by_id),
    }


def _existing_evidence_ids(
    client: Any,
    namespace: str,
    bucket: str,
    evidence_prefix: str,
    rejected_prefix: str = "",
) -> set[str]:
    """Return terminal evidence IDs; transient errors remain retryable."""

    record_ids: set[str] = set()
    prefixes = [evidence_prefix] + ([rejected_prefix] if rejected_prefix else [])
    for prefix in prefixes:
        for object_name in _list_csv_objects(client, namespace, bucket, prefix):
            for row in _read_csv_object(client, namespace, bucket, object_name):
                record_id = row.get("record_id", "").strip()
                if record_id and _evidence_row_is_terminal(row):
                    record_ids.add(record_id)
    return record_ids


def _select_claims(
    rows_by_id: dict[str, dict[str, str]],
    completed_evidence_ids: set[str],
    max_rows: int,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    selected: list[dict[str, str]] = []
    pending_candidates: list[dict[str, str]] = []
    stats = Counter(
        {
            "claims_seen": len(rows_by_id),
            "skipped_existing_evidence": 0,
            "skipped_not_eligible": 0,
            "skipped_missing_query": 0,
        }
    )
    for record_id in sorted(rows_by_id):
        row = rows_by_id[record_id]
        if record_id in completed_evidence_ids:
            stats["skipped_existing_evidence"] += 1
            continue
        eligible = row.get("is_claim_eligible", "").strip().lower() == "true"
        if not eligible or not row.get("claim_text", "").strip():
            stats["skipped_not_eligible"] += 1
            continue
        if not _row_complete(row):
            stats["skipped_missing_query"] += 1
            continue
        pending_candidates.append(row)

    source_objects = sorted(
        {row.get("source_claims_object", "") for row in pending_candidates}
    )
    selected_source = source_objects[0] if source_objects else ""
    for row in pending_candidates:
        if row.get("source_claims_object", "") != selected_source:
            continue
        selected.append(row)
        if max_rows > 0 and len(selected) >= max_rows:
            break
    stats["pending"] = len(pending_candidates)
    stats["selected"] = len(selected)
    stats["selected_source_object"] = selected_source
    return selected, dict(stats)


def _http_request(
    endpoint: str,
    params: dict[str, str],
    timeout: float,
    accept: str,
    *,
    method: str = "GET",
) -> bytes:
    encoded_params = urllib.parse.urlencode(params, doseq=True).encode("utf-8")
    request_method = method.upper()
    headers = {"Accept": accept, "User-Agent": "mednews-thesis/0.1"}
    if request_method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            endpoint,
            data=encoded_params,
            headers=headers,
            method=request_method,
        )
    else:
        query = encoded_params.decode("ascii")
        request = urllib.request.Request(
            f"{endpoint}?{query}",
            headers=headers,
            method=request_method,
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as result:
            return result.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"PubMed HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"PubMed network error: {exc.reason}") from exc


def _search_pubmed(
    query: str,
    top_k: int,
    tool: str,
    email: str,
    api_key: str,
    timeout: float,
) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": str(top_k),
        "sort": "relevance",
        "tool": tool,
        "email": email,
    }
    if api_key:
        params["api_key"] = api_key
    payload = json.loads(
        _http_request(
            f"{PUBMED_BASE_URL}/esearch.fcgi",
            params,
            timeout,
            "application/json",
            method="POST",
        ).decode("utf-8", errors="replace")
    )
    return [str(value) for value in payload.get("esearchresult", {}).get("idlist", [])]


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _candidate_list(row: dict[str, str]) -> list[dict[str, Any]]:
    try:
        value = json.loads(row.get("pubmed_results_json", "[]") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid pubmed_results_json for {row.get('record_id', '')}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"pubmed_results_json must be a list for {row.get('record_id', '')}")
    return value


def _rerank_candidates(row: dict[str, str]) -> int:
    candidates = _candidate_list(row)
    embedding_scored = any(
        candidate.get("embedding_similarity") is not None for candidate in candidates
    )
    for candidate in candidates:
        abstract_es = str(candidate.get("abstract_es", "") or "").strip()
        if abstract_es:
            candidate["tfidf_cosine_similarity"] = round(
                _cosine_similarity(row.get("claim_text", ""), abstract_es),
                4,
            )
        else:
            candidate["tfidf_cosine_similarity"] = None
        # Keep the original field for compatibility with previously written
        # evidence CSVs; it now represents the transparent Spanish TF-IDF
        # score while `embedding_similarity` is the OCI ranking score.
        candidate["cosine_similarity"] = candidate.get("tfidf_cosine_similarity")
    candidates.sort(
        key=lambda item: (
            -(
                float(item["embedding_similarity"])
                if embedding_scored and item.get("embedding_similarity") is not None
                else float(item["cosine_similarity"])
                if item.get("cosine_similarity") is not None
                else -1.0
            ),
            -(
                float(item["cosine_similarity"])
                if item.get("cosine_similarity") is not None
                else -1.0
            ),
            int(item.get("pubmed_rank", 0)),
        )
    )
    row["pubmed_results_json"] = json.dumps(candidates, ensure_ascii=False)
    tfidf_scored = [item for item in candidates if item.get("cosine_similarity") is not None]
    embedding_candidates = [
        item for item in candidates if item.get("embedding_similarity") is not None
    ]
    if tfidf_scored:
        row["best_tfidf_similarity"] = str(
            max(float(item["cosine_similarity"]) for item in tfidf_scored)
        )
    else:
        row["best_tfidf_similarity"] = ""
    scored = embedding_candidates or tfidf_scored
    row["cosine_method"] = EMBEDDING_METHOD if embedding_candidates else COSINE_METHOD if tfidf_scored else row.get("cosine_method", "")
    row["ranking_method"] = EMBEDDING_METHOD if embedding_candidates else COSINE_METHOD if tfidf_scored else row.get("ranking_method", "")
    if scored:
        row["best_pmid"] = str(candidates[0].get("pmid", ""))
        if embedding_candidates:
            row["best_embedding_similarity"] = str(candidates[0].get("embedding_similarity", ""))
        else:
            row["best_embedding_similarity"] = ""
    else:
        row["best_pmid"] = ""
        row["best_embedding_similarity"] = ""
    return len(scored)


def _apply_translations(rows: list[dict[str, str]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Keep PubMed evidence in its source language until a local phase is defined."""

    run_id = ""
    if len(args) >= 7:
        run_id = str(args[6] or "")
    if kwargs.get("run_id") is not None:
        run_id = str(kwargs.get("run_id") or run_id)

    stats: Counter[str] = Counter()
    for row in rows:
        row["translation_model"] = ""
        row["translation_key_slot"] = ""
        row["translation_prompt_version"] = ""
        row["translation_run_id"] = run_id
        row.setdefault("translation_error", "")
        if row.get("evidence_status", "").strip().upper() != "OK":
            row["translation_status"] = "SKIPPED_NO_EVIDENCE"
            stats["skipped_no_evidence"] += 1
            continue
        try:
            candidates = _candidate_list(row)
        except ValueError as exc:
            row["translation_status"] = "ERROR"
            row["translation_error"] = str(exc)[:1500]
            stats["translation_errors"] += 1
            continue
        abstracts = [
            candidate for candidate in candidates
            if str(candidate.get("abstract_excerpt", "") or "").strip()
        ]
        if not abstracts:
            row["translation_status"] = "NO_ABSTRACTS"
            stats["skipped_no_abstracts"] += 1
            continue
        row["translation_status"] = "NOT_REQUESTED"
        row["translation_error"] = (
            "Translation is outside the current local PubMed retrieval phase; "
            "the retained abstract remains in its source language."
        )
        stats["translation_disabled"] += 1
    return dict(stats)



def _fetch_pubmed(
    pmids: list[str],
    tool: str,
    email: str,
    api_key: str,
    timeout: float,
) -> dict[str, dict[str, Any]]:
    if not pmids:
        return {}
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
        "tool": tool,
        "email": email,
    }
    if api_key:
        params["api_key"] = api_key
    root = ET.fromstring(
        _http_request(
            f"{PUBMED_BASE_URL}/efetch.fcgi",
            params,
            timeout,
            "application/xml",
            method="POST",
        )
    )
    records: dict[str, dict[str, Any]] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid = _element_text(article.find(".//PMID"))
        if not pmid:
            continue
        title = _element_text(article.find(".//ArticleTitle"))
        abstract_parts = [
            _element_text(node)
            for node in article.findall(".//Abstract/AbstractText")
            if _element_text(node)
        ]
        abstract = " ".join(abstract_parts)
        journal = _element_text(article.find(".//Journal/Title"))
        pub_date = _element_text(article.find(".//ArticleDate")) or _element_text(
            article.find(".//PubDate")
        )
        doi = ""
        for article_id in article.findall(".//ArticleId"):
            if str(article_id.attrib.get("IdType", "")).lower() == "doi":
                doi = _element_text(article_id)
                break
        publication_types = [
            _element_text(node)
            for node in article.findall(".//PublicationType")
            if _element_text(node)
        ]
        records[pmid] = {
            "pmid": pmid,
            "title": title,
            "abstract": abstract,
            "journal": journal,
            "publication_date": pub_date,
            "doi": doi,
            "publication_types": publication_types,
        }
    return records


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", without_accents)


def _cosine_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    left_counts = Counter(left_tokens)
    right_counts = Counter(right_tokens)
    vocabulary = set(left_counts) | set(right_counts)
    document_frequency = {
        token: int(token in left_counts) + int(token in right_counts)
        for token in vocabulary
    }
    left_vector: dict[str, float] = {}
    right_vector: dict[str, float] = {}
    total_left = len(left_tokens)
    total_right = len(right_tokens)
    for token in vocabulary:
        idf = math.log((1.0 + 2.0) / (1.0 + document_frequency[token])) + 1.0
        left_vector[token] = (left_counts[token] / total_left) * idf
        right_vector[token] = (right_counts[token] / total_right) * idf
    numerator = sum(left_vector[token] * right_vector[token] for token in vocabulary)
    left_norm = math.sqrt(sum(value * value for value in left_vector.values()))
    right_norm = math.sqrt(sum(value * value for value in right_vector.values()))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def _candidate_results(
    row: dict[str, str],
    pmids: list[str],
    records: dict[str, dict[str, Any]],
    top_k: int,
    abstract_max_chars: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for pubmed_rank, pmid in enumerate(pmids, start=1):
        article = records.get(pmid)
        abstract = str(article.get("abstract", "") or "").strip() if article else ""
        # A title alone is not usable evidence.  Excluding it here ensures a
        # source with PubMed hits but no abstract reaches NO_ABSTRACT rather
        # than an apparently successful evidence record.
        if not article or not abstract:
            continue
        candidates.append(
            {
                "pmid": pmid,
                "title": article.get("title", ""),
                "abstract_excerpt": abstract[:abstract_max_chars],
                "abstract_es": "",
                "journal": article.get("journal", ""),
                "publication_date": article.get("publication_date", ""),
                "doi": article.get("doi", ""),
                "publication_types": article.get("publication_types", []),
                "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "pubmed_rank": pubmed_rank,
                "translation_status": "NOT_REQUESTED",
                "cosine_similarity": None,
                "tfidf_cosine_similarity": None,
                "embedding_similarity": None,
            }
        )
    return candidates[:top_k]


def _prefilter_candidates(
    row: dict[str, str],
    candidates: list[dict[str, Any]],
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Rank candidates lexically before downstream work without dropping them by default."""

    query = (row.get("claim_text_en", "") or row.get("claim_text", "")).strip()
    for candidate in candidates:
        document = (
            f"{str(candidate.get('title', '') or '').strip()}\n"
            f"{str(candidate.get('abstract_excerpt', '') or '').strip()}"
        ).strip()
        candidate["prefilter_tfidf_similarity"] = round(_cosine_similarity(query, document), 6)
    ranked = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("prefilter_tfidf_similarity", 0.0)),
            int(item.get("pubmed_rank", 0)),
        ),
    )
    return ranked if top_k is None else ranked[:top_k]


def _embedding_batches(
    texts: list[str],
    max_inputs: int,
    max_input_tokens: int,
) -> list[list[str]]:
    """Split Embed 4 inputs by count and a conservative token estimate."""

    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for text in texts:
        estimate = max(1, (len(text) + 3) // 4)
        if current and (
            len(current) >= max_inputs
            or current_tokens + estimate > max_input_tokens
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(text)
        current_tokens += estimate
    if current:
        batches.append(current)
    return batches


def _embed_in_batches(
    client: Any,
    texts: list[str],
    compartment_id: str,
    model_id: str,
    input_type: str,
    output_dimensions: int,
    timeout: float,
    deadline: float,
    max_inputs: int,
    max_input_tokens: int,
    stats: Counter[str],
    stats_key: str,
) -> list[list[float]]:
    """Embed a potentially large list without crossing the Embed 4 input cap."""

    vectors: list[list[float]] = []
    for batch in _embedding_batches(texts, max_inputs, max_input_tokens):
        if time.monotonic() >= deadline:
            raise TimeoutError("Embedding time budget reached")
        vectors.extend(
            _embed_texts(
                client,
                batch,
                compartment_id,
                model_id,
                input_type,
                output_dimensions,
                timeout,
            )
        )
        stats[stats_key] += 1
    return vectors


def _apply_embeddings(
    rows: list[dict[str, str]],
    client: Any,
    compartment_id: str,
    model_id: str,
    output_dimensions: int,
    max_chars: int,
    timeout: float,
    deadline: float,
    max_inputs: int,
    max_input_tokens: int,
) -> dict[str, Any]:
    """Rank each PubMed candidate list with one query and one document batch."""

    stats: Counter[str] = Counter()
    eligible: list[tuple[dict[str, str], list[dict[str, Any]]]] = []
    for row in rows:
        row["embedding_model"] = model_id
        row["embedding_query_field"] = "claim_text_en" if row.get("claim_text_en", "").strip() else "claim_text"
        if row.get("evidence_status", "").strip().upper() != "OK":
            row["embedding_status"] = "SKIPPED_NO_EVIDENCE"
            continue
        try:
            candidates = _candidate_list(row)
        except ValueError as exc:
            row["embedding_status"] = "ERROR"
            row["evidence_error"] = str(exc)[:1500]
            stats["embedding_errors"] += 1
            continue
        if not candidates:
            row["embedding_status"] = "NO_CANDIDATES"
            continue
        eligible.append((row, candidates))

    if not eligible:
        return {"status": "no_candidates", **dict(stats)}
    if time.monotonic() >= deadline:
        for row, _ in eligible:
            row["embedding_status"] = "TIME_BUDGET"
        stats["embedding_errors"] += len(eligible)
        return {"status": "time_budget", **dict(stats)}

    query_texts = [
        (row.get("claim_text_en", "") or row.get("claim_text", "")).strip()[:max_chars]
        for row, _ in eligible
    ]
    document_map: list[tuple[dict[str, str], dict[str, Any], str]] = []
    document_texts_by_key: dict[str, str] = {}
    for row, candidates in eligible:
        for candidate in candidates:
            document_text = (
                f"{str(candidate.get('title', '') or '').strip()}\n"
                f"{str(candidate.get('abstract_excerpt', '') or '').strip()}"
            ).strip()[:max_chars]
            pmid = str(candidate.get("pmid", "")).strip()
            document_key = f"{pmid}:{hashlib.sha256(document_text.encode('utf-8')).hexdigest()}"
            if document_key in document_texts_by_key:
                stats["document_embedding_cache_hits"] += 1
            else:
                document_texts_by_key[document_key] = document_text
            document_map.append((row, candidate, document_key))
    document_keys = list(document_texts_by_key)
    document_texts = [document_texts_by_key[key] for key in document_keys]

    try:
        query_vectors = _embed_in_batches(
            client,
            query_texts,
            compartment_id,
            model_id,
            "SEARCH_QUERY",
            output_dimensions,
            timeout,
            deadline,
            max_inputs,
            max_input_tokens,
            stats,
            "embedding_query_requests",
        )
        if time.monotonic() >= deadline:
            raise TimeoutError("Embedding time budget reached before document batch")
        document_vectors = _embed_in_batches(
            client,
            document_texts,
            compartment_id,
            model_id,
            "SEARCH_DOCUMENT",
            output_dimensions,
            timeout,
            deadline,
            max_inputs,
            max_input_tokens,
            stats,
            "embedding_document_requests",
        )
        if len(query_vectors) != len(query_texts) or len(document_vectors) != len(document_texts):
            raise RuntimeError("OCI Embed 4 returned an incomplete batch of vectors")
    except TimeoutError as exc:
        detail = f"{type(exc).__name__}: {exc}"[:1500]
        for row, _ in eligible:
            row["embedding_status"] = "TIME_BUDGET"
            row["evidence_error"] = detail
        stats["embedding_errors"] += len(eligible)
        return {"status": "time_budget", "error": detail, **dict(stats)}
    except Exception as exc:  # noqa: BLE001 - keep TF-IDF fallback available
        detail = f"{type(exc).__name__}: {exc}"[:1500]
        for row, _ in eligible:
            row["embedding_status"] = "ERROR"
            row["evidence_error"] = detail
        stats["embedding_errors"] += len(eligible)
        return {"status": "error", "error": detail, **dict(stats)}

    query_by_claim = {
        _claim_identity(row): vector
        for (row, _), vector in zip(eligible, query_vectors)
    }
    document_vector_by_key = dict(zip(document_keys, document_vectors))
    for row, candidate, document_key in document_map:
        query_vector = query_by_claim.get(_claim_identity(row), [])
        document_vector = document_vector_by_key.get(document_key, [])
        candidate["embedding_similarity"] = round(
            _vector_cosine_similarity(query_vector, document_vector),
            6,
        )

    for row, candidates in eligible:
        candidates.sort(
            key=lambda item: (
                -float(item.get("embedding_similarity", -1.0)),
                int(item.get("pubmed_rank", 0)),
            )
        )
        row["pubmed_results_json"] = json.dumps(candidates, ensure_ascii=False)
        row["embedding_status"] = "OK"
        row["ranking_method"] = EMBEDDING_METHOD
        if candidates and candidates[0].get("embedding_similarity") is not None:
            row["best_pmid"] = str(candidates[0].get("pmid", ""))
            row["best_embedding_similarity"] = str(candidates[0]["embedding_similarity"])
            row["cosine_method"] = EMBEDDING_METHOD
        stats["rows_ranked"] += 1
        stats["candidates_ranked"] += len(candidates)
    stats["embedding_inputs"] = len(query_texts) + len(document_texts)
    return {"status": "ok", "model": model_id, **dict(stats)}


def _base_output_row(row: dict[str, str], run_id: str, retrieved_at: str) -> dict[str, str]:
    return {
        "record_id": row.get("record_id", ""),
        "claim_id": row.get("claim_id", ""),
        "claim_index": row.get("claim_index", ""),
        "llm_status": row.get("llm_status", ""),
        "source_body_object": row.get("source_body_object", ""),
        "country": row.get("country", ""),
        "source_name": row.get("source_name", ""),
        "canonical_url": row.get("canonical_url", ""),
        "published_at": row.get("published_at", ""),
        "title": row.get("title", ""),
        "subtitle_or_bajada": row.get("subtitle_or_bajada", ""),
        "topic": row.get("topic", ""),
        # Carry the article text through Evidence so the human-validated
        # Gold export can build the training input without a second join.
        "body": row.get("body", ""),
        "http_status": row.get("http_status", ""),
        "extraction_method": row.get("extraction_method", ""),
        "extraction_status": row.get("extraction_status", ""),
        "body_char_count": row.get("body_char_count", ""),
        "body_word_count": row.get("body_word_count", ""),
        "extracted_at": row.get("extracted_at", ""),
        "source_bronze_object": row.get("source_bronze_object", ""),
        "extraction_run_id": row.get("extraction_run_id", ""),
        "extraction_error": row.get("extraction_error", ""),
        "claim_text": row.get("claim_text", ""),
        "claim_text_en": row.get("claim_text_en", ""),
        "claim_type": row.get("claim_type", ""),
        "pubmed_query_en": row.get("pubmed_query_en", ""),
        "evidence_status": "",
        "evidence_error": "",
        "pubmed_result_count": "0",
        "best_pmid": "",
        "best_embedding_similarity": "",
        "best_tfidf_similarity": "",
        "pubmed_results_json": "[]",
        "source_claims_object": row.get("source_claims_object", ""),
        "claim_run_id": row.get("claim_run_id", ""),
        "evidence_run_id": run_id,
        "cosine_method": COSINE_METHOD,
        "ranking_method": "",
        "embedding_model": "",
        "embedding_status": "PENDING",
        "embedding_query_field": "",
        "translation_status": "PENDING",
        "translation_error": "",
        "translation_model": "",
        "translation_key_slot": "",
        "translation_prompt_version": "",
        "translation_run_id": run_id,
        "retrieved_at": retrieved_at,
    }


def _write_evidence(
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
    object_name = _evidence_output_name(prefix, source_object)
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig")),
        content_type="text/csv; charset=utf-8",
    )
    return object_name


def _write_rejected_evidence(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
    rows: list[dict[str, str]],
    source_object: str,
) -> str:
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=REJECTED_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    object_name = _evidence_output_name(prefix, source_object, rejected=True)
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig")),
        content_type="text/csv; charset=utf-8",
    )
    return object_name


def _evidence_rejection_reason(row: dict[str, str]) -> str:
    status = row.get("evidence_status", "").strip().upper()
    if status == "NO_RESULTS":
        return "PUBMED_NO_RESULTS"
    if status == "NO_ABSTRACT":
        return "PUBMED_RESULTS_WITHOUT_ABSTRACT"
    if status == "ERROR":
        return row.get("evidence_error", "") or "EVIDENCE_RETRIEVAL_ERROR"
    return status or "EVIDENCE_NOT_USABLE"


def _gate_evidence_for_human_review(
    rows: list[dict[str, str]],
    require_embeddings: bool = True,
    require_translation: bool = False,
) -> None:
    """Prevent degraded retrieval rows from masquerading as usable evidence."""

    for row in rows:
        if row.get("evidence_status", "").strip().upper() != "OK":
            continue
        issues: list[str] = []
        if row.get("llm_status", "").strip().upper() not in {"", "OK"}:
            issues.append("LLM_STATUS_NOT_OK")
        if require_embeddings and row.get("embedding_status", "").strip().upper() != "OK":
            issues.append(f"EMBEDDING_{row.get('embedding_status', 'MISSING').strip().upper() or 'MISSING'}")
        if require_translation and row.get("translation_status", "").strip().upper() != "OK":
            issues.append(f"TRANSLATION_{row.get('translation_status', 'MISSING').strip().upper() or 'MISSING'}")
        if issues:
            row["evidence_status"] = "ERROR"
            prior_error = row.get("evidence_error", "").strip()
            row["evidence_error"] = "; ".join([part for part in [prior_error, *issues] if part])[:1500]


def _process_v1_evidence_rows(
    claim_rows: list[dict[str, str]],
    run_id: str,
    retrieved_at: str,
    *,
    candidates_fetched: int,
    request_delay: float,
    request_timeout: float,
    abstract_max_chars: int,
    embedding_model: str,
    embedding_region: str,
    embedding_compartment_id: str,
    embedding_dimensions: int,
    embedding_max_chars: int,
    embedding_timeout: float,
    embedding_max_inputs: int,
    embedding_max_input_tokens: int,
    tool: str,
    email: str,
    api_key: str,
    deadline: float,
    deferred_claim_ids: set[str] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Retrieve, rank and translate a bounded set of Claims v1 rows.

    ``deferred_claim_ids`` is used only by the first invocation of a large
    source object.  It still writes one auditable ERROR row per deferred claim
    before a durable continuation job takes over the actual retrieval.
    """

    deferred_claim_ids = deferred_claim_ids or set()
    query_pmids: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    pubmed_requests = 0
    ready_rows = [
        row
        for row in claim_rows
        if not row.get("_input_error", "") and _claim_identity(row) not in deferred_claim_ids
    ]
    for row in ready_rows:
        identity = _claim_identity(row)
        if time.monotonic() >= deadline:
            errors[identity] = "EVIDENCE_TIME_BUDGET"
            continue
        try:
            query_pmids[identity] = _search_pubmed(
                row.get("pubmed_query_en", "")[:1000],
                candidates_fetched,
                tool,
                email,
                api_key,
                request_timeout,
            )
            pubmed_requests += 1
        except Exception as exc:  # noqa: BLE001 - every claim remains represented in JSONL
            errors[identity] = f"{type(exc).__name__}: {exc}"[:1500]
        remaining = deadline - time.monotonic()
        if remaining > request_delay:
            time.sleep(request_delay)

    all_pmids: list[str] = []
    for pmids in query_pmids.values():
        for pmid in pmids:
            if pmid not in all_pmids:
                all_pmids.append(pmid)
    records: dict[str, dict[str, Any]] = {}
    if all_pmids and time.monotonic() < deadline:
        try:
            records = _fetch_pubmed(all_pmids, tool, email, api_key, request_timeout)
            pubmed_requests += 1
        except Exception as exc:  # noqa: BLE001 - retain claim-level error detail
            fetch_error = f"{type(exc).__name__}: {exc}"[:1500]
            for identity in query_pmids:
                errors[identity] = fetch_error

    output_rows: list[dict[str, str]] = []
    for row in claim_rows:
        identity = _claim_identity(row)
        output = _base_output_row(row, run_id, retrieved_at)
        input_error = row.get("_input_error", "")
        if input_error:
            output["evidence_status"] = "ERROR"
            output["evidence_error"] = input_error[:1500]
        elif identity in deferred_claim_ids:
            output["evidence_status"] = "ERROR"
            output["evidence_error"] = "EVIDENCE_RETRIEVAL_CONTINUATION_QUEUED"
        elif identity in errors:
            output["evidence_status"] = "ERROR"
            output["evidence_error"] = errors[identity]
        else:
            pmids = query_pmids.get(identity, [])
            candidates = _candidate_results(row, pmids, records, candidates_fetched, abstract_max_chars)
            # Every usable abstract among the bounded PubMed results is
            # embedded and translated.  Lexical TF-IDF only establishes a
            # deterministic presentation order; it must not discard a
            # candidate before semantic ranking.
            candidates = _prefilter_candidates(row, candidates)
            output["pubmed_result_count"] = str(len(pmids))
            output["pubmed_results_json"] = json.dumps(candidates, ensure_ascii=False)
            if not pmids:
                output["evidence_status"] = "NO_RESULTS"
            elif not candidates:
                output["evidence_status"] = "NO_ABSTRACT"
            else:
                output["evidence_status"] = "OK"
        output_rows.append(output)

    embedding_stats: dict[str, Any] = {"status": "not_run", "model": embedding_model}
    if any(row.get("evidence_status", "").upper() == "OK" for row in output_rows):
        try:
            embedding_stats = _apply_embeddings(
                output_rows,
                _generative_ai_client(embedding_region, embedding_timeout),
                embedding_compartment_id,
                embedding_model,
                embedding_dimensions,
                embedding_max_chars,
                embedding_timeout,
                deadline,
                embedding_max_inputs,
                embedding_max_input_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - gate turns the affected rows into ERROR
            detail = f"{type(exc).__name__}: {exc}"[:1500]
            embedding_stats = {"status": "error", "error": detail}
            for row in output_rows:
                if row.get("evidence_status", "").upper() == "OK":
                    row["embedding_status"] = "ERROR"
                    row["embedding_model"] = embedding_model
                    row["evidence_error"] = detail

    translation_stats = _apply_translations(output_rows, run_id=run_id)
    _gate_evidence_for_human_review(output_rows, require_embeddings=True, require_translation=False)
    workload = {
        "claims_ready_for_pubmed": len(ready_rows),
        "claims_deferred_to_continuation": len(deferred_claim_ids),
        "pubmed_search_requests": len(query_pmids),
        "pubmed_requests": pubmed_requests,
        "unique_pmids_requested": len(all_pmids),
    }
    return output_rows, workload, embedding_stats, translation_stats


def _handler_legacy(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    retrieved_at = now.isoformat()
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        claims_prefix = os.getenv("SILVER_CLAIMS_PREFIX", "silver/claims").strip("/")
        evidence_prefix = os.getenv("SILVER_EVIDENCE_PREFIX", "silver/evidence").strip("/")
        rejected_prefix = os.getenv("SILVER_REJECTED_EVIDENCE_PREFIX", "silver/rejected/evidence").strip("/")
        # A zero max processes every eligible claim in the selected source.
        # Production can set a positive cap to let a recurring drain advance a
        # large backlog without crossing the PubMed time budget.
        max_rows = _env_int("EVIDENCE_MAX_ROWS", 20, minimum=0, maximum=1000)
        top_k = _env_int("PUBMED_TOP_K", 10, minimum=1, maximum=20)
        request_delay = _env_float("PUBMED_REQUEST_DELAY", 0.4, minimum=0.34)
        request_timeout = _env_float("PUBMED_REQUEST_TIMEOUT", 20.0, minimum=5.0)
        # Use almost all of the 300-second OCI Function window while keeping a
        # small configurable margin to persist the evidence CSV.
        max_seconds = _env_float("EVIDENCE_MAX_SECONDS", 295.0, minimum=30.0)
        time_buffer = _env_float("EVIDENCE_TIME_BUFFER", 5.0, minimum=1.0)
        abstract_max_chars = _env_int("ABSTRACT_MAX_CHARS", 1500, minimum=500, maximum=10000)
        embeddings_enabled = _env_bool("EMBEDDINGS_ENABLED", True)
        embedding_model = os.getenv("EMBEDDING_MODEL_ID", "cohere.embed-v4.0").strip()
        embedding_region = os.getenv("EMBEDDING_REGION", "us-ashburn-1").strip()
        embedding_compartment_id = os.getenv("GENAI_COMPARTMENT_ID", "").strip()
        embedding_dimensions = _env_int("EMBEDDING_OUTPUT_DIMENSIONS", 512, minimum=256, maximum=1536)
        embedding_max_chars = _env_int("EMBEDDING_MAX_CHARS", 6000, minimum=500, maximum=20000)
        embedding_max_inputs = _env_int("EMBEDDING_MAX_INPUTS", 96, minimum=1, maximum=1000)
        embedding_max_input_tokens = _env_int(
            "EMBEDDING_MAX_INPUT_TOKENS", 100000, minimum=1000, maximum=128000
        )
        embedding_timeout = _env_float("EMBEDDING_REQUEST_TIMEOUT", 45.0, minimum=5.0)
        tool = os.getenv("PUBMED_TOOL", "mednews-thesis").strip() or "mednews-thesis"
        email = _required_env("PUBMED_EMAIL")
        api_key = os.getenv("PUBMED_API_KEY", "").strip()
        if time_buffer >= max_seconds:
            raise RuntimeError("EVIDENCE_TIME_BUFFER must be smaller than EVIDENCE_MAX_SECONDS")
        deadline = started + max_seconds - time_buffer
        mode = "retrieve"

        storage = _object_storage_client()
        pubmed_requests = 0
        event_object = _event_object_name_or_none(data, bucket, claims_prefix)
        if event_object:
            claim_rows, scan_meta = _event_claim_rows(
                storage,
                namespace,
                bucket,
                event_object,
            )
            invocation_mode = "event"
        else:
            claim_rows, scan_meta = _latest_claim_rows(storage, namespace, bucket, claims_prefix)
            invocation_mode = "drain"
        existing_ids = _existing_evidence_ids(storage, namespace, bucket, evidence_prefix, rejected_prefix)
        pending, selection_meta = _select_claims(claim_rows, existing_ids, max_rows)
        source_object = str(selection_meta.get("selected_source_object", ""))

        query_pmids: dict[str, list[str]] = {}
        errors: dict[str, str] = {}
        for row in pending:
            if time.monotonic() >= deadline:
                errors[row.get("record_id", "")] = "EVIDENCE_TIME_BUDGET"
                continue
            try:
                query_pmids[row["record_id"]] = _search_pubmed(
                    row["pubmed_query_en"][:1000],
                    top_k,
                    tool,
                    email,
                    api_key,
                    request_timeout,
                )
                pubmed_requests += 1
            except Exception as exc:  # noqa: BLE001 - keep other claims moving
                errors[row.get("record_id", "")] = f"{type(exc).__name__}: {exc}"[:1500]
            remaining = deadline - time.monotonic()
            if remaining > request_delay:
                time.sleep(request_delay)

        all_pmids: list[str] = []
        for pmids in query_pmids.values():
            for pmid in pmids:
                if pmid not in all_pmids:
                    all_pmids.append(pmid)

        records: dict[str, dict[str, Any]] = {}
        if all_pmids and time.monotonic() < deadline:
            try:
                records = _fetch_pubmed(all_pmids, tool, email, api_key, request_timeout)
                pubmed_requests += 1
            except Exception as exc:  # noqa: BLE001 - preserve claim-level error output
                fetch_error = f"{type(exc).__name__}: {exc}"[:1500]
                for row in pending:
                    if row.get("record_id") in query_pmids:
                        errors[row.get("record_id", "")] = fetch_error

        output_rows: list[dict[str, str]] = []
        for row in pending:
            record_id = row.get("record_id", "")
            output = _base_output_row(row, run_id, retrieved_at)
            if record_id in errors:
                output["evidence_status"] = "ERROR"
                output["evidence_error"] = errors[record_id]
            else:
                pmids = query_pmids.get(record_id, [])
                candidates = _candidate_results(row, pmids, records, top_k, abstract_max_chars)
                output["pubmed_result_count"] = str(len(candidates))
                output["pubmed_results_json"] = json.dumps(candidates, ensure_ascii=False)
                if not pmids:
                    output["evidence_status"] = "NO_RESULTS"
                elif not candidates:
                    output["evidence_status"] = "NO_ABSTRACT"
                else:
                    output["evidence_status"] = "OK"
            output_rows.append(output)

        embedding_stats: dict[str, Any] = {
            "status": "disabled" if not embeddings_enabled else "not_run",
            "model": embedding_model if embeddings_enabled else "",
        }
        if embeddings_enabled and any(
            row.get("evidence_status", "").strip().upper() == "OK" for row in output_rows
        ):
            if not embedding_compartment_id:
                detail = "GENAI_COMPARTMENT_ID is not configured"
                embedding_stats = {"status": "configuration_error", "error": detail}
                for row in output_rows:
                    if row.get("evidence_status", "").strip().upper() == "OK":
                        row["embedding_status"] = "ERROR"
                        row["embedding_model"] = embedding_model
                        row["evidence_error"] = detail
            else:
                try:
                    embedding_client = _generative_ai_client(embedding_region, embedding_timeout)
                    embedding_stats = _apply_embeddings(
                        output_rows,
                        embedding_client,
                        embedding_compartment_id,
                        embedding_model,
                        embedding_dimensions,
                        embedding_max_chars,
                        embedding_timeout,
                        deadline,
                        embedding_max_inputs,
                        embedding_max_input_tokens,
                    )
                except Exception as exc:  # noqa: BLE001 - keep the ranking fallback usable
                    detail = f"{type(exc).__name__}: {exc}"[:1500]
                    embedding_stats = {"status": "error", "error": detail}
                    for row in output_rows:
                        if row.get("evidence_status", "").strip().upper() == "OK":
                            row["embedding_status"] = "ERROR"
                            row["embedding_model"] = embedding_model
                            row["evidence_error"] = detail

        translation_stats = _apply_translations(output_rows, run_id=run_id)
        _gate_evidence_for_human_review(
            output_rows,
            require_embeddings=embeddings_enabled,
            require_translation=False,
        )
        accepted_rows = [row for row in output_rows if row.get("evidence_status", "").upper() == "OK"]
        rejected_rows = [row for row in output_rows if row.get("evidence_status", "").upper() != "OK"]
        for row in rejected_rows:
            row["rejection_reason"] = _evidence_rejection_reason(row)
        status_counts = Counter(row.get("evidence_status", "") for row in output_rows)
        object_name = ""
        rejected_object_name = ""
        if output_rows and source_object:
            accepted_name = _evidence_output_name(evidence_prefix, source_object)
            rejected_name = _evidence_output_name(rejected_prefix, source_object, rejected=True)
            evidence_objects = _list_csv_objects(storage, namespace, bucket, evidence_prefix)
            rejected_objects = _list_csv_objects(storage, namespace, bucket, rejected_prefix)
            existing_accepted = (
                _read_csv_object(storage, namespace, bucket, accepted_name)
                if accepted_name in evidence_objects
                else []
            )
            existing_rejected = (
                _read_csv_object(storage, namespace, bucket, rejected_name)
                if rejected_name in rejected_objects
                else []
            )
            accepted_by_id = {
                row.get("record_id", ""): row
                for row in existing_accepted
                if row.get("record_id", "")
            }
            rejected_by_id = {
                row.get("record_id", ""): row
                for row in existing_rejected
                if row.get("record_id", "")
            }
            for row in accepted_rows:
                accepted_by_id[row["record_id"]] = row
                rejected_by_id.pop(row["record_id"], None)
            for row in rejected_rows:
                if row["record_id"] not in accepted_by_id:
                    rejected_by_id[row["record_id"]] = row
            if accepted_by_id:
                object_name = _write_evidence(
                    storage,
                    namespace,
                    bucket,
                    evidence_prefix,
                    sorted(accepted_by_id.values(), key=lambda row: row.get("record_id", "")),
                    source_object,
                )
            if rejected_by_id or rejected_name in rejected_objects:
                rejected_object_name = _write_rejected_evidence(
                    storage,
                    namespace,
                    bucket,
                    rejected_prefix,
                    sorted(rejected_by_id.values(), key=lambda row: row.get("record_id", "")),
                    source_object,
                )

        result = {
            "status": "ok" if not status_counts.get("ERROR") else "partial",
            "mode": mode,
            "invocation_mode": invocation_mode,
            "run_id": run_id,
            "bucket": bucket,
            "object_name": object_name,
            "source_object": source_object,
            "row_count": str(len(accepted_rows)),
            "rejected_object_name": rejected_object_name,
            "rejected_row_count": str(len(rejected_rows)),
            "status_counts": dict(status_counts),
            "rows_left_for_next_run": str(
                max(
                    0,
                    int(selection_meta.get("pending", 0))
                    - sum(1 for row in output_rows if _evidence_row_is_terminal(row)),
                )
            ),
            "pubmed_requests": str(pubmed_requests),
            "top_k": str(top_k),
            "max_rows": "all" if max_rows == 0 else str(max_rows),
            "translation": translation_stats,
            "cosine_method": (
                EMBEDDING_METHOD
                if embedding_stats.get("status") == "ok"
                else COSINE_METHOD
            ),
            "ranking_method": (
                EMBEDDING_METHOD
                if embedding_stats.get("status") == "ok"
                else COSINE_METHOD
            ),
            "embedding_model": embedding_model if embeddings_enabled else "",
            "embedding_dimensions": str(embedding_dimensions) if embeddings_enabled else "",
            "embedding_max_inputs": str(embedding_max_inputs) if embeddings_enabled else "",
            "embedding_max_input_tokens": str(embedding_max_input_tokens) if embeddings_enabled else "",
            "embedding": embedding_stats,
            "elapsed_seconds": f"{time.monotonic() - started:.2f}",
            "scan": scan_meta,
            "selection": selection_meta,
        }
        LOGGER.info("Silver evidence batch written: %s (%s rows)", object_name or "none", len(output_rows))
        return response.Response(
            ctx,
            response_data=json.dumps(result, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - expose useful invocation errors
        LOGGER.exception("retrieve-pubmed-evidence failed")
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "run_id": run_id, "message": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )


# ---------------------------------------------------------------------------
# JSONL v1 multi-claim contract
# ---------------------------------------------------------------------------


def _read_jsonl_object(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
) -> list[dict[str, Any]]:
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
    records: list[dict[str, Any]] = []
    for number, line in enumerate(bytes(content).decode("utf-8-sig", errors="replace").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSONL in {object_name} at line {number}") from exc
        if not isinstance(item, dict):
            raise RuntimeError(f"JSONL record {number} in {object_name} is not an object")
        records.append(item)
    return records


def _event_jsonl_object_name(data: io.BytesIO | None, bucket: str, prefix: str) -> str:
    """Read exactly the claims.jsonl object from an Object Storage event."""

    if data is None:
        raise RuntimeError("Object Storage event payload is required in evidence.v1")
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
        if object_name.startswith(expected_prefix) and object_name.endswith("/claims.jsonl"):
            return object_name
    raise RuntimeError(f"Event does not identify a claims.jsonl under {expected_prefix}")


def _evidence_v1_object_names(prefix: str, run_id: str) -> tuple[str, str]:
    base = f"{prefix.strip('/')}/run_id={run_id}"
    return f"{base}/evidence.jsonl", f"{base}/manifest.json"


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


def _read_json_object(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
) -> dict[str, Any]:
    """Read one JSON object from Object Storage with an auditable error."""

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
    try:
        document = json.loads(bytes(content).decode("utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {object_name}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"JSON object {object_name} is not an object")
    return document


def _object_exists(client: Any, namespace: str, bucket: str, object_name: str) -> bool:
    """Return whether an Object Storage object exists without masking real failures."""

    try:
        client.get_object(
            namespace_name=namespace,
            bucket_name=bucket,
            object_name=object_name,
        )
    except Exception as exc:  # noqa: BLE001 - OCI exposes a typed ServiceError at runtime
        if isinstance(exc, (FileNotFoundError, KeyError)) or getattr(exc, "status", None) == 404:
            return False
        if "404" in str(exc):
            return False
        raise
    return True


def _event_object_name_under_prefix(
    data: io.BytesIO | None,
    bucket: str,
    prefix: str,
    suffix: str,
) -> str:
    """Return the single Object Storage object named by an event payload."""

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
        if object_name.startswith(expected_prefix) and object_name.endswith(suffix):
            return object_name
    raise RuntimeError(f"Event does not identify a {suffix} object under {expected_prefix}")


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value or "").strip()


def _v1_claim_rows(
    client: Any,
    namespace: str,
    bucket: str,
    claims_object: str,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Expand an immutable Claims JSONL file to one input row per claim.

    The source Body object is joined once per object so `body` is not copied
    into Claims nor multiplied by the number of claims in Object Storage.
    """

    records = _read_jsonl_object(client, namespace, bucket, claims_object)
    body_cache: dict[str, dict[str, dict[str, str]]] = {}
    body_errors: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    stats: Counter[str] = Counter({"articles_seen": len(records), "claims_seen": 0})

    for record in records:
        if str(record.get("schema_version", "")).strip() != "claims.v1":
            raise RuntimeError(f"Unsupported Claims schema in {claims_object}")
        record_id = _as_text(record.get("record_id"))
        if not record_id:
            stats["skipped_missing_record_id"] += 1
            continue
        claims = record.get("claims", [])
        if not isinstance(claims, list):
            raise RuntimeError(f"Claims record {record_id} has a non-list claims field")
        stats["claims_seen"] += len(claims)
        if _as_text(record.get("llm_status")).upper() != "OK":
            stats["skipped_llm_not_ok"] += len(claims)
            continue

        source_body_object = _as_text(record.get("source_body_object"))
        if source_body_object and source_body_object not in body_cache and source_body_object not in body_errors:
            try:
                body_rows = _read_csv_object(client, namespace, bucket, source_body_object)
                body_cache[source_body_object] = {
                    row.get("record_id", "").strip(): row
                    for row in body_rows
                    if row.get("record_id", "").strip()
                }
            except Exception as exc:  # noqa: BLE001 - create per-claim audit errors below
                body_errors[source_body_object] = f"BODY_SOURCE_ERROR: {type(exc).__name__}: {exc}"[:1500]

        article = record.get("article", {})
        if not isinstance(article, dict):
            article = {}
        source_body_row = body_cache.get(source_body_object, {}).get(record_id, {})
        source_error = body_errors.get(source_body_object, "")
        if not source_error and not source_body_row:
            source_error = "BODY_SOURCE_ERROR: record_id missing from source body object"

        for claim in claims:
            if not isinstance(claim, dict):
                stats["skipped_invalid_claim"] += 1
                continue
            claim_id = _as_text(claim.get("claim_id"))
            if not claim_id:
                stats["skipped_missing_claim_id"] += 1
                continue
            if _as_text(claim.get("is_claim_eligible")).lower() != "true" or not _as_text(
                claim.get("claim_text")
            ):
                stats["skipped_not_eligible"] += 1
                continue
            merged = {str(key): _as_text(value) for key, value in article.items()}
            merged.update({str(key): _as_text(value) for key, value in source_body_row.items()})
            merged.update({str(key): _as_text(value) for key, value in claim.items()})
            merged.update(
                {
                    "record_id": record_id,
                    "claim_id": claim_id,
                    "llm_status": "OK",
                    "source_claims_object": claims_object,
                    "source_body_object": source_body_object,
                    "body_sha256": _as_text(record.get("body_sha256")),
                    "claim_run_id": _as_text(record.get("run_id")),
                    "_input_error": source_error,
                }
            )
            if not _row_complete(merged) and not source_error:
                merged["_input_error"] = "CLAIM_QUERY_NOT_READY"
            rows.append(merged)
    stats["claims_selected"] = len(rows)
    return rows, dict(stats)


def _v1_article_metadata(row: dict[str, str]) -> dict[str, str]:
    fields = (
        "country",
        "source_name",
        "canonical_url",
        "published_at",
        "title",
        "subtitle_or_bajada",
        "topic",
        "selection_status",
        "retrieved_at",
        "http_status",
        "extraction_method",
        "extraction_status",
        "body_char_count",
        "body_word_count",
        "extracted_at",
        "source_bronze_object",
        "extraction_run_id",
        "extraction_error",
    )
    return {field: row.get(field, "") for field in fields}


def _evidence_v1_record(row: dict[str, str], run_id: str) -> dict[str, Any]:
    try:
        candidates = _candidate_list(row)
    except ValueError:
        candidates = []
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "evidence_run_id": row.get("evidence_run_id", run_id),
        "record_id": row.get("record_id", ""),
        "claim_id": row.get("claim_id", ""),
        "claim_index": row.get("claim_index", ""),
        "source_claims_object": row.get("source_claims_object", ""),
        "source_body_object": row.get("source_body_object", ""),
        "body_sha256": row.get("body_sha256", ""),
        "article": _v1_article_metadata(row),
        "llm_status": row.get("llm_status", ""),
        "claim_text": row.get("claim_text", ""),
        "claim_text_en": row.get("claim_text_en", ""),
        "pubmed_query_en": row.get("pubmed_query_en", ""),
        "claim_type": row.get("claim_type", ""),
        "claim_status": row.get("claim_status", ""),
        "evidence_status": row.get("evidence_status", ""),
        "evidence_error": row.get("evidence_error", ""),
        "pubmed_result_count": int(row.get("pubmed_result_count", "0") or 0),
        "usable_candidate_count": len(candidates),
        "best_pmid": row.get("best_pmid", ""),
        "best_embedding_similarity": row.get("best_embedding_similarity", ""),
        "best_tfidf_similarity": row.get("best_tfidf_similarity", ""),
        "cosine_method": row.get("cosine_method", ""),
        "ranking_method": row.get("ranking_method", ""),
        "embedding_model": row.get("embedding_model", ""),
        "embedding_status": row.get("embedding_status", ""),
        "embedding_query_field": row.get("embedding_query_field", ""),
        "translation_status": row.get("translation_status", ""),
        "translation_error": row.get("translation_error", ""),
        "translation_model": row.get("translation_model", ""),
        "translation_prompt_version": row.get("translation_prompt_version", ""),
        "translation_run_id": row.get("translation_run_id", ""),
        "retrieved_at": row.get("retrieved_at", ""),
        "candidates": candidates,
    }


def _evidence_abstract_metrics(records: list[dict[str, Any]]) -> dict[str, int]:
    """Expose the abstract and embedding workload without retaining secrets."""

    abstract_count = 0
    source_chars = 0
    translated_chars = 0
    unique_abstracts: set[str] = set()
    for record in records:
        candidates = record.get("candidates", [])
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            abstract = str(candidate.get("abstract_excerpt", "") or "").strip()
            if not abstract:
                continue
            abstract_count += 1
            source_chars += len(abstract)
            pmid = str(candidate.get("pmid", "") or "").strip()
            unique_abstracts.add(f"{pmid}:{hashlib.sha256(abstract.encode('utf-8')).hexdigest()}")
            translated_chars += len(str(candidate.get("abstract_es", "") or "").strip())
    return {
        "candidate_abstracts": abstract_count,
        "unique_pmid_abstracts": len(unique_abstracts),
        "abstract_source_chars": source_chars,
        "abstract_translation_chars": translated_chars,
    }


def _evidence_claim_key(record: dict[str, Any]) -> str:
    """Return the durable article/claim identity used across continuation jobs."""

    record_id = _as_text(record.get("record_id"))
    claim_id = _as_text(record.get("claim_id"))
    if not record_id or not claim_id:
        raise RuntimeError("Evidence continuation record lacks record_id or claim_id")
    return f"{record_id}::{claim_id}"


def _evidence_manifest_status(records: list[dict[str, Any]]) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter(_as_text(record.get("evidence_status")) for record in records)
    if not counts.get("ERROR"):
        return "COMPLETE", counts
    return ("FAILED" if len(counts) == 1 else "PARTIAL"), counts


def _v1_runtime_settings() -> dict[str, Any]:
    """Read the shared Evidence v1 contract for first and continued work."""

    configured_max_rows = _env_int("EVIDENCE_MAX_ROWS", 0, minimum=0, maximum=1000)
    if configured_max_rows:
        raise RuntimeError("EVIDENCE_MAX_ROWS must be 0 in evidence.v1; every claim must be processed")
    candidates_fetched = _env_int("PUBMED_CANDIDATES_FETCHED", 5, minimum=1, maximum=10)
    request_delay = _env_float("PUBMED_REQUEST_DELAY", 0.4, minimum=0.34)
    request_timeout = _env_float("PUBMED_REQUEST_TIMEOUT", 20.0, minimum=5.0)
    abstract_max_chars = _env_int("ABSTRACT_MAX_CHARS", 1500, minimum=500, maximum=10000)
    embeddings_enabled = _env_bool("EMBEDDINGS_ENABLED", True)
    if not embeddings_enabled:
        raise RuntimeError("evidence.v1 requires EMBEDDINGS_ENABLED=true")
    settings = {
        "candidates_fetched": candidates_fetched,
        "request_delay": request_delay,
        "request_timeout": request_timeout,
        "abstract_max_chars": abstract_max_chars,
        "translation_enabled": False,
        "embedding_model": os.getenv("EMBEDDING_MODEL_ID", "cohere.embed-v4.0").strip(),
        "embedding_region": os.getenv("EMBEDDING_REGION", "us-ashburn-1").strip(),
        "embedding_compartment_id": _required_env("GENAI_COMPARTMENT_ID"),
        "embedding_dimensions": _env_int("EMBEDDING_OUTPUT_DIMENSIONS", 512, minimum=256, maximum=1536),
        "embedding_max_chars": _env_int("EMBEDDING_MAX_CHARS", 6000, minimum=500, maximum=20000),
        "embedding_max_inputs": _env_int("EMBEDDING_MAX_INPUTS", 96, minimum=1, maximum=1000),
        "embedding_max_input_tokens": _env_int(
            "EMBEDDING_MAX_INPUT_TOKENS", 100000, minimum=1000, maximum=128000
        ),
        "embedding_timeout": _env_float("EMBEDDING_REQUEST_TIMEOUT", 45.0, minimum=5.0),
        "tool": os.getenv("PUBMED_TOOL", "mednews-thesis").strip() or "mednews-thesis",
        "email": _required_env("PUBMED_EMAIL"),
        "api_key": os.getenv("PUBMED_API_KEY", "").strip(),
        "retrieval_continuation_enabled": _env_bool("RETRIEVAL_CONTINUATION_ENABLED", True),
        "retrieval_job_prefix": os.getenv(
            "SILVER_RETRIEVAL_JOB_PREFIX", "silver/evidence-retrieval-jobs"
        ).strip("/"),
        "retrieval_stage_prefix": os.getenv(
            "SILVER_RETRIEVAL_STAGE_PREFIX", "silver/evidence-retrieval-stage"
        ).strip("/"),
        "retrieval_chunk_size": _env_int(
            "RETRIEVAL_CONTINUATION_MAX_CLAIMS", 8, minimum=1, maximum=20
        ),
        "retrieval_max_attempts": _env_int(
            "RETRIEVAL_CONTINUATION_MAX_ATTEMPTS", 3, minimum=1, maximum=5
        ),
    }
    return settings


def _process_v1_evidence_rows_with_settings(
    claim_rows: list[dict[str, str]],
    run_id: str,
    retrieved_at: str,
    settings: dict[str, Any],
    deadline: float,
    deferred_claim_ids: set[str] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Keep both retrieval entrypoints on the exact same evidence contract."""

    return _process_v1_evidence_rows(
        claim_rows,
        run_id,
        retrieved_at,
        candidates_fetched=settings["candidates_fetched"],
        request_delay=settings["request_delay"],
        request_timeout=settings["request_timeout"],
        abstract_max_chars=settings["abstract_max_chars"],
        embedding_model=settings["embedding_model"],
        embedding_region=settings["embedding_region"],
        embedding_compartment_id=settings["embedding_compartment_id"],
        embedding_dimensions=settings["embedding_dimensions"],
        embedding_max_chars=settings["embedding_max_chars"],
        embedding_timeout=settings["embedding_timeout"],
        embedding_max_inputs=settings["embedding_max_inputs"],
        embedding_max_input_tokens=settings["embedding_max_input_tokens"],
        tool=settings["tool"],
        email=settings["email"],
        api_key=settings["api_key"],
        deadline=deadline,
        deferred_claim_ids=deferred_claim_ids,
    )


def _retrieval_job_object_name(prefix: str, parent_run_id: str, sequence: int) -> str:
    if sequence < 1:
        raise RuntimeError("Retrieval continuation sequence must be positive")
    return f"{prefix.strip('/')}/run_id={parent_run_id}/job-{sequence:04d}.job.json"


def _retrieval_stage_object_names(
    prefix: str,
    parent_run_id: str,
    sequence: int,
) -> tuple[str, str]:
    base = f"{prefix.strip('/')}/run_id={parent_run_id}/job-{sequence:04d}"
    return f"{base}/evidence.jsonl", f"{base}/completion.json"


def _retrieval_completion_object_name(job_object: str) -> str:
    if not job_object.endswith(".job.json"):
        raise RuntimeError("Retrieval continuation job object has an invalid suffix")
    return f"{job_object[:-len('.job.json')]}.done"


def _retrieval_job_document(
    parent_run_id: str,
    parent_evidence_object: str,
    source_claims_object: str,
    sequence: int,
    chunk_size: int,
    max_attempts: int,
    items: list[dict[str, Any]],
    staging_objects: list[str],
) -> dict[str, Any]:
    """Build an immutable queue of Claims that have not reached PubMed yet."""

    if not items:
        raise RuntimeError("Retrieval continuation jobs require at least one claim")
    return {
        "schema_version": RETRIEVAL_JOB_SCHEMA_VERSION,
        "stage": RETRIEVAL_CONTINUATION_STAGE,
        "parent_run_id": parent_run_id,
        "parent_evidence_object": parent_evidence_object,
        "source_claims_object": source_claims_object,
        "sequence": sequence,
        "chunk_size": chunk_size,
        "max_attempts": max_attempts,
        "items": items,
        "staging_objects": staging_objects,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _retrieval_continuation_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = document.get("items", [])
    if not isinstance(raw_items, list) or not raw_items:
        raise RuntimeError("Retrieval continuation job has no items")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Retrieval continuation job item {index} is invalid")
        claim_key = _as_text(item.get("claim_key"))
        if "::" not in claim_key:
            raise RuntimeError(f"Retrieval continuation job item {index} has an invalid claim key")
        if claim_key in seen:
            raise RuntimeError(f"Retrieval continuation job repeats {claim_key}")
        seen.add(claim_key)
        try:
            attempt = int(item.get("attempt", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Retrieval continuation job item {index} has an invalid attempt") from exc
        if attempt < 0:
            raise RuntimeError(f"Retrieval continuation job item {index} has a negative attempt")
        items.append({"claim_key": claim_key, "attempt": attempt})
    return items


def _claim_rows_by_key(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for row in rows:
        claim_key = _evidence_claim_key(row)
        if claim_key in by_key:
            raise RuntimeError(f"Claims source repeats {claim_key}")
        by_key[claim_key] = row
    return by_key


def _retrieval_retryable_evidence_record(record: dict[str, Any]) -> bool:
    """Return whether a full PubMed/Embed retry may safely recover a claim."""

    if _as_text(record.get("schema_version")) != EVIDENCE_SCHEMA_VERSION:
        return False
    if _as_text(record.get("llm_status")).upper() != "OK":
        return False
    if _as_text(record.get("evidence_status")).upper() != "ERROR":
        return False
    error_text = " ".join(
        (
            _as_text(record.get("evidence_error")),
            _as_text(record.get("embedding_status")),
        )
    ).lower()
    transient_markers = (
        "evidence_time_budget",
        "time budget",
        "timeout",
        "timed out",
        "network",
        "temporarily",
        "unavailable",
        "connection",
        "http 408",
        "http 425",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    return any(marker in error_text for marker in transient_markers)


def _schedule_retrieval_continuation(
    storage: Any,
    namespace: str,
    bucket: str,
    job_prefix: str,
    parent_run_id: str,
    parent_evidence_object: str,
    source_claims_object: str,
    pending_rows: list[dict[str, str]],
    enabled: bool,
    chunk_size: int,
    max_attempts: int,
) -> dict[str, Any]:
    """Persist the remaining claim identities without duplicating source bodies."""

    if not enabled:
        return {"status": "DISABLED", "pending_claims": len(pending_rows)}
    if not pending_rows:
        return {"status": "NOT_NEEDED", "pending_claims": 0}
    items = [{"claim_key": _evidence_claim_key(row), "attempt": 0} for row in pending_rows]
    job_object = _retrieval_job_object_name(job_prefix, parent_run_id, 1)
    job = _retrieval_job_document(
        parent_run_id,
        parent_evidence_object,
        source_claims_object,
        1,
        chunk_size,
        max_attempts,
        items,
        [],
    )
    _write_json_object(storage, namespace, bucket, job_object, job)
    return {
        "status": "SCHEDULED",
        "pending_claims": len(items),
        "job_object": job_object,
        "chunk_size": chunk_size,
        "max_attempts": max_attempts,
    }


def _finalize_retrieval_continuation(
    storage: Any,
    namespace: str,
    bucket: str,
    evidence_prefix: str,
    parent_run_id: str,
    parent_evidence_object: str,
    source_claims_object: str,
    staging_objects: list[str],
    started_at: datetime,
) -> tuple[str, str, str, dict[str, Any], list[dict[str, Any]]]:
    """Overlay retrieval stages on the complete parent inventory of claims."""

    parent_records = _read_jsonl_object(storage, namespace, bucket, parent_evidence_object)
    if not parent_records:
        raise RuntimeError("Parent evidence output is empty")
    overrides: dict[str, dict[str, Any]] = {}
    for stage_object in staging_objects:
        for record in _read_jsonl_object(storage, namespace, bucket, stage_object):
            overrides[_evidence_claim_key(record)] = record

    final_run_id = f"{parent_run_id}_retrieval_final"
    now = datetime.now(timezone.utc)
    final_records: list[dict[str, Any]] = []
    parent_keys: set[str] = set()
    for record in parent_records:
        claim_key = _evidence_claim_key(record)
        if claim_key in parent_keys:
            raise RuntimeError(f"Parent evidence output repeats {claim_key}")
        parent_keys.add(claim_key)
        selected = dict(overrides.get(claim_key, record))
        selected["schema_version"] = EVIDENCE_SCHEMA_VERSION
        selected["run_id"] = final_run_id
        selected["evidence_run_id"] = final_run_id
        selected["retrieved_at"] = now.isoformat()
        final_records.append(selected)
    unexpected = sorted(set(overrides) - parent_keys)
    if unexpected:
        raise RuntimeError(
            "Retrieval continuation stage contains claims absent from the parent Evidence output: "
            + ", ".join(unexpected[:5])
        )

    evidence_object, manifest_object = _evidence_v1_object_names(evidence_prefix, final_run_id)
    manifest_status, evidence_counts = _evidence_manifest_status(final_records)
    _write_jsonl_object(storage, namespace, bucket, evidence_object, final_records)
    manifest = {
        "schema_version": EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "stage": "evidence",
        "run_id": final_run_id,
        "status": manifest_status,
        "source_object": source_claims_object,
        "evidence_object": evidence_object,
        "started_at": started_at.isoformat(),
        "finished_at": now.isoformat(),
        "elapsed_seconds": 0.0,
        "evidence_claims_processed": len(final_records),
        "candidates_total": sum(
            len(record.get("candidates", []))
            for record in final_records
            if isinstance(record.get("candidates"), list)
        ),
        "evidence_status_counts": dict(evidence_counts),
        "continuation": {
            "status": "COMPLETE",
            "stage": RETRIEVAL_CONTINUATION_STAGE,
            "parent_run_id": parent_run_id,
            "parent_evidence_object": parent_evidence_object,
            "staging_objects": staging_objects,
            "stages_processed": len(staging_objects),
        },
        "workload": {"abstracts": _evidence_abstract_metrics(final_records)},
    }
    _write_json_object(storage, namespace, bucket, manifest_object, manifest)
    return final_run_id, evidence_object, manifest_object, manifest, final_records


def _handler_v1(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    """Start a durable, serial retrieval chain for one Claims v1 object."""

    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    retrieved_at = now.isoformat()
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        claims_prefix = os.getenv("SILVER_CLAIMS_PREFIX", "silver/claims").strip("/")
        evidence_prefix = os.getenv("SILVER_EVIDENCE_PREFIX", "silver/evidence").strip("/")
        max_seconds = _env_float("EVIDENCE_MAX_SECONDS", 285.0, minimum=30.0)
        time_buffer = _env_float("EVIDENCE_TIME_BUFFER", 10.0, minimum=1.0)
        if time_buffer >= max_seconds:
            raise RuntimeError("EVIDENCE_TIME_BUFFER must be smaller than EVIDENCE_MAX_SECONDS")
        settings = _v1_runtime_settings()
        deadline = started + max_seconds - time_buffer

        storage = _object_storage_client()
        source_object = _event_jsonl_object_name(data, bucket, claims_prefix)
        claim_rows, scan = _v1_claim_rows(storage, namespace, bucket, source_object)
        ready_rows = [row for row in claim_rows if not row.get("_input_error", "")]
        # Validate the durable identities before publishing a parent that the
        # continuation must later overlay exactly once.
        _claim_rows_by_key(claim_rows)

        pending_rows: list[dict[str, str]] = []
        deferred_claim_ids: set[str] = set()
        if settings["retrieval_continuation_enabled"]:
            initial_rows = ready_rows[: settings["retrieval_chunk_size"]]
            pending_rows = ready_rows[len(initial_rows) :]
            deferred_claim_ids = {_claim_identity(row) for row in pending_rows}

        output_rows, workload, embedding_stats, translation_stats = _process_v1_evidence_rows_with_settings(
            claim_rows,
            run_id,
            retrieved_at,
            settings,
            deadline,
            deferred_claim_ids,
        )
        evidence_object, manifest_object = _evidence_v1_object_names(evidence_prefix, run_id)
        evidence_records = [_evidence_v1_record(row, run_id) for row in output_rows]
        _write_jsonl_object(storage, namespace, bucket, evidence_object, evidence_records)

        if pending_rows:
            try:
                continuation: dict[str, Any] = _schedule_retrieval_continuation(
                    storage,
                    namespace,
                    bucket,
                    settings["retrieval_job_prefix"],
                    run_id,
                    evidence_object,
                    source_object,
                    pending_rows,
                    True,
                    settings["retrieval_chunk_size"],
                    settings["retrieval_max_attempts"],
                )
                continuation["stage"] = RETRIEVAL_CONTINUATION_STAGE
            except Exception as exc:  # noqa: BLE001 - preserve the parent inventory for recovery
                LOGGER.exception("Unable to schedule retrieval continuation")
                continuation = {
                    "status": "ERROR",
                    "stage": RETRIEVAL_CONTINUATION_STAGE,
                    "pending_claims": len(pending_rows),
                    "error": f"{type(exc).__name__}: {exc}"[:1500],
                }
        else:
            continuation = {"status": "NOT_NEEDED", "pending_claims": 0}

        status_counts = Counter(row.get("evidence_status", "") for row in output_rows)
        manifest_status = "COMPLETE"
        if status_counts.get("ERROR"):
            manifest_status = "FAILED" if len(status_counts) == 1 else "PARTIAL"
        manifest = {
            "schema_version": EVIDENCE_MANIFEST_SCHEMA_VERSION,
            "stage": "evidence",
            "run_id": run_id,
            "status": manifest_status,
            "source_object": source_object,
            "evidence_object": evidence_object,
            "started_at": now.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "evidence_claims_processed": len(output_rows),
            "candidates_total": sum(len(record["candidates"]) for record in evidence_records),
            "evidence_status_counts": dict(status_counts),
            "scan": scan,
            "configuration": {
                "candidates_fetched_per_claim": settings["candidates_fetched"],
                "embed_all_retrieved_candidates_with_abstract": True,
                "retrieval_continuation_max_claims": settings["retrieval_chunk_size"],
                "translation_enabled": False,
                "embedding_model": settings["embedding_model"],
            },
            "workload": {**workload, "abstracts": _evidence_abstract_metrics(evidence_records)},
            "embedding": embedding_stats,
            "translation": translation_stats,
            "continuation": continuation,
        }
        _write_json_object(storage, namespace, bucket, manifest_object, manifest)
        result = {
            "status": manifest_status.lower(),
            "mode": "evidence.v1",
            "run_id": run_id,
            "source_object": source_object,
            "object_name": evidence_object,
            "manifest_object": manifest_object,
            "evidence_claims_processed": len(output_rows),
            "status_counts": dict(status_counts),
            "pubmed_requests": workload["pubmed_requests"],
            "continuation": continuation,
            "elapsed_seconds": f"{time.monotonic() - started:.2f}",
        }
        LOGGER.info("Evidence JSONL written: %s (%s claims)", evidence_object, len(evidence_records))
        return response.Response(
            ctx,
            response_data=json.dumps(result, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - make external/config failures observable
        LOGGER.exception("retrieve-pubmed-evidence v1 failed")
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "run_id": run_id, "message": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )


def _handler_retrieval_continuation(
    ctx: Any,
    data: io.BytesIO | None = None,
) -> response.Response:
    """Retrieve one small claim tranche and enqueue the next one serially."""

    started_monotonic = time.monotonic()
    started_at = datetime.now(timezone.utc)
    continuation_run_id = f"retrieval_{started_at.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        claims_prefix = os.getenv("SILVER_CLAIMS_PREFIX", "silver/claims").strip("/")
        evidence_prefix = os.getenv("SILVER_EVIDENCE_PREFIX", "silver/evidence").strip("/")
        settings = _v1_runtime_settings()
        max_seconds = _env_float("RETRIEVAL_CONTINUATION_MAX_SECONDS", 285.0, minimum=30.0)
        time_buffer = _env_float("RETRIEVAL_CONTINUATION_TIME_BUFFER", 10.0, minimum=1.0)
        if time_buffer >= max_seconds:
            raise RuntimeError("RETRIEVAL_CONTINUATION_TIME_BUFFER must be smaller than the max seconds")
        deadline = started_monotonic + max_seconds - time_buffer

        storage = _object_storage_client()
        job_object = _event_object_name_under_prefix(
            data, bucket, settings["retrieval_job_prefix"], ".job.json"
        )
        completion_object = _retrieval_completion_object_name(job_object)
        if _object_exists(storage, namespace, bucket, completion_object):
            result = {
                "status": "already_complete",
                "mode": RETRIEVAL_CONTINUATION_STAGE,
                "job_object": job_object,
                "completion_object": completion_object,
            }
            return response.Response(
                ctx,
                response_data=json.dumps(result, ensure_ascii=False),
                headers={"Content-Type": "application/json"},
            )

        job = _read_json_object(storage, namespace, bucket, job_object)
        if job.get("schema_version") != RETRIEVAL_JOB_SCHEMA_VERSION or job.get("stage") != RETRIEVAL_CONTINUATION_STAGE:
            raise RuntimeError("Unsupported retrieval continuation job schema or stage")
        parent_run_id = _as_text(job.get("parent_run_id"))
        parent_evidence_object = _as_text(job.get("parent_evidence_object"))
        source_claims_object = _as_text(job.get("source_claims_object"))
        if not parent_run_id or not parent_evidence_object or not source_claims_object:
            raise RuntimeError("Retrieval continuation job lacks parent source metadata")
        if not source_claims_object.startswith(f"{claims_prefix}/") or not source_claims_object.endswith("/claims.jsonl"):
            raise RuntimeError("Retrieval continuation job has an invalid Claims source object")
        try:
            sequence = int(job.get("sequence", 0))
            chunk_size = int(job.get("chunk_size", 0))
            max_attempts = int(job.get("max_attempts", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Retrieval continuation job has invalid numeric limits") from exc
        if sequence < 1 or not 1 <= chunk_size <= 20 or not 1 <= max_attempts <= 5:
            raise RuntimeError("Retrieval continuation job has unsafe numeric limits")
        staging_objects = job.get("staging_objects", [])
        if not isinstance(staging_objects, list) or not all(isinstance(value, str) and value for value in staging_objects):
            raise RuntimeError("Retrieval continuation job has invalid staging object references")
        items = _retrieval_continuation_items(job)
        current_items = items[:chunk_size]
        remaining_items = items[chunk_size:]
        source_rows, scan = _v1_claim_rows(storage, namespace, bucket, source_claims_object)
        rows_by_key = _claim_rows_by_key(source_rows)
        missing = [item["claim_key"] for item in current_items if item["claim_key"] not in rows_by_key]
        if missing:
            raise RuntimeError("Retrieval continuation source no longer contains " + ", ".join(missing[:5]))
        current_rows = [rows_by_key[item["claim_key"]] for item in current_items]

        output_rows, workload, embedding_stats, translation_stats = _process_v1_evidence_rows_with_settings(
            current_rows,
            continuation_run_id,
            started_at.isoformat(),
            settings,
            deadline,
        )
        stage_records = [_evidence_v1_record(row, continuation_run_id) for row in output_rows]
        stage_object, stage_manifest_object = _retrieval_stage_object_names(
            settings["retrieval_stage_prefix"], parent_run_id, sequence
        )
        _write_jsonl_object(storage, namespace, bucket, stage_object, stage_records)
        _write_json_object(
            storage,
            namespace,
            bucket,
            stage_manifest_object,
            {
                "schema_version": RETRIEVAL_JOB_SCHEMA_VERSION,
                "stage": RETRIEVAL_CONTINUATION_STAGE,
                "parent_run_id": parent_run_id,
                "job_object": job_object,
                "sequence": sequence,
                "records_processed": len(stage_records),
                "scan": scan,
                "workload": {**workload, "abstracts": _evidence_abstract_metrics(stage_records)},
                "embedding": embedding_stats,
                "translation": translation_stats,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        retry_items: list[dict[str, Any]] = []
        for item, record in zip(current_items, stage_records):
            attempt = int(item["attempt"]) + 1
            if _retrieval_retryable_evidence_record(record) and attempt < max_attempts:
                retry_items.append({"claim_key": item["claim_key"], "attempt": attempt})
        next_staging_objects = [*staging_objects, stage_object]
        next_items = [*retry_items, *remaining_items]
        result: dict[str, Any] = {
            "status": "continued",
            "mode": RETRIEVAL_CONTINUATION_STAGE,
            "job_object": job_object,
            "records_processed": len(stage_records),
            "retry_claims": len(retry_items),
            "remaining_claims": len(next_items),
            "stage_object": stage_object,
        }
        if next_items:
            next_job_object = _retrieval_job_object_name(
                settings["retrieval_job_prefix"], parent_run_id, sequence + 1
            )
            next_job = _retrieval_job_document(
                parent_run_id,
                parent_evidence_object,
                source_claims_object,
                sequence + 1,
                chunk_size,
                max_attempts,
                next_items,
                next_staging_objects,
            )
            _write_json_object(storage, namespace, bucket, next_job_object, next_job)
            result["next_job_object"] = next_job_object
        else:
            final_run_id, evidence_object, manifest_object, manifest, final_records = _finalize_retrieval_continuation(
                storage,
                namespace,
                bucket,
                evidence_prefix,
                parent_run_id,
                parent_evidence_object,
                source_claims_object,
                next_staging_objects,
                started_at,
            )
            _write_json_object(storage, namespace, bucket, manifest_object, manifest)
            result.update(
                {
                    "status": manifest["status"].lower(),
                    "final_run_id": final_run_id,
                    "object_name": evidence_object,
                    "manifest_object": manifest_object,
                    "status_counts": manifest["evidence_status_counts"],
                }
            )

        result["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 3)
        _write_json_object(
            storage,
            namespace,
            bucket,
            completion_object,
            {
                "schema_version": RETRIEVAL_JOB_SCHEMA_VERSION,
                "stage": RETRIEVAL_CONTINUATION_STAGE,
                "job_object": job_object,
                "result": result,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return response.Response(
            ctx,
            response_data=json.dumps(result, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - ensure Event retries expose a useful reason
        LOGGER.exception("retrieval continuation failed")
        return response.Response(
            ctx,
            response_data=json.dumps(
                {
                    "status": "error",
                    "mode": RETRIEVAL_CONTINUATION_STAGE,
                    "run_id": continuation_run_id,
                    "message": str(exc),
                }
            ),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )


def handler(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    """Dispatch PubMed evidence retrieval or its retrieval continuation."""

    mode = os.getenv("PIPELINE_MODE", "retrieve").strip().lower()
    if mode in {"retrieval_continuation", "continue_evidence_retrieval"}:
        return _handler_retrieval_continuation(ctx, data)
    if mode not in {"", "retrieve", "evidence"}:
        raise RuntimeError(
            "PIPELINE_MODE must be retrieve or retrieval_continuation"
        )

    schema = os.getenv("PIPELINE_SCHEMA", "v1").strip().lower()
    if schema in {"v1", "evidence.v1", "jsonl"}:
        return _handler_v1(ctx, data)
    if schema in {"legacy", "csv"}:
        return _handler_legacy(ctx, data)
    raise RuntimeError("PIPELINE_SCHEMA must be v1/jsonl or legacy/csv")
