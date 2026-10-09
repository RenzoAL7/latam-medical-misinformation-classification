#!/usr/bin/env python3
"""Import one deduplicated evidence task per record_id into Label Studio.

The script is intentionally standard-library only. It is safe to rerun: task
record_ids already visible in the project are skipped before importing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api_auth import authorization_header


def _request(base_url: str, path: str, token: str, method: str = "GET", payload: Any | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    headers.update(authorization_header(token))
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as result:
            raw = result.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Label Studio HTTP {exc.code}: {detail}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def _list_tasks(base_url: str, project_id: int, token: str) -> list[dict[str, Any]]:
    paths = [
        f"/api/projects/{project_id}/tasks?page_size=100000",
        f"/api/tasks?project={project_id}&page_size=100000",
    ]
    last_error: Exception | None = None
    for path in paths:
        try:
            payload = _request(base_url, path, token)
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                results = payload.get("tasks", payload.get("results", []))
                if isinstance(results, list):
                    return [item for item in results if isinstance(item, dict)]
        except Exception as exc:  # noqa: BLE001 - try the compatible endpoint
            last_error = exc
    if last_error:
        raise last_error
    return []


def _candidates(row: dict[str, str]) -> list[dict[str, Any]]:
    try:
        candidates = json.loads(row.get("pubmed_results_json", "[]") or "[]")
    except json.JSONDecodeError:
        candidates = []
    return [candidate for candidate in candidates if isinstance(candidate, dict)] if isinstance(candidates, list) else []


def _candidate_text(row: dict[str, str], start: int = 0, end: int | None = None) -> str:
    candidates = _candidates(row)[start:end]
    chunks: list[str] = []
    for index, candidate in enumerate(candidates, start=start + 1):
        pmid = str(candidate.get("pmid", ""))
        title = str(candidate.get("title", "")).strip()
        abstract = str(candidate.get("abstract_es", "") or candidate.get("abstract_excerpt", "")).strip()
        score = candidate.get("embedding_similarity", candidate.get("cosine_similarity", ""))
        chunks.append(
            f"[{index}] PMID {pmid} | similitud={score}\n"
            f"Título: {title}\n"
            f"Abstract: {abstract}\n"
            f"PubMed: https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        )
    return "\n\n".join(chunks) or "No hay abstracts disponibles."


def _task_from_row(row: dict[str, str]) -> dict[str, Any]:
    candidates = _candidates(row)
    data = {
        "record_id": row.get("record_id", ""),
        "country": row.get("country", ""),
        "source_name": row.get("source_name", ""),
        "canonical_url": row.get("canonical_url", ""),
        "published_at": row.get("published_at", ""),
        "title": row.get("title", ""),
        "claim_text": row.get("claim_text", ""),
        "claim_text_en": row.get("claim_text_en", ""),
        "pubmed_query_en": row.get("pubmed_query_en", ""),
        "evidence_status": row.get("evidence_status", ""),
        "best_pmid": row.get("best_pmid", ""),
        "best_embedding_similarity": row.get("best_embedding_similarity", ""),
        "best_tfidf_similarity": row.get("best_tfidf_similarity", ""),
        # Keep the complete text for compatibility, but expose a compact
        # primary candidate and a separate collapsed block for the rest.
        "evidence_text": _candidate_text(row),
        "evidence_primary": _candidate_text(row, 0, 1),
        "evidence_additional": _candidate_text(row, 1) if len(candidates) > 1 else "No hay candidatos adicionales.",
        "evidence_count": str(len(candidates)),
        "evidence_run_id": row.get("evidence_run_id", ""),
    }
    return {"data": data}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, nargs="+", help="Evidence CSV file(s) or directories")
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://localhost:8080"))
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--manifest", default="labelstudio/manifest.csv")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Update task data for matching record_id values without changing annotations",
    )
    args = parser.parse_args()

    csv_paths: list[Path] = []
    for value in args.input:
        path = Path(value)
        if path.is_dir():
            csv_paths.extend(sorted(path.glob("*.csv")))
        elif path.is_file():
            csv_paths.append(path)
        else:
            parser.error(f"Input does not exist: {path}")
    csv_paths = sorted(set(csv_paths))
    if not csv_paths:
        parser.error("No CSV files found")

    existing = _list_tasks(args.url, args.project_id, args.token)
    existing_ids = {
        str(item.get("data", {}).get("record_id", "")).strip()
        for item in existing
        if isinstance(item.get("data"), dict)
    }
    existing_task_ids = {
        str(item.get("data", {}).get("record_id", "")).strip(): item.get("id")
        for item in existing
        if isinstance(item.get("data"), dict) and item.get("id") is not None
    }

    latest_by_id: dict[str, dict[str, str]] = {}
    for path in csv_paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                record_id = str(row.get("record_id", "")).strip()
                if not record_id or row.get("evidence_status", "").strip().upper() != "OK":
                    continue
                latest_by_id[record_id] = {str(key): str(value or "") for key, value in row.items()}

    refreshed_count = 0
    if args.refresh_existing:
        for record_id, row in sorted(latest_by_id.items()):
            task_id = existing_task_ids.get(record_id)
            if task_id is None:
                continue
            _request(
                args.url,
                f"/api/tasks/{task_id}/",
                args.token,
                method="PATCH",
                payload={"data": _task_from_row(row)["data"]},
            )
            refreshed_count += 1

    tasks = [
        _task_from_row(row)
        for record_id, row in sorted(latest_by_id.items())
        if record_id not in existing_ids
    ]
    if not tasks:
        print(json.dumps({"status": "ok", "imported": 0, "refreshed": refreshed_count, "skipped_existing": len(latest_by_id) - refreshed_count}, ensure_ascii=False))
        return 0

    result = _request(
        args.url,
        f"/api/projects/{args.project_id}/import",
        args.token,
        method="POST",
        payload=tasks,
    )
    imported_count = len(tasks)
    if isinstance(result, dict):
        imported_count = int(result.get("task_count", result.get("count", imported_count)) or imported_count)

    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    exists = manifest_path.exists()
    with manifest_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["record_id", "project_id", "imported_at", "source_files"])
        if not exists:
            writer.writeheader()
        for record_id in sorted(latest_by_id):
            if record_id in existing_ids:
                continue
            writer.writerow(
                {
                    "record_id": record_id,
                    "project_id": args.project_id,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                    "source_files": ";".join(str(path) for path in csv_paths),
                }
            )
    print(json.dumps({"status": "ok", "imported": imported_count, "refreshed": refreshed_count, "skipped_existing": len(existing_ids), "candidate_records": len(latest_by_id), "project_id": args.project_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
