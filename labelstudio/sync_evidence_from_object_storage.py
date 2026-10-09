#!/usr/bin/env python3
"""Synchronize final Evidence CSVs from OCI Object Storage into Label Studio.

The program is intended for a private Label Studio VM. It authenticates to
Object Storage with the VM's Instance Principal, accepts only the immutable
``silver/evidence/evidence_run_*.csv`` outputs, and invokes the project's
existing importer one object at a time. Processing objects in name order keeps
the newest run authoritative when a record appears in more than one run.

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


def _required(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _evidence_object_name(object_name: str, prefix: str) -> bool:
    """Accept only current terminal Evidence outputs, never legacy drains."""

    expected_prefix = f"{prefix.strip('/')}/"
    filename = PurePosixPath(object_name).name
    return (
        object_name.startswith(expected_prefix)
        and filename.startswith("evidence_run_")
        and filename.endswith(".csv")
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


def _object_client() -> Any:
    if oci is None:
        raise RuntimeError("OCI Python SDK is not installed; install oci>=2.0.0 in the VM environment")
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.object_storage.ObjectStorageClient(config={}, signer=signer)


def _list_evidence_objects(client: Any, namespace: str, bucket: str, prefix: str) -> list[Any]:
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
        (item for item in objects if _evidence_object_name(str(item.name), prefix)),
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


def _run_importer(
    importer: Path,
    csv_path: Path,
    work_dir: Path,
    url: str,
    project_id: int,
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
        str(csv_path),
        "--manifest",
        str(work_dir / "labelstudio-manifest.csv"),
    ]
    if refresh_existing:
        command.append("--refresh-existing")
    # ``import_evidence.py`` reads LABEL_STUDIO_TOKEN from its environment.
    # Do not put the token in command arguments, which can be visible in ps.
    subprocess.run(command, check=True, env=os.environ.copy())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=os.getenv("OBJECT_STORAGE_NAMESPACE", ""))
    parser.add_argument("--bucket", default=os.getenv("OBJECT_STORAGE_BUCKET", "mednews-data"))
    parser.add_argument("--prefix", default=os.getenv("OBJECT_STORAGE_PREFIX", DEFAULT_PREFIX))
    parser.add_argument("--project-id", type=int, default=int(os.getenv("LABEL_STUDIO_PROJECT_ID", "0") or "0"))
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--work-dir", type=Path, default=Path(os.getenv("MEDNEWS_SYNC_WORK_DIR", ".mednews-sync")))
    parser.add_argument("--label-config", type=Path, default=Path(__file__).with_name("config.xml"))
    parser.add_argument("--refresh-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--apply-label-config", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    namespace = _required(args.namespace, "OBJECT_STORAGE_NAMESPACE / --namespace")
    bucket = _required(args.bucket, "OBJECT_STORAGE_BUCKET / --bucket")
    token = _required(args.token, "LABEL_STUDIO_TOKEN / --token")
    if args.project_id <= 0:
        raise RuntimeError("LABEL_STUDIO_PROJECT_ID / --project-id must be positive")
    if not args.label_config.is_file():
        raise RuntimeError(f"Label Studio configuration not found: {args.label_config}")
    if not args.url.startswith(("http://", "https://")):
        raise RuntimeError("LABEL_STUDIO_URL / --url must be an HTTP(S) URL")

    work_dir = args.work_dir.expanduser().resolve()
    cache_dir = work_dir / "evidence-cache"
    state_path = work_dir / STATE_FILE_NAME
    state = _load_state(state_path)
    tracked = state.setdefault("objects", {})
    if not isinstance(tracked, dict):
        raise RuntimeError("Invalid objects entry in sync state")

    if args.apply_label_config:
        _label_studio_request(args.url, token, args.project_id, args.label_config)

    client = _object_client()
    objects = _list_evidence_objects(client, namespace, bucket, args.prefix)
    downloaded = 0
    imported = 0
    skipped = 0
    for item in objects:
        object_name = str(item.name)
        etag = str(getattr(item, "etag", "") or "")
        local_path = _local_csv_path(cache_dir, object_name)
        previous = tracked.get(object_name, {})
        if (
            isinstance(previous, dict)
            and previous.get("imported_etag") == etag
            and local_path.is_file()
        ):
            skipped += 1
            continue
        if not local_path.is_file() or not isinstance(previous, dict) or previous.get("etag") != etag:
            _download_object(client, namespace, bucket, object_name, local_path)
            downloaded += 1
        _run_importer(
            Path(__file__).with_name("import_evidence.py"),
            local_path,
            work_dir,
            args.url,
            args.project_id,
            args.refresh_existing,
        )
        tracked[object_name] = {"etag": etag, "imported_etag": etag, "local_path": local_path.name}
        _write_state(state_path, state)
        imported += 1

    print(
        json.dumps(
            {
                "status": "ok",
                "bucket": bucket,
                "prefix": args.prefix.strip("/"),
                "objects_seen": len(objects),
                "downloaded": downloaded,
                "imported_or_refreshed": imported,
                "skipped_unchanged": skipped,
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
