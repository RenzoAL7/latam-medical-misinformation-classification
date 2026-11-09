#!/usr/bin/env python3
"""Importa a Label Studio los lotes nuevos guardados en silver/labelstudio/.

Corre en la VM con Instance Principal. Un lote cuenta como listo cuando existe su
manifest.json. No toca la configuración del proyecto ni salta el control de
duplicados: una noticia cuyo record_id ya exista en el proyecto no se vuelve a subir.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import oci

csv.field_size_limit(10_000_000)

MANIFEST_RE = re.compile(r"^silver/labelstudio/([^/]+)/manifest\.json$")
PAGE_SIZE = 500


def api(base: str, path: str, token: str | None, payload: Any = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"null")


def existing_record_ids(base: str, token: str, project: int) -> set[str]:
    found: set[str] = set()
    page = 1
    while True:
        try:
            tasks = api(base, f"/api/projects/{project}/tasks/?page={page}&page_size={PAGE_SIZE}", token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # la API responde 404 al pasar de la última página
                return found
            raise
        items = tasks["tasks"] if isinstance(tasks, dict) else tasks
        found.update(str(t["data"]["record_id"]) for t in items if t.get("data", {}).get("record_id"))
        if len(items) < PAGE_SIZE:
            return found
        page += 1


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    namespace = os.environ["OBJECT_STORAGE_NAMESPACE"]
    bucket = os.getenv("OBJECT_STORAGE_BUCKET", "mednews-data")
    base = os.getenv("LABEL_STUDIO_URL", "http://127.0.0.1:18080").rstrip("/")
    project = int(os.environ["LABEL_STUDIO_BATCH_PROJECT_ID"])
    state_path = Path(os.environ["LABEL_STUDIO_BATCH_STATE"])

    client = oci.object_storage.ObjectStorageClient(
        config={"region": os.getenv("OCI_REGION", "us-ashburn-1")},
        signer=oci.auth.signers.InstancePrincipalsSecurityTokenSigner(),
    )
    objects = oci.pagination.list_call_get_all_results(
        client.list_objects, namespace, bucket, prefix="silver/labelstudio/", fields="name"
    ).data.objects
    runs = sorted(m.group(1) for o in objects if (m := MANIFEST_RE.match(o.name)))

    state = load_state(state_path)
    pending = [run for run in runs if run not in state]
    if not pending:
        print(json.dumps({"status": "sin_lotes_nuevos", "runs": len(runs)}))
        return 0

    refresh = os.environ["LABEL_STUDIO_REFRESH_TOKEN"].strip()
    token = api(base, "/api/token/refresh/", None, {"refresh": refresh})["access"]
    known = existing_record_ids(base, token, project)

    for run in pending:
        name = f"silver/labelstudio/{run}/labelstudio_tasks.csv"
        raw = client.get_object(namespace, bucket, name).data.content.decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(raw, newline="")))
        new = [r for r in rows if r["record_id"] not in known]
        if new:
            api(base, f"/api/projects/{project}/import", token, [{"data": r} for r in new])
            known.update(r["record_id"] for r in new)
        state[run] = {"filas_lote": len(rows), "importadas": len(new), "duplicadas": len(rows) - len(new)}
        save_state(state_path, state)
        print(json.dumps({"status": "importado", "run": run, **state[run]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
