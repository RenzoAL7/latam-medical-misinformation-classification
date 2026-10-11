"""Extract article bodies from the Bronze backlog in bounded, idempotent batches.

Object Storage Events still wake this Function when a Bronze CSV is created,
but the handler drains the oldest pending rows across all Bronze CSVs.  A
deterministic output object is maintained per Bronze source, so retries replace
the previous attempt instead of creating a second CSV for the same articles.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import oci
from bs4 import BeautifulSoup
from fdk import response


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


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

SILVER_FIELDS = BRONZE_FIELDS + [
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

# Publishers can reject a first automated request or return a temporary
# redirect. Retrying is limited to this one invocation; later runs never pick
# up this row automatically.
RETRYABLE_HTTP_STATUS = {307, 308, 403, 408, 425, 429, 500, 502, 503, 504}
REDIRECT_HTTP_STATUS = {301, 302, 303, 307, 308}
DEFAULT_BODY_SELECTORS = (
    "[itemprop='articleBody']",
    "article",
    ".article-body",
    ".article__body",
    ".article-content",
    ".story-body",
    ".entry-content",
    ".post-content",
    "main",
)


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


def _list_csv_objects(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
) -> list[str]:
    """List CSV objects in a prefix and return them in deterministic order."""

    list_prefix = f"{prefix.strip('/')}/" if prefix.strip('/') else ""
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


def _read_csv_object(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
) -> list[dict[str, str]]:
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
    """Build a readable, collision-resistant token for a source object."""

    stem = object_name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:72] or "source"
    digest = hashlib.sha256(object_name.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}"


def _body_output_name(prefix: str, source_object: str) -> str:
    """Return the one canonical Body CSV for a Bronze source."""

    return f"{prefix.strip('/')}/body_current_{_stable_token(source_object)}.csv"


def _body_row_is_terminal(row: dict[str, str]) -> bool:
    """Return whether a Body result must not be retried automatically."""

    status = row.get("extraction_status", "").strip().upper()
    if status in {"OK", "CUERPO_INSUFICIENTE"}:
        return True
    if status == "HTTP_ERROR":
        try:
            http_status = int(row.get("http_status", "0"))
        except ValueError:
            http_status = 0
        return http_status not in RETRYABLE_HTTP_STATUS
    if status == "URL_ERROR":
        error = row.get("extraction_error", "").lower()
        return bool(error) and not any(
            marker in error
            for marker in (
                "timed out",
                "timeout",
                "temporary",
                "temporarily",
                "network",
                "connection",
                "urlopen error",
                "try again",
            )
        )
    if status in {"TIMEOUT", "PENDING", ""}:
        return False
    error = row.get("extraction_error", "").lower()
    return bool(error) and not any(
        marker in error
        for marker in (
            "time budget",
            "timeout",
            "temporarily",
            "network",
            "connection",
            "try again",
        )
    )


def _existing_body_rows(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
) -> dict[str, dict[str, str]]:
    """Read the latest Body attempt for each record_id across all output CSVs."""

    latest: dict[str, dict[str, str]] = {}
    for object_name in _list_csv_objects(client, namespace, bucket, prefix):
        for row in _read_csv_object(client, namespace, bucket, object_name):
            record_id = row.get("record_id", "").strip()
            if record_id:
                previous = latest.get(record_id)
                if previous is None or (
                    _body_row_is_terminal(row) and not _body_row_is_terminal(previous)
                ) or (_body_row_is_terminal(row) == _body_row_is_terminal(previous)):
                    latest[record_id] = row
    return latest


def _pending_rows(
    client: Any,
    namespace: str,
    bucket: str,
    bronze_prefix: str,
    silver_prefix: str,
    batch_size: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Select one bounded source batch and count the complete pending backlog."""

    existing = _existing_body_rows(client, namespace, bucket, silver_prefix)
    bronze_objects = _list_csv_objects(client, namespace, bucket, bronze_prefix)
    selected: list[dict[str, str]] = []
    selected_ids: set[str] = set()
    pending_ids: set[str] = set()
    selected_source = ""
    rows_seen = 0
    invalid_rows = 0
    skipped_terminal = 0
    duplicate_rows = 0

    for object_name in bronze_objects:
        source_pending: list[dict[str, str]] = []
        for source_row in _read_csv_object(client, namespace, bucket, object_name):
            rows_seen += 1
            record_id = source_row.get("record_id", "").strip()
            canonical_url = source_row.get("canonical_url", "").strip()
            if not record_id or not canonical_url:
                invalid_rows += 1
                continue
            previous = existing.get(record_id)
            if previous is not None and _body_row_is_terminal(previous):
                skipped_terminal += 1
                continue
            if record_id in pending_ids:
                duplicate_rows += 1
                continue
            pending_ids.add(record_id)
            row = {
                field: source_row.get(field, "").strip() for field in BRONZE_FIELDS
            }
            row["source_bronze_object"] = object_name
            source_pending.append(row)

        if source_pending and not selected_source:
            selected_source = object_name
            selected = source_pending[:batch_size]
            selected_ids = {row["record_id"] for row in selected}

    return selected, {
        "bronze_objects_scanned": len(bronze_objects),
        "bronze_rows_seen": rows_seen,
        "invalid_or_duplicate_rows": invalid_rows + duplicate_rows,
        "duplicate_rows": duplicate_rows,
        "skipped_terminal": skipped_terminal,
        "bronze_record_ids": len(pending_ids),
        "pending_rows": len(pending_ids),
        "selected_source_object": selected_source,
        "selected_rows": len(selected_ids),
    }


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

    candidates = (
        event_data.get("resourceName"),
        details.get("objectName"),
        details.get("resourceName"),
        event_data.get("objectName"),
        payload.get("source_object"),
        payload.get("object_name"),
    )
    expected_prefix = f"{prefix.strip('/')}/"
    for candidate in candidates:
        object_name = unquote(str(candidate or "")).lstrip("/")
        if object_name.startswith(expected_prefix) and object_name.endswith(".csv"):
            return object_name
    raise RuntimeError(f"Event does not identify a CSV under {expected_prefix}")


def _event_rows(
    client: Any,
    namespace: str,
    bucket: str,
    object_name: str,
    batch_size: int,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Read valid rows from one triggering Bronze CSV without cross-run scans."""

    selected: list[dict[str, str]] = []
    invalid_rows = 0
    seen_ids: set[str] = set()
    source_rows = _read_csv_object(client, namespace, bucket, object_name)
    for source_row in source_rows:
        record_id = source_row.get("record_id", "").strip()
        canonical_url = source_row.get("canonical_url", "").strip()
        if not record_id or not canonical_url or record_id in seen_ids:
            invalid_rows += 1
            continue
        seen_ids.add(record_id)
        selected.append(
            {field: source_row.get(field, "").strip() for field in BRONZE_FIELDS}
            | {"source_bronze_object": object_name}
        )
    if batch_size and len(selected) > batch_size:
        raise RuntimeError(
            f"Event object {object_name} has {len(selected)} valid rows, exceeding BODY_BATCH_SIZE={batch_size}"
        )
    return selected, {
        "source_object": object_name,
        "bronze_objects_scanned": 1,
        "bronze_rows_seen": len(source_rows),
        "invalid_or_duplicate_rows": invalid_rows,
    }


def _time_budget_row(source_row: dict[str, str], run_id: str) -> dict[str, str]:
    """Persist an explicit outcome instead of silently dropping a source row."""

    output = {field: source_row.get(field, "") for field in BRONZE_FIELDS}
    output.update(
        {
            "body": "",
            "http_status": "",
            "extraction_method": "",
            "extraction_status": "ERROR",
            "body_char_count": "0",
            "body_word_count": "0",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "source_bronze_object": source_row.get("source_bronze_object", ""),
            "extraction_run_id": run_id,
            "extraction_error": "BODY_TIME_BUDGET",
        }
    )
    return output


def _decode_html(raw: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
    encoding = charset_match.group(1) if charset_match else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _extract_body_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(
        ["script", "style", "noscript", "template", "svg", "canvas", "nav", "header", "footer", "aside", "form"]
    ):
        tag.decompose()

    candidates: list[str] = []
    for selector in DEFAULT_BODY_SELECTORS:
        for node in soup.select(selector):
            text = _normalize_text(node.get_text(" ", strip=True))
            if text:
                candidates.append(text)
    if not candidates:
        root = soup.body or soup
        fallback = _normalize_text(root.get_text(" ", strip=True))
        if fallback:
            candidates.append(fallback)
    if not candidates:
        return "", "none"
    return max(candidates, key=len), "beautifulsoup"


class _NoRedirectHandler(HTTPRedirectHandler):
    """Expose redirect responses so the extractor can validate each hop."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _redirect_target(current_url: str, location: str) -> str:
    """Resolve one HTTP redirect and reject a malformed/non-web destination."""

    target = urljoin(current_url, location.strip())
    parsed = urlsplit(target)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("redirect target must use http or https")
    return target


def _download_html(
    url: str,
    timeout: float,
    max_bytes: int,
    max_retries: int,
    max_redirects: int,
    user_agent: str,
) -> tuple[bytes, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https")

    opener = build_opener(_NoRedirectHandler())
    current_url = url
    redirects = 0
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        request = Request(
            current_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "es-419,es;q=0.9,en;q=0.5",
                "User-Agent": user_agent,
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=timeout) as result:
                raw = result.read(max_bytes + 1)
                return raw[:max_bytes], int(result.status or 200), str(
                    result.headers.get("Content-Type", "")
                )
        except HTTPError as exc:
            last_error = exc
            if exc.code in REDIRECT_HTTP_STATUS:
                location = str(exc.headers.get("Location", "")).strip()
                if not location or redirects >= max_redirects:
                    raise
                current_url = _redirect_target(current_url, location)
                redirects += 1
                # Following a redirect is not a failed retrieval attempt.
                continue
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt >= max_retries:
                raise
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= max_retries:
                raise
        time.sleep(min(2.0 ** attempt, 4.0))
    raise RuntimeError(str(last_error or "body download failed"))


def _extract_row(
    source_row: dict[str, str],
    run_id: str,
    deadline: float,
    request_timeout: float,
    max_bytes: int,
    max_retries: int,
    max_redirects: int,
    min_words: int,
    min_chars: int,
    max_text_chars: int,
    user_agent: str,
) -> dict[str, str]:
    extracted_at = datetime.now(timezone.utc).isoformat()
    output = {field: source_row.get(field, "") for field in BRONZE_FIELDS}
    output.update(
        {
            "body": "",
            "http_status": "",
            "extraction_method": "none",
            "extraction_status": "ERROR",
            "body_char_count": "0",
            "body_word_count": "0",
            "extracted_at": extracted_at,
            "source_bronze_object": source_row.get("source_bronze_object", ""),
            "extraction_run_id": run_id,
            "extraction_error": "",
        }
    )
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError("body batch time budget reached")
        body_bytes, http_status, content_type = _download_html(
            source_row["canonical_url"],
            timeout=max(1.0, min(request_timeout, max(1.0, deadline - time.monotonic()))),
            max_bytes=max_bytes,
            max_retries=max_retries,
            max_redirects=max_redirects,
            user_agent=user_agent,
        )
        html = _decode_html(body_bytes, content_type)
        body, extraction_method = _extract_body_text(html)
        body = body[:max_text_chars]
        word_count = len(re.findall(r"\b[\wÀ-ÿ]+\b", body, flags=re.UNICODE))
        char_count = len(body)
        output.update(
            {
                "body": body,
                "http_status": str(http_status),
                "extraction_method": extraction_method,
                "body_char_count": str(char_count),
                "body_word_count": str(word_count),
                "extraction_status": (
                    "OK" if word_count >= min_words and char_count >= min_chars else "CUERPO_INSUFICIENTE"
                ),
            }
        )
    except HTTPError as exc:
        output.update(
            {
                "http_status": str(exc.code),
                "extraction_status": "HTTP_ERROR",
                "extraction_error": f"HTTP {exc.code}",
            }
        )
    except TimeoutError as exc:
        output["extraction_error"] = str(exc)[:500]
        output["extraction_status"] = "TIMEOUT"
    except ValueError as exc:
        output["extraction_error"] = str(exc)[:500]
        output["extraction_status"] = "URL_ERROR"
    except (URLError, OSError) as exc:
        output["extraction_error"] = str(exc)[:500]
        output["extraction_status"] = "URL_ERROR"
    except Exception as exc:  # noqa: BLE001 - preserve one row even if a parser fails
        LOGGER.warning("Body extraction failed for %s: %s", source_row.get("record_id"), type(exc).__name__)
        output["extraction_error"] = f"{type(exc).__name__}: {str(exc)[:450]}"
    return output


def _write_silver(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
    rows: list[dict[str, str]],
    source_object: str,
) -> str:
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=SILVER_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    object_name = _body_output_name(prefix, source_object)
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(csv_buffer.getvalue().encode("utf-8-sig")),
        content_type="text/csv; charset=utf-8",
    )
    return object_name


def _body_output_prefix() -> str:
    """Return the only prefix that can trigger the next OCI Events stage.

    ``SILVER_PREFIX=silver`` was a historical setting that wrote body CSVs next
    to the folders, so the ``silver/body/*.csv`` Events rule never matched.
    Honour an explicitly scoped legacy value, but treat the generic ``silver``
    value as the safe body default.
    """

    scoped_prefix = os.getenv("SILVER_BODY_PREFIX", "").strip("/")
    if scoped_prefix:
        return scoped_prefix
    legacy_prefix = os.getenv("SILVER_PREFIX", "").strip("/")
    return legacy_prefix if legacy_prefix and legacy_prefix != "silver" else "silver/body"


def handler(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    """Process one bounded Body batch from the global Bronze backlog."""

    started = time.monotonic()
    now = datetime.now(timezone.utc)
    run_id = f"run_{now.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    try:
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        bronze_prefix = os.getenv("BRONZE_PREFIX", "bronze").strip("/")
        silver_prefix = _body_output_prefix()
        batch_size = _env_int("BODY_BATCH_SIZE", 50, minimum=1)
        max_seconds = _env_float("BODY_MAX_SECONDS", 150.0, minimum=1.0)
        time_buffer = _env_float("BODY_TIME_BUFFER", 10.0, minimum=0.0)
        request_timeout = _env_float("BODY_REQUEST_TIMEOUT", 15.0, minimum=1.0)
        max_bytes = _env_int("BODY_MAX_BYTES", 1_500_000, minimum=10_000)
        max_retries = _env_int("BODY_MAX_RETRIES", 1, minimum=0)
        max_redirects = _env_int("BODY_MAX_REDIRECTS", 5, minimum=0)
        min_words = _env_int("BODY_MIN_WORDS", 150, minimum=1)
        min_chars = _env_int("BODY_MIN_CHARS", 800, minimum=1)
        max_text_chars = _env_int("BODY_MAX_TEXT_CHARS", 200_000, minimum=1_000)
        user_agent = os.getenv(
            "BODY_USER_AGENT",
            "Mozilla/5.0 (compatible; mednews-thesis/0.2; +research contact)",
        ).strip()
        if time_buffer >= max_seconds:
            raise RuntimeError("BODY_TIME_BUFFER must be smaller than BODY_MAX_SECONDS")

        client = _object_storage_client()
        pending, scan_meta = _pending_rows(
            client,
            namespace,
            bucket,
            bronze_prefix,
            silver_prefix,
            batch_size,
        )
        source_object = str(scan_meta.get("selected_source_object", ""))
        deadline = started + max_seconds - time_buffer
        output_rows: list[dict[str, str]] = []
        for index, source_row in enumerate(pending):
            if time.monotonic() >= deadline:
                break
            output_rows.append(
                _extract_row(
                    source_row,
                    run_id,
                    deadline,
                    request_timeout,
                    max_bytes,
                    max_retries,
                    max_redirects,
                    min_words,
                    min_chars,
                    max_text_chars,
                    user_agent,
                )
            )

        object_name = ""
        if output_rows:
            canonical_name = _body_output_name(silver_prefix, source_object)
            existing_rows = (
                _read_csv_object(client, namespace, bucket, canonical_name)
                if canonical_name in _list_csv_objects(client, namespace, bucket, silver_prefix)
                else []
            )
            merged = {
                row.get("record_id", ""): row
                for row in existing_rows
                if row.get("record_id", "")
            }
            merged.update({row["record_id"]: row for row in output_rows})
            object_name = _write_silver(
                client,
                namespace,
                bucket,
                silver_prefix,
                sorted(merged.values(), key=lambda row: row.get("record_id", "")),
                source_object,
            )
        statuses = Counter(row["extraction_status"] for row in output_rows)
        terminal_written = sum(1 for row in output_rows if _body_row_is_terminal(row))
        pending_total = int(scan_meta.get("pending_rows", 0))
        result = {
            "status": "ok",
            "run_id": run_id,
            "bucket": bucket,
            "object_name": object_name,
            "source_object": source_object,
            "row_count": str(len(output_rows)),
            "selected_pending_rows": str(len(pending)),
            "rows_left_for_next_run": str(max(0, pending_total - terminal_written)),
            "ok_rows": str(statuses.get("OK", 0)),
            "insufficient_rows": str(statuses.get("CUERPO_INSUFICIENTE", 0)),
            "error_rows": str(sum(value for key, value in statuses.items() if key not in {"OK", "CUERPO_INSUFICIENTE"})),
            "elapsed_seconds": f"{time.monotonic() - started:.2f}",
            "batch_size": str(batch_size),
            "max_seconds": str(max_seconds),
            "scan": scan_meta,
        }
        LOGGER.info("Silver body batch written: %s (%s rows)", object_name or "none", len(output_rows))
        return response.Response(
            ctx,
            response_data=json.dumps(result, ensure_ascii=False),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - return a useful invocation error
        LOGGER.exception("extract-news-body failed")
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "run_id": run_id, "message": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )
