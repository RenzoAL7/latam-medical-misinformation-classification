#!/usr/bin/env python3
"""Import one deduplicated Evidence JSONL task per claim into Label Studio."""

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


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value or "").strip()


def _candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    direct = row.get("candidates")
    if isinstance(direct, list):
        return [candidate for candidate in direct if isinstance(candidate, dict)]
    try:
        candidates = json.loads(row.get("pubmed_results_json", "[]") or "[]")
    except json.JSONDecodeError:
        candidates = []
    return [candidate for candidate in candidates if isinstance(candidate, dict)] if isinstance(candidates, list) else []


def _candidate_text(row: dict[str, Any], start: int = 0, end: int | None = None) -> str:
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


def _claim_key(row: dict[str, Any]) -> str:
    record_id = _as_text(row.get("record_id"))
    claim_id = _as_text(row.get("claim_id"))
    return f"{record_id}::{claim_id}" if claim_id else record_id


def _task_from_row(row: dict[str, Any]) -> dict[str, Any]:
    candidates = _candidates(row)
    data = {
        "record_id": row.get("record_id", ""),
        "claim_id": row.get("claim_id", ""),
        "claim_key": _claim_key(row),
        "country": row.get("country", ""),
        "source_name": row.get("source_name", ""),
        "topic": row.get("topic", ""),
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
        "ranking_method": row.get("ranking_method", ""),
        "embedding_model": row.get("embedding_model", ""),
        "embedding_status": row.get("embedding_status", ""),
        "translation_status": row.get("translation_status", ""),
        "claim_type": row.get("claim_type", ""),
        # Keep one copy of the candidate text in the multi-claim interface.
        # Storing a second complete ``evidence_text`` copy makes every task
        # unnecessarily large and slows Label Studio as the corpus grows.
        "evidence_primary": _candidate_text(row, 0, 1),
        "evidence_additional": _candidate_text(row, 1) if len(candidates) > 1 else "No hay candidatos adicionales.",
        "evidence_count": str(len(candidates)),
        "evidence_run_id": row.get("evidence_run_id", ""),
    }
    return {"data": data}


def _flatten_jsonl_record(record: dict[str, Any]) -> dict[str, Any]:
    """Flatten the lightweight article envelope used by evidence.v1."""

    article = record.get("article", {})
    if not isinstance(article, dict):
        article = {}
    row = {str(key): value for key, value in article.items()}
    row.update({str(key): value for key, value in record.items() if key != "article"})
    row["pubmed_results_json"] = json.dumps(_candidates(row), ensure_ascii=False)
    return row


def _rows_from_path(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() != ".jsonl":
        raise RuntimeError(f"Evidence input must be JSONL, not {path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSONL in {path} at line {number}") from exc
        if not isinstance(record, dict):
            raise RuntimeError(f"JSONL record {number} in {path} is not an object")
        if _as_text(record.get("schema_version")) != "evidence.v1":
            raise RuntimeError(f"Unsupported evidence schema in {path} at line {number}")
        rows.append(_flatten_jsonl_record(record))
    return rows


def _input_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jsonl")))
        elif path.is_file():
            paths.append(path)
        else:
            raise FileNotFoundError(f"Input does not exist: {path}")
    return sorted(set(paths))


def _ready_for_review(row: dict[str, Any]) -> bool:
    if _as_text(row.get("schema_version")) != "evidence.v1":
        return False
    if _as_text(row.get("llm_status")).upper() != "OK":
        return False

    evidence_status = _as_text(row.get("evidence_status")).upper()
    if evidence_status == "OK":
        return all(
            _as_text(row.get(field)).upper() == "OK"
            for field in ("embedding_status", "translation_status")
        )

    # Terminal PubMed misses still need a human materiality decision. They
    # may become secondary, or central/material with an explicit
    # NO_DETERMINABLE verdict; they are never supporting/contradicting evidence.
    return (
        evidence_status in {"NO_RESULTS", "NO_ABSTRACT"}
        and _as_text(row.get("embedding_status")).upper() == "SKIPPED_NO_EVIDENCE"
        and _as_text(row.get("translation_status")).upper() == "SKIPPED_NO_EVIDENCE"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, nargs="+", help="Evidence JSONL file(s) or directories")
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://localhost:8080"))
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--manifest", default="labelstudio/manifest.csv")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Update task data for matching claim_key values without changing annotations",
    )
    args = parser.parse_args()

    try:
        input_paths = _input_paths(args.input)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    if not input_paths:
        parser.error("No Evidence JSONL files found")

    existing = _list_tasks(args.url, args.project_id, args.token)
    existing_ids = {
        _as_text(item.get("data", {}).get("claim_key"))
        or _as_text(item.get("data", {}).get("record_id"))
        for item in existing
        if isinstance(item.get("data"), dict)
    }
    existing_task_ids = {
        (
            _as_text(item.get("data", {}).get("claim_key"))
            or _as_text(item.get("data", {}).get("record_id"))
        ): item.get("id")
        for item in existing
        if isinstance(item.get("data"), dict) and item.get("id") is not None
    }

    latest_by_key: dict[str, dict[str, Any]] = {}
    for path in input_paths:
        for row in _rows_from_path(path):
            key = _claim_key(row)
            if not key or not _ready_for_review(row):
                continue
            latest_by_key[key] = row

    refreshed_count = 0
    if args.refresh_existing:
        for claim_key, row in sorted(latest_by_key.items()):
            task_id = existing_task_ids.get(claim_key)
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
        for claim_key, row in sorted(latest_by_key.items())
        if claim_key not in existing_ids
    ]
    if not tasks:
        print(json.dumps({"status": "ok", "imported": 0, "refreshed": refreshed_count, "skipped_existing": len(latest_by_key) - refreshed_count}, ensure_ascii=False))
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
        writer = csv.DictWriter(
            handle,
            fieldnames=["claim_key", "record_id", "claim_id", "project_id", "imported_at", "source_files"],
        )
        if not exists:
            writer.writeheader()
        for claim_key, row in sorted(latest_by_key.items()):
            if claim_key in existing_ids:
                continue
            writer.writerow(
                {
                    "claim_key": claim_key,
                    "record_id": _as_text(row.get("record_id")),
                    "claim_id": _as_text(row.get("claim_id")),
                    "project_id": args.project_id,
                    "imported_at": datetime.now(timezone.utc).isoformat(),
                    "source_files": ";".join(str(path) for path in input_paths),
                }
            )
    print(json.dumps({"status": "ok", "imported": imported_count, "refreshed": refreshed_count, "skipped_existing": len(existing_ids), "candidate_claims": len(latest_by_key), "project_id": args.project_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
