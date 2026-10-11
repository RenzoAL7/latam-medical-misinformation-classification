"""OCI Function that stores NewsData candidates in the Bronze layer.

The function deliberately keeps collection separate from later article-body
extraction and labeling. It reads the NewsData token from OCI Vault using a
resource principal and writes one Excel-compatible CSV per run to Object
Storage.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import oci
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

TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}

# Countries are requested in this order within one batch. This is deliberately
# limited to Spanish-speaking Latin America; Spain and Equatorial Guinea are
# outside the thesis collection scope. Puerto Rico is included as a separate
# territory so its coverage is visible in the audit.
LATAM_COUNTRY_CODES = frozenset(
    {
        "ar",
        "bo",
        "cl",
        "co",
        "cr",
        "cu",
        "do",
        "ec",
        "gt",
        "hn",
        "mx",
        "ni",
        "pa",
        "pe",
        "pr",
        "py",
        "sv",
        "uy",
        "ve",
    }
)

DEFAULT_COUNTRY_SEQUENCE = (
    "ar",
    "bo",
    "cl",
    "co",
    "cr",
    "cu",
    "do",
    "ec",
    "gt",
    "hn",
    "mx",
    "ni",
    "pa",
    "pe",
    "pr",
    "py",
    "sv",
    "uy",
    "ve",
)

COUNTRY_NAME_ALIASES = {
    "ar": ("ar", "argentina"),
    "bo": ("bo", "bolivia", "bolivia plurinational state of"),
    "cl": ("cl", "chile"),
    "co": ("co", "colombia"),
    "cr": ("cr", "costa rica"),
    "cu": ("cu", "cuba"),
    "do": ("do", "dominican republic", "republica dominicana"),
    "ec": ("ec", "ecuador"),
    "gt": ("gt", "guatemala"),
    "hn": ("hn", "honduras"),
    "mx": ("mx", "mexico"),
    "ni": ("ni", "nicaragua"),
    "pa": ("pa", "panama"),
    "pe": ("pe", "peru"),
    "pr": ("pr", "puerto rico"),
    "py": ("py", "paraguay"),
    "sv": ("sv", "el salvador"),
    "uy": ("uy", "uruguay"),
    "ve": ("ve", "venezuela"),
}


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


def _secret_value(secret_id: str) -> str:
    signer = oci.auth.signers.get_resource_principals_signer()
    client = oci.secrets.SecretsClient(config={}, signer=signer)
    bundle = client.get_secret_bundle(secret_id=secret_id, stage="CURRENT").data
    content = bundle.secret_bundle_content.content
    return base64.b64decode(content).decode("utf-8").strip()


def _secret_ids() -> list[str]:
    """Read one or more Vault secret OCIDs without exposing their values."""

    configured = os.getenv("NEWSDATA_SECRET_OCIDS", "").strip()
    if configured:
        values = [value.strip() for value in configured.split(",") if value.strip()]
    else:
        values = [_required_env("NEWSDATA_SECRET_OCID")]
    unique_values = list(dict.fromkeys(values))
    if not unique_values:
        raise RuntimeError("At least one NewsData Vault secret OCID is required")
    return unique_values


def _newsdata_api_keys() -> list[str]:
    """Load all usable NewsData keys, allowing one secret to fail over to another."""

    keys: list[str] = []
    for slot, secret_id in enumerate(_secret_ids(), start=1):
        try:
            value = _secret_value(secret_id)
        except Exception as exc:  # noqa: BLE001 - keep another key available
            LOGGER.warning(
                "Unable to read NewsData secret slot %s: %s",
                slot,
                type(exc).__name__,
            )
            continue
        if not value:
            LOGGER.warning("NewsData secret slot %s is empty", slot)
            continue
        if value not in keys:
            keys.append(value)
    if not keys:
        raise RuntimeError("No usable NewsData Vault secret was found")
    LOGGER.info("Loaded %s NewsData key slot(s)", len(keys))
    return keys


def _canonicalize_url(raw_url: str) -> str:
    """Remove tracking parameters so the same article gets one record_id."""

    parsed = urlsplit(raw_url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid article URL")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
    ]
    hostname = parsed.hostname.lower()
    netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", urlencode(query), ""))


def _record_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]


def _list_value(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _normalize_country_value(value: str) -> str:
    """Normalize country labels without losing the ISO code mapping."""

    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char)).strip()


def _country_code_from_value(value: str) -> str:
    """Map NewsData's country code/name to one of the allowed ISO-2 codes."""

    normalized_values = {
        _normalize_country_value(part)
        for part in re.split(r"[,;/|]", value)
        if str(part).strip()
    }
    for code in DEFAULT_COUNTRY_SEQUENCE:
        if normalized_values.intersection(COUNTRY_NAME_ALIASES[code]):
            return code
    return ""


def _country_sequence() -> list[str]:
    """Return the ordered country cycle used by automatic collection."""

    configured = os.getenv("NEWSDATA_COUNTRY_SEQUENCE", "").strip()
    values = configured.split(",") if configured else list(DEFAULT_COUNTRY_SEQUENCE)
    sequence = [value.strip().lower() for value in values if value.strip()]
    if not sequence:
        raise RuntimeError("NEWSDATA_COUNTRY_SEQUENCE cannot be empty")
    if any(len(value) != 2 for value in sequence):
        raise RuntimeError("NEWSDATA_COUNTRY_SEQUENCE must contain ISO-2 country codes")
    invalid = sorted(set(sequence) - LATAM_COUNTRY_CODES)
    if invalid:
        raise RuntimeError(
            "NEWSDATA_COUNTRY_SEQUENCE may contain only Spanish LATAM codes: "
            + ", ".join(sorted(LATAM_COUNTRY_CODES))
            + f"; invalid: {', '.join(invalid)}"
        )
    if len(set(sequence)) != len(sequence):
        raise RuntimeError("NEWSDATA_COUNTRY_SEQUENCE cannot contain duplicate country codes")
    return sequence


def _country_rank(row: dict[str, str], sequence: list[str]) -> int:
    """Sort API results into the configured country order for the CSV."""

    country_code = _country_code_from_value(row.get("country", ""))
    for index, code in enumerate(sequence):
        if country_code == code:
            return index
    return len(sequence)


def _object_storage_client() -> Any:
    signer = oci.auth.signers.get_resource_principals_signer()
    return oci.object_storage.ObjectStorageClient(config={}, signer=signer)


def _bronze_rows(
    payload: dict[str, Any],
    retrieved_at: str,
    allowed_countries: set[str] | None = None,
) -> list[dict[str, str]]:
    """Map API articles to the ten columns used by the thesis Bronze sheet."""

    rows: list[dict[str, str]] = []
    seen_record_ids: set[str] = set()
    results = payload.get("results") or []
    if not isinstance(results, list):
        return rows

    for item in results:
        if not isinstance(item, dict):
            continue
        language = str(item.get("language") or "").strip().casefold()
        if language not in {"es", "spanish"}:
            continue
        category_text = _list_value(item.get("category")).casefold()
        categories = {value.strip() for value in category_text.split(",") if value.strip()}
        if "health" not in categories:
            continue
        country_code = _country_code_from_value(_list_value(item.get("country")))
        if not country_code or country_code not in LATAM_COUNTRY_CODES:
            continue
        if allowed_countries is not None and country_code not in allowed_countries:
            continue

        raw_url = str(item.get("link") or "").strip()
        if not raw_url:
            continue
        try:
            canonical_url = _canonicalize_url(raw_url)
        except ValueError:
            continue
        record_id = _record_id(canonical_url)
        if record_id in seen_record_ids:
            continue
        seen_record_ids.add(record_id)

        rows.append(
            {
                "record_id": record_id,
                "source_name": str(item.get("source_name") or "").strip(),
                "canonical_url": canonical_url,
                "published_at": str(item.get("pubDate") or item.get("pub_date") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "subtitle_or_bajada": str(item.get("description") or "").strip(),
                "topic": "health",
                "selection_status": "PENDIENTE",
                "retrieved_at": retrieved_at,
                "country": country_code,
            }
        )
    return rows


def _existing_record_ids(
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
) -> set[str]:
    """Read prior Bronze CSVs so a later 48-hour run cannot repeat records."""

    record_ids: set[str] = set()
    list_prefix = f"{prefix}/" if prefix else ""
    start: str | None = None

    while True:
        response = client.list_objects(
            namespace_name=namespace,
            bucket_name=bucket,
            prefix=list_prefix,
            start=start,
            fields="name",
            limit=1000,
        )
        for item in response.data.objects:
            object_name = str(item.name)
            if not object_name.endswith(".csv"):
                continue
            object_response = client.get_object(
                namespace_name=namespace,
                bucket_name=bucket,
                object_name=object_name,
            )
            content = object_response.data.content
            if hasattr(content, "read"):
                content = content.read()
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            for row in reader:
                record_id = str(row.get("record_id") or "").strip()
                if record_id:
                    record_ids.add(record_id)

        start = response.data.next_start_with
        if not start:
            break
    return record_ids


def _request_newsdata(
    api_key: str,
    requested_country: str | None = None,
    deadline: float | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    endpoint = os.getenv("NEWSDATA_ENDPOINT", "https://newsdata.io/api/1/latest").strip()
    params: dict[str, str] = {
        "apikey": api_key,
        "language": os.getenv("NEWSDATA_LANGUAGE", "es").strip(),
        "category": os.getenv("NEWSDATA_CATEGORY", "health").strip(),
        "size": os.getenv("NEWSDATA_SIZE", "10").strip(),
        "removeduplicate": os.getenv("NEWSDATA_REMOVEDUPLICATE", "1").strip(),
        "video": os.getenv("NEWSDATA_VIDEO", "0").strip(),
    }
    query = os.getenv(
        "NEWSDATA_QUERY",
        "(salud OR medicina OR médico OR enfermedad OR vacuna OR tratamiento "
        "OR cáncer OR diabetes)",
    ).strip()
    if len(query) > 100:
        raise RuntimeError("NEWSDATA_QUERY cannot be longer than 100 characters")
    country = (
        requested_country.strip().lower()
        if requested_country is not None
        else os.getenv("NEWSDATA_COUNTRY", "").strip().lower()
    )
    if query:
        params["q"] = query
    if country:
        params["country"] = country

    try:
        max_pages = int(os.getenv("NEWSDATA_MAX_PAGES", "1"))
    except ValueError as exc:
        raise RuntimeError("NEWSDATA_MAX_PAGES must be an integer") from exc
    if not 1 <= max_pages <= 100:
        raise RuntimeError("NEWSDATA_MAX_PAGES must be between 1 and 100")

    try:
        timeout = float(os.getenv("NEWSDATA_TIMEOUT", "30"))
    except ValueError as exc:
        raise RuntimeError("NEWSDATA_TIMEOUT must be a number") from exc
    try:
        page_delay = float(os.getenv("NEWSDATA_PAGE_DELAY", "2"))
        max_retries = int(os.getenv("NEWSDATA_MAX_RETRIES", "3"))
    except ValueError as exc:
        raise RuntimeError("NEWSDATA_PAGE_DELAY and NEWSDATA_MAX_RETRIES must be numeric") from exc
    if page_delay < 0 or max_retries < 0:
        raise RuntimeError("NEWSDATA_PAGE_DELAY and NEWSDATA_MAX_RETRIES cannot be negative")

    all_results: list[Any] = []
    first_payload: dict[str, Any] | None = None
    next_page: str | None = None
    pages_fetched = 0
    last_status = ""

    while pages_fetched < max_pages:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("NewsData batch time budget reached")
        if pages_fetched and page_delay:
            remaining = deadline - time.monotonic() if deadline is not None else page_delay
            if remaining <= 0:
                raise TimeoutError("NewsData batch time budget reached")
            time.sleep(min(page_delay, remaining))
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("NewsData batch time budget reached")
        page_params = dict(params)
        if next_page:
            page_params["page"] = next_page
        request = Request(
            f"{endpoint}?{urlencode(page_params)}",
            headers={"Accept": "application/json", "User-Agent": "mednews-oci/0.1"},
            method="GET",
        )
        for attempt in range(max_retries + 1):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("NewsData batch time budget reached")
                request_timeout = min(timeout, max(0.1, remaining))
            else:
                request_timeout = timeout
            try:
                with urlopen(request, timeout=request_timeout) as result:
                    raw = result.read()
                    last_status = str(result.status)
                break
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code != 429 or attempt >= max_retries:
                    raise RuntimeError(f"NewsData returned HTTP {exc.code}: {body[:500]}") from exc
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    retry_delay = max(float(retry_after), 2.0 ** (attempt + 1))
                except ValueError:
                    retry_delay = 2.0 ** (attempt + 1)
                LOGGER.warning(
                    "NewsData rate limit on page %s; retrying in %.1f seconds",
                    pages_fetched + 1,
                    retry_delay,
                )
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= retry_delay:
                        raise TimeoutError("NewsData batch time budget reached")
                time.sleep(retry_delay)
            except TimeoutError as exc:
                if "batch time budget" in str(exc).lower() or (
                    deadline is not None and time.monotonic() >= deadline
                ):
                    raise
                raise RuntimeError(f"NewsData request timed out: {exc}") from exc
            except URLError as exc:
                raise RuntimeError(f"NewsData request failed: {exc.reason}") from exc

        try:
            page_payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("NewsData returned a non-JSON response") from exc
        if not isinstance(page_payload, dict):
            raise RuntimeError("NewsData returned an unexpected JSON shape")
        api_status = str(page_payload.get("status") or "").strip().lower()
        if api_status in {"error", "failed"}:
            details = page_payload.get("results") or page_payload.get("message")
            if isinstance(details, dict):
                details = details.get("message") or details.get("error") or details
            raise RuntimeError(f"NewsData API error: {str(details)[:500]}")
        if first_payload is None:
            first_payload = page_payload

        page_results = page_payload.get("results") or []
        if isinstance(page_results, list):
            all_results.extend(page_results)
        pages_fetched += 1
        next_page_value = page_payload.get("nextPage")
        next_page = str(next_page_value).strip() if next_page_value else None
        if not next_page:
            break

    payload = dict(first_payload or {})
    payload["results"] = all_results
    safe_params = {key: value for key, value in params.items() if key != "apikey"}
    safe_params.update(
        {
            "endpoint": endpoint,
            "requested_country": country,
            "http_status": last_status,
            "max_pages": str(max_pages),
            "pages_fetched": str(pages_fetched),
            "articles_received": str(len(all_results)),
            "next_page_available": str(bool(next_page)).lower(),
            "page_delay_seconds": str(page_delay),
            "max_retries": str(max_retries),
            "api_status": str((first_payload or {}).get("status") or ""),
            "total_results": str((first_payload or {}).get("totalResults") or ""),
        }
    )
    return payload, safe_params


def _request_newsdata_with_failover(
    api_keys: list[str],
    requested_country: str,
    key_start_index: int,
    deadline: float | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Try the assigned key first, then fail over to another configured key."""

    errors: list[str] = []
    for offset in range(len(api_keys)):
        key_slot = (key_start_index + offset) % len(api_keys)
        try:
            payload, request_meta = _request_newsdata(
                api_keys[key_slot],
                requested_country=requested_country,
                deadline=deadline,
            )
        except RuntimeError as exc:
            errors.append(f"slot {key_slot + 1}: {str(exc)[:240]}")
            LOGGER.warning(
                "NewsData request failed with key slot %s; trying failover",
                key_slot + 1,
            )
            continue
        request_meta["key_slot"] = str(key_slot + 1)
        return payload, request_meta
    raise RuntimeError("All NewsData key slots failed: " + " | ".join(errors))


def _collect_country_batch(
    api_keys: list[str],
    client: Any,
    namespace: str,
    bucket: str,
    prefix: str,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Collect one ordered country page at a time until the batch is full."""

    started = time.monotonic()
    try:
        target_rows = int(os.getenv("NEWSDATA_TARGET_ROWS", "50"))
    except ValueError as exc:
        raise RuntimeError("NEWSDATA_TARGET_ROWS must be an integer") from exc
    if target_rows < 1:
        raise RuntimeError("NEWSDATA_TARGET_ROWS must be greater than zero")

    try:
        country_group_size = int(os.getenv("NEWSDATA_COUNTRY_GROUP_SIZE", "5"))
    except ValueError as exc:
        raise RuntimeError("NEWSDATA_COUNTRY_GROUP_SIZE must be an integer") from exc
    if not 1 <= country_group_size <= 5:
        raise RuntimeError("NEWSDATA_COUNTRY_GROUP_SIZE must be between 1 and 5")

    try:
        country_delay = float(os.getenv("NEWSDATA_COUNTRY_DELAY", "2"))
    except ValueError as exc:
        raise RuntimeError("NEWSDATA_COUNTRY_DELAY must be a number") from exc
    if country_delay < 0:
        raise RuntimeError("NEWSDATA_COUNTRY_DELAY cannot be negative")
    max_seconds = _env_float("NEWSDATA_MAX_SECONDS", 150.0, minimum=30.0)
    time_buffer = _env_float("NEWSDATA_TIME_BUFFER", 10.0, minimum=0.0)
    if time_buffer >= max_seconds:
        raise RuntimeError("NEWSDATA_TIME_BUFFER must be smaller than NEWSDATA_MAX_SECONDS")
    deadline = time.monotonic() + max_seconds - time_buffer

    dedup_enabled = os.getenv("BRONZE_DEDUP_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    existing_record_ids = (
        _existing_record_ids(client, namespace, bucket, prefix) if dedup_enabled else set()
    )
    seen_record_ids = set(existing_record_ids)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, str]] = []
    country_stats: list[dict[str, Any]] = []
    countries = _country_sequence()
    country_groups = [
        countries[index : index + country_group_size]
        for index in range(0, len(countries), country_group_size)
    ]
    candidates_seen = 0
    duplicates_skipped = 0

    for group_index, country_group in enumerate(country_groups):
        if len(rows) >= target_rows:
            break
        if time.monotonic() >= deadline:
            break

        requested_country = ",".join(country_group)
        try:
            payload, request_meta = _request_newsdata_with_failover(
                api_keys,
                requested_country=requested_country,
                key_start_index=group_index % len(api_keys),
                deadline=deadline,
            )
        except TimeoutError:
            LOGGER.warning("NewsData collection stopped at the internal time budget")
            break
        candidates = _bronze_rows(payload, retrieved_at, set(country_group))
        country_candidates = len(candidates)
        country_duplicates = 0
        country_new = 0
        candidates = sorted(candidates, key=lambda row: _country_rank(row, countries))

        for row in candidates:
            candidates_seen += 1
            if row["record_id"] in seen_record_ids:
                country_duplicates += 1
                duplicates_skipped += 1
                continue
            seen_record_ids.add(row["record_id"])
            rows.append(row)
            country_new += 1
            if len(rows) >= target_rows:
                break

        country_stats.append(
            {
                "countries": country_group,
                "group_index": group_index,
                "candidate_rows": country_candidates,
                "new_rows": country_new,
                "duplicates_skipped": country_duplicates,
                "request": request_meta,
            }
        )
        if len(rows) >= target_rows:
            break
        if country_delay and group_index < len(country_groups) - 1:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(country_delay, remaining))

    rows.sort(key=lambda row: _country_rank(row, countries))
    countries_attempted = [
        country
        for group in country_stats
        for country in group["countries"]
    ]

    request_meta = {
        "mode": "grouped_ordered_country_batch",
        "target_rows": target_rows,
        "max_seconds": max_seconds,
        "time_buffer_seconds": time_buffer,
        "time_budget_reached": time.monotonic() >= deadline,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "country_group_size": country_group_size,
        "countries_configured": countries,
        "countries_attempted": countries_attempted,
        "groups_attempted": len(country_stats),
        "key_slots_configured": len(api_keys),
        "key_slots_used": sorted(
            {
                item["request"].get("key_slot", "")
                for item in country_stats
                if item["request"].get("key_slot")
            }
        ),
        "requests_made": sum(
            int(item["request"].get("pages_fetched", "0")) for item in country_stats
        ),
        "candidate_rows": candidates_seen,
        "new_rows": len(rows),
        "duplicates_skipped": duplicates_skipped,
        "existing_record_ids": len(existing_record_ids),
        "dedup_enabled": dedup_enabled,
        "country_stats": country_stats,
    }
    return rows, request_meta


def _write_bronze(
    rows: list[dict[str, str]],
    request_meta: dict[str, Any],
    client: Any,
) -> dict[str, str]:
    namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
    bucket = _required_env("OBJECT_STORAGE_BUCKET")
    prefix = os.getenv("BRONZE_PREFIX", "bronze").strip("/")
    retrieved_at = datetime.now(timezone.utc)
    run_id = f"run_{retrieved_at.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    if not rows:
        return {
            "run_id": run_id,
            "bucket": bucket,
            "object_name": "",
            "row_count": "0",
            "candidate_row_count": str(request_meta["candidate_rows"]),
            "duplicates_skipped": str(request_meta["duplicates_skipped"]),
            "existing_record_ids": str(request_meta["existing_record_ids"]),
            "dedup_enabled": str(request_meta["dedup_enabled"]).lower(),
            "countries_attempted": ",".join(request_meta["countries_attempted"]),
            "write_skipped": "empty_batch",
            "request_meta": json.dumps(request_meta, ensure_ascii=False),
        }
    # The layer is already encoded by ``prefix``.  Keep the object name short
    # and stable so a bucket listing is readable across pipeline stages.
    object_name = f"{prefix}/newsdata_{run_id}.csv"

    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=BRONZE_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    encoded = csv_buffer.getvalue().encode("utf-8-sig")

    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=object_name,
        put_object_body=io.BytesIO(encoded),
        content_type="text/csv; charset=utf-8",
    )
    return {
        "run_id": run_id,
        "bucket": bucket,
        "object_name": object_name,
        "row_count": str(len(rows)),
        "candidate_row_count": str(request_meta["candidate_rows"]),
        "duplicates_skipped": str(request_meta["duplicates_skipped"]),
        "existing_record_ids": str(request_meta["existing_record_ids"]),
        "dedup_enabled": str(request_meta["dedup_enabled"]).lower(),
        "countries_attempted": ",".join(request_meta["countries_attempted"]),
        "write_skipped": "",
        "request_meta": json.dumps(request_meta, ensure_ascii=False),
    }


def handler(ctx: Any, data: io.BytesIO | None = None) -> response.Response:
    """Fetch ordered country pages and write one deduplicated Bronze CSV."""

    try:
        api_keys = _newsdata_api_keys()
        namespace = _required_env("OBJECT_STORAGE_NAMESPACE")
        bucket = _required_env("OBJECT_STORAGE_BUCKET")
        prefix = os.getenv("BRONZE_PREFIX", "bronze").strip("/")
        client = _object_storage_client()
        rows, request_meta = _collect_country_batch(
            api_keys,
            client,
            namespace,
            bucket,
            prefix,
        )
        require_target = os.getenv(
            "NEWSDATA_REQUIRE_TARGET_ROWS", "0"
        ).strip().lower() in {"1", "true", "yes"}
        if require_target and len(rows) < int(request_meta["target_rows"]):
            summary = {
                "new_rows": len(rows),
                "target_rows": request_meta["target_rows"],
                "requests_made": request_meta["requests_made"],
                "candidate_rows": request_meta["candidate_rows"],
                "duplicates_skipped": request_meta["duplicates_skipped"],
                "key_slots_configured": request_meta["key_slots_configured"],
            }
            raise RuntimeError(
                "NewsData returned only "
                f"{len(rows)} new unique rows; target is {request_meta['target_rows']}. "
                "No incomplete Bronze CSV was written. Details: "
                f"{json.dumps(summary, ensure_ascii=False)}"
            )
        result = _write_bronze(rows, request_meta, client)
        result_status = "ok" if result["object_name"] else "no_new_rows"
        LOGGER.info(
            "Bronze batch completed: %s (%s rows)",
            result["object_name"] or "no object written",
            result["row_count"],
        )
        return response.Response(
            ctx,
            response_data=json.dumps({"status": result_status, **result}),
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 - the invocation must return a useful error
        LOGGER.exception("fetch-newsdata failed")
        return response.Response(
            ctx,
            response_data=json.dumps({"status": "error", "message": str(exc)}),
            status_code=500,
            headers={"Content-Type": "application/json"},
        )
