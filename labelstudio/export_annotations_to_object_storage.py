#!/usr/bin/env python3
"""Export the private Label Studio project to the Silver Object Storage layer.

This runs on PUB_VM because Label Studio intentionally listens only on
127.0.0.1. The long-lived token is loaded from OCI Vault through the VM's
Instance Principal and is never printed, written to an object, or passed in a
process argument.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import oci

from api_auth import authorization_header


LOGGER = logging.getLogger("labelstudio-export")
LOGGER.setLevel(logging.INFO)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _secret_value(secret_id: str) -> str:
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    client = oci.secrets.SecretsClient(config={}, signer=signer)
    bundle = client.get_secret_bundle(secret_id=secret_id, stage="CURRENT").data
    content = bundle.secret_bundle_content.content
    return base64.b64decode(content).decode("utf-8").strip()


def _refresh_access_token(url: str, refresh_token: str) -> str:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/token/refresh/",
        data=json.dumps({"refresh": refresh_token}).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Label Studio token refresh failed with HTTP {exc.code}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Label Studio token refresh returned an invalid response") from exc
    access = payload.get("access") if isinstance(payload, dict) else None
    if not isinstance(access, str) or not access.strip():
        raise RuntimeError("Label Studio token refresh did not return an access token")
    return access.strip()


def _token_candidates(url: str) -> list[tuple[str, str]]:
    """Return usable token candidates without ever logging their values.

    Vault is preferred, but the VM already has a protected refresh-token
    variable used by the existing Label Studio synchronizer.  Trying that
    second candidate makes a newly-created or mistyped Vault secret fail
    safely without taking down the daily export.  A valid Vault secret still
    remains the first choice.
    """

    configured: list[tuple[str, str]] = []
    secret_id = os.getenv("LABEL_STUDIO_SECRET_OCID", "").strip()
    if secret_id:
        try:
            secret = _secret_value(secret_id)
        except Exception as exc:  # noqa: BLE001 - continue with protected VM fallback
            LOGGER.warning("Unable to load the Label Studio Vault secret: %s", type(exc).__name__)
        else:
            if secret:
                configured.append(("vault", secret))

    for variable in ("LABEL_STUDIO_REFRESH_TOKEN", "LABEL_STUDIO_TOKEN"):
        token = os.getenv(variable, "").strip()
        if token:
            configured.append((variable, token))

    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, token in configured:
        refreshed = token
        if token.count(".") == 2:
            try:
                refreshed = _refresh_access_token(url, token)
            except RuntimeError:
                LOGGER.warning("JWT refresh failed for token source=%s; trying it directly", source)
        for candidate in (refreshed, token):
            candidate = candidate.strip()
            if candidate and candidate not in seen:
                candidates.append((source, candidate))
                seen.add(candidate)

    if not candidates:
        raise RuntimeError("LABEL_STUDIO_SECRET_OCID or a Label Studio token variable is required")
    return candidates


def _token(url: str) -> str:
    """Return the first configured token for backwards-compatible callers."""

    return _token_candidates(url)[0][1]


def _export(url: str, project_id: int, token: str) -> bytes:
    query = urllib.parse.urlencode({"exportType": "JSON", "download_all_tasks": "true"})
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/projects/{project_id}/export?{query}",
        headers={"Accept": "application/json", **authorization_header(token)},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Label Studio export failed with HTTP {exc.code}") from exc
    except OSError as exc:
        raise RuntimeError("Label Studio export request failed") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Label Studio returned an invalid JSON export") from exc
    tasks = payload if isinstance(payload, list) else payload.get("tasks", []) if isinstance(payload, dict) else []
    if not isinstance(tasks, list):
        raise RuntimeError("Label Studio export did not contain a task list")
    if not tasks:
        raise RuntimeError("Label Studio export contained no tasks")
    return raw


def _storage_client() -> Any:
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.object_storage.ObjectStorageClient(config={}, signer=signer)


def _put(client: Any, namespace: str, bucket: str, name: str, content: bytes, content_type: str) -> None:
    client.put_object(
        namespace_name=namespace,
        bucket_name=bucket,
        object_name=name,
        put_object_body=io.BytesIO(content),
        content_type=content_type,
    )


def main() -> int:
    namespace = _required("OBJECT_STORAGE_NAMESPACE")
    bucket = _required("OBJECT_STORAGE_BUCKET")
    url = _required("LABEL_STUDIO_URL")
    project_id = int(_required("LABEL_STUDIO_PROJECT_ID"))
    prefix = os.getenv("LABEL_STUDIO_EXPORT_PREFIX", "silver/annotations/labelstudio").strip("/")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client: Any | None = None
    try:
        raw: bytes | None = None
        last_error: RuntimeError | None = None
        for source, token in _token_candidates(url):
            try:
                raw = _export(url, project_id, token)
                LOGGER.info("Label Studio export authenticated with source=%s", source)
                break
            except RuntimeError as exc:
                last_error = exc
                LOGGER.warning("Label Studio token candidate failed: source=%s error=%s", source, exc)
        if raw is None:
            raise last_error or RuntimeError("Label Studio export failed for all token candidates")
        client = _storage_client()
        object_name = f"{prefix}/labelstudio_{stamp}.json"
        _put(client, namespace, bucket, object_name, raw, "application/json")
        LOGGER.info(
            "Label Studio export uploaded: object=%s bytes=%s sha256=%s",
            object_name,
            len(raw),
            hashlib.sha256(raw).hexdigest()[:16],
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - systemd must report a failed unit
        LOGGER.exception("Label Studio export failed: %s", type(exc).__name__)
        if client is not None:
            rejected_prefix = os.getenv("SILVER_REJECTED_PREFIX", "silver/rejected").strip("/")
            error_name = f"{rejected_prefix}/labelstudio-export/export_{stamp}.jsonl"
            error = {
                "stage": "labelstudio-export",
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                _put(client, namespace, bucket, error_name, (json.dumps(error, ensure_ascii=False) + "\n").encode("utf-8"), "application/jsonl")
            except Exception:  # noqa: BLE001 - preserve the original failure
                LOGGER.exception("Unable to write Label Studio export rejection artifact")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
