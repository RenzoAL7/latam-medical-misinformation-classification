#!/usr/bin/env python3
"""Synchronize final Evidence artifacts from OCI Object Storage into Label Studio.

The program is intended for a private Label Studio VM. It authenticates to
Object Storage with the VM's Instance Principal, accepts only the immutable
``silver/evidence/run_id=*/manifest.json`` outputs for the multi-claim JSONL
contract. Manifests can be ``COMPLETE`` or ``PARTIAL``. The importer accepts
claims with a valid LLM extraction and either complete evidence processing or
a terminal PubMed miss (``NO_RESULTS``/``NO_ABSTRACT``) so a human can triage
their materiality. Transient evidence failures remain excluded.

The Label Studio API token is read only from ``LABEL_STUDIO_TOKEN`` at runtime;
it is never written to the state file or committed to the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from api_auth import authorization_header

try:
    import oci
except ModuleNotFoundError:  # Allows lightweight unit tests without OCI SDK.
    oci = None  # type: ignore[assignment]


DEFAULT_PREFIX = "silver/evidence"
STATE_FILE_NAME = "sync-state.json"
REVIEWABLE_MANIFEST_STATUSES = frozenset({"COMPLETE", "PARTIAL"})


def _required(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _evidence_manifest_object_name(object_name: str, prefix: str) -> bool:
    """Accept only immutable v1 manifests, never a data object by itself."""

    expected_prefix = f"{prefix.strip('/')}/"
    return (
        object_name.startswith(expected_prefix)
        and object_name.endswith("/manifest.json")
        and "/run_id=" in object_name
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "objects": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid sync state file: {path}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("objects"), dict):
        raise RuntimeError(f"Invalid sync state structure: {path}")
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(path)


def _local_csv_path(cache_dir: Path, object_name: str) -> Path:
    """Keep readable names while preventing collisions across object paths."""

    import hashlib

    digest = hashlib.sha256(object_name.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}-{PurePosixPath(object_name).name}"


def _local_input_path(cache_dir: Path, object_name: str) -> Path:
    return _local_csv_path(cache_dir, object_name)


def _object_client() -> Any:
    if oci is None:
        raise RuntimeError("OCI Python SDK is not installed; install oci>=2.0.0 in the VM environment")
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.object_storage.ObjectStorageClient(config={}, signer=signer)


def _list_evidence_manifest_objects(client: Any, namespace: str, bucket: str, prefix: str) -> list[Any]:
    if oci is None:
        raise RuntimeError("OCI Python SDK is not installed")
    response = oci.pagination.list_call_get_all_results(
        client.list_objects,
        namespace,
        bucket,
        prefix=f"{prefix.strip('/')}/",
        fields="name,etag",
    )
    objects = getattr(response.data, "objects", []) or []
    return sorted(
        (item for item in objects if _evidence_manifest_object_name(str(item.name), prefix)),
        key=lambda item: str(item.name),
    )


def _download_object(client: Any, namespace: str, bucket: str, object_name: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = client.get_object(namespace, bucket, object_name)
    with tempfile.NamedTemporaryFile(
        "wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as handle:
        content = response.data.content
        if not isinstance(content, (bytes, bytearray)):
            raise RuntimeError(f"Object Storage returned invalid content for {object_name}")
        handle.write(content)
        temporary_path = Path(handle.name)
    temporary_path.replace(destination)


def _read_v1_manifest(path: Path, prefix: str) -> tuple[str, str]:
    """Return a validated Evidence JSONL reference and its run status."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid evidence manifest: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "evidence.manifest.v1":
        raise RuntimeError(f"Unsupported evidence manifest schema: {path}")
    status = str(payload.get("status", "")).upper()
    evidence_object = str(payload.get("evidence_object", "")).lstrip("/")
    expected_prefix = f"{prefix.strip('/')}/"
    if not evidence_object.startswith(expected_prefix) or not evidence_object.endswith("/evidence.jsonl"):
        raise RuntimeError(f"Evidence manifest references an invalid object: {evidence_object!r}")
    return status, evidence_object


def _manifest_has_reviewable_rows(status: str) -> bool:
    """Whether a manifest may contain claim rows safe for human review."""

    return status.strip().upper() in REVIEWABLE_MANIFEST_STATUSES


def _manifest_was_handled(previous: object, etag: str) -> bool:
    """Do not re-import a run, but reconsider an old skipped PARTIAL run.

    Earlier releases stored ``observed_etag`` for every non-COMPLETE manifest.
    Keeping that value out of this decision lets the upgraded synchronizer
    recover ready claims from an already-observed PARTIAL run exactly once.
    """

    if not isinstance(previous, dict):
        return False
    return previous.get("imported_etag") == etag or previous.get("skipped_etag") == etag


def _label_studio_request(url: str, token: str, project_id: int, label_config: Path) -> None:
    """Converge the project interface before creating or refreshing tasks."""

    payload = json.dumps({"label_config": label_config.read_text(encoding="utf-8")}).encode("utf-8")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/projects/{project_id}",
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            **authorization_header(token),
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=60):
            return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Unable to update Label Studio project configuration: HTTP {exc.code}: {detail}") from exc


def _refresh_access_token(url: str, refresh_token: str) -> str:
    """Exchange a long-lived Label Studio refresh token for a short-lived access token."""

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
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Unable to refresh Label Studio access token: HTTP {exc.code}: {detail}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Label Studio returned an invalid token-refresh response") from exc

    access_token = payload.get("access") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("Label Studio token-refresh response did not contain an access token")
    return access_token.strip()


def _run_importer(
    importer: Path,
    input_path: Path,
    work_dir: Path,
    url: str,
    project_id: int,
    token: str,
    refresh_existing: bool,
) -> None:
    command = [
        sys.executable,
        str(importer),
        "--url",
        url,
        "--project-id",
        str(project_id),
        "--input",
        str(input_path),
        "--manifest",
        str(work_dir / "labelstudio-manifest.csv"),
    ]
    if refresh_existing:
        command.append("--refresh-existing")
    # ``import_evidence.py`` reads LABEL_STUDIO_TOKEN from its environment.
    # Do not put the token in command arguments, which can be visible in ps.
    environment = os.environ.copy()
    environment["LABEL_STUDIO_TOKEN"] = token
    environment.pop("LABEL_STUDIO_REFRESH_TOKEN", None)
    subprocess.run(command, check=True, env=environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=os.getenv("OBJECT_STORAGE_NAMESPACE", ""))
    parser.add_argument("--bucket", default=os.getenv("OBJECT_STORAGE_BUCKET", "mednews-data"))
    parser.add_argument("--prefix", default=os.getenv("OBJECT_STORAGE_PREFIX", DEFAULT_PREFIX))
    parser.add_argument("--project-id", type=int, default=int(os.getenv("LABEL_STUDIO_PROJECT_ID", "0") or "0"))
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--refresh-token", default=os.getenv("LABEL_STUDIO_REFRESH_TOKEN", ""))
    parser.add_argument("--work-dir", type=Path, default=Path(os.getenv("MEDNEWS_SYNC_WORK_DIR", ".mednews-sync")))
    parser.add_argument("--label-config", type=Path, default=None)
    parser.add_argument("--refresh-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--apply-label-config", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    namespace = _required(args.namespace, "OBJECT_STORAGE_NAMESPACE / --namespace")
    bucket = _required(args.bucket, "OBJECT_STORAGE_BUCKET / --bucket")
    if args.project_id <= 0:
        raise RuntimeError("LABEL_STUDIO_PROJECT_ID / --project-id must be positive")
    label_config = args.label_config or Path(__file__).with_name("config_multiclaim.xml")
    if not label_config.is_file():
        raise RuntimeError(f"Label Studio configuration not found: {label_config}")
    if not args.url.startswith(("http://", "https://")):
        raise RuntimeError("LABEL_STUDIO_URL / --url must be an HTTP(S) URL")

    token = (
        _refresh_access_token(args.url, args.refresh_token.strip())
        if args.refresh_token.strip()
        else _required(args.token, "LABEL_STUDIO_TOKEN / --token")
    )

    work_dir = args.work_dir.expanduser().resolve()
    cache_dir = work_dir / "evidence-cache"
    state_path = work_dir / STATE_FILE_NAME
    state = _load_state(state_path)
    tracked = state.setdefault("objects", {})
    if not isinstance(tracked, dict):
        raise RuntimeError("Invalid objects entry in sync state")

    if args.apply_label_config:
        _label_studio_request(args.url, token, args.project_id, label_config)

    client = _object_client()
    downloaded = 0
    imported = 0
    skipped = 0
    skipped_incomplete = 0
    partial_manifests_imported = 0
    objects = _list_evidence_manifest_objects(client, namespace, bucket, args.prefix)
    for item in objects:
        object_name = str(item.name)
        etag = str(getattr(item, "etag", "") or "")
        previous = tracked.get(object_name, {})
        manifest_path = _local_input_path(cache_dir / "manifests", object_name)
        if (
            isinstance(previous, dict)
            and _manifest_was_handled(previous, etag)
            and manifest_path.is_file()
        ):
            skipped += 1
            continue
        if not manifest_path.is_file() or not isinstance(previous, dict) or previous.get("etag") != etag:
            _download_object(client, namespace, bucket, object_name, manifest_path)
            downloaded += 1
        status, evidence_object = _read_v1_manifest(manifest_path, args.prefix)
        if not _manifest_has_reviewable_rows(status):
            tracked[object_name] = {
                "etag": etag,
                "observed_etag": etag,
                "skipped_etag": etag,
                "manifest_status": status,
                "local_path": manifest_path.name,
            }
            _write_state(state_path, state)
            skipped_incomplete += 1
            continue
        evidence_path = _local_input_path(cache_dir / "jsonl", evidence_object)
        if not evidence_path.is_file() or not isinstance(previous, dict) or previous.get("etag") != etag:
            _download_object(client, namespace, bucket, evidence_object, evidence_path)
            downloaded += 1
        _run_importer(
            Path(__file__).with_name("import_evidence.py"),
            evidence_path,
            work_dir,
            args.url,
            args.project_id,
            token,
            args.refresh_existing,
        )
        tracked[object_name] = {
            "etag": etag,
            "imported_etag": etag,
            "manifest_status": status,
            "evidence_object": evidence_object,
            "local_path": manifest_path.name,
        }
        _write_state(state_path, state)
        imported += 1
        partial_manifests_imported += int(status == "PARTIAL")

    print(
        json.dumps(
            {
                "status": "ok",
                "bucket": bucket,
                "prefix": args.prefix.strip("/"),
                "format": "jsonl",
                "objects_seen": len(objects),
                "downloaded": downloaded,
                "imported_or_refreshed": imported,
                "skipped_unchanged": skipped,
                "skipped_incomplete": skipped_incomplete,
                "partial_manifests_imported": partial_manifests_imported,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - systemd journal needs a concise failure reason
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc
