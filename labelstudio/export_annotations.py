#!/usr/bin/env python3
"""Export one latest human annotation per record_id as a Gold-ready CSV."""

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


def _request(base_url: str, path: str, token: str) -> Any:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json", **authorization_header(token)},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as result:
            raw = result.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Label Studio HTTP {exc.code}: {detail}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Label Studio export was not JSON") from exc


def _value_for(results: list[dict[str, Any]], name: str, key: str) -> str:
    for result in results:
        if result.get("from_name") != name:
            continue
        value = result.get("value", {})
        if not isinstance(value, dict):
            continue
        values = value.get(key)
        if isinstance(values, list) and values:
            # TextArea results are exported as a list of strings.  Join all
            # entries so a multi-line reviewer note remains readable in Gold.
            return "\n".join(str(item) for item in values if str(item).strip())
        if isinstance(values, str):
            return values
    return ""


def _task_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        tasks = payload.get("tasks", payload.get("results", []))
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, dict)]
    return []


def _evidence_rows(paths: list[str]) -> dict[str, dict[str, str]]:
    """Load one latest Evidence row per record for the Gold text join."""

    csv_paths: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.is_dir():
            csv_paths.extend(sorted(path.glob("*.csv")))
        elif path.is_file():
            csv_paths.append(path)
        else:
            raise FileNotFoundError(f"Evidence input does not exist: {path}")

    rows_by_id: dict[str, dict[str, str]] = {}
    for path in sorted(set(csv_paths)):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for source_row in csv.DictReader(handle):
                record_id = str(source_row.get("record_id", "")).strip()
                if record_id:
                    candidate = {
                        str(key): str(value or "").strip()
                        for key, value in source_row.items()
                        if key
                    }
                    previous = rows_by_id.get(record_id, {})
                    # Prefer the row carrying the article body when a folder
                    # contains an older evidence CSV plus a refreshed one.
                    if not previous or candidate.get("body") or not previous.get("body"):
                        rows_by_id[record_id] = candidate
    return rows_by_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://localhost:8080"))
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--output", default="gold_labels.csv")
    parser.add_argument("--raw-export", default="")
    parser.add_argument(
        "--evidence-input",
        nargs="*",
        default=[],
        help="Evidence CSV file(s) or directories used to restore body/topic metadata by record_id",
    )
    args = parser.parse_args()

    payload = _request(args.url, f"/api/projects/{args.project_id}/export?exportType=JSON", args.token)
    evidence_rows = _evidence_rows(args.evidence_input)
    if args.raw_export:
        Path(args.raw_export).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rows_by_id: dict[str, dict[str, Any]] = {}
    for task in _task_rows(payload):
        data = task.get("data", {})
        if not isinstance(data, dict):
            continue
        record_id = str(data.get("record_id", "")).strip()
        if not record_id:
            continue
        annotations = task.get("annotations", [])
        if not isinstance(annotations, list):
            continue
        completed = [item for item in annotations if isinstance(item, dict) and item.get("completed")]
        if not completed:
            completed = [item for item in annotations if isinstance(item, dict)]
        if not completed:
            continue
        annotation = sorted(completed, key=lambda item: str(item.get("updated_at", item.get("created_at", ""))))[-1]
        results = annotation.get("result", [])
        if not isinstance(results, list):
            results = []
        source_row = evidence_rows.get(record_id, {})
        value = lambda name: data.get(name, "") or source_row.get(name, "")
        rows_by_id[record_id] = {
            "record_id": record_id,
            "country": value("country"),
            "source_name": value("source_name"),
            "topic": value("topic"),
            "canonical_url": value("canonical_url"),
            "title": value("title"),
            "published_at": value("published_at"),
            "body": value("body"),
            "claim_text": value("claim_text"),
            "best_pmid": value("best_pmid"),
            "best_cosine_similarity": value("best_cosine_similarity"),
            "best_embedding_similarity": value("best_embedding_similarity"),
            "best_tfidf_similarity": value("best_tfidf_similarity"),
            "ranking_method": value("ranking_method"),
            "embedding_model": value("embedding_model"),
            "evidence_status": value("evidence_status"),
            "evidence_run_id": value("evidence_run_id"),
            "evidence_relation": _value_for(results, "evidence_relation", "choices"),
            # The UI calls this control ``final_label``; Gold uses the
            # canonical dataset column name ``label``.
            "label": _value_for(results, "final_label", "choices"),
            "verification_note": _value_for(results, "verification_note", "text"),
            "annotation_id": annotation.get("id", ""),
            "annotator": annotation.get("completed_by", annotation.get("created_username", "")),
            "annotated_at": annotation.get("updated_at", annotation.get("created_at", "")),
        }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "record_id",
        "country",
        "source_name",
        "topic",
        "canonical_url",
        "published_at",
        "title",
        "body",
        "claim_text",
        "best_pmid",
        "best_cosine_similarity",
        "best_embedding_similarity",
        "best_tfidf_similarity",
        "ranking_method",
        "embedding_model",
        "evidence_status",
        "evidence_run_id",
        "evidence_relation",
        "label",
        "verification_note",
        "annotation_id",
        "annotator",
        "annotated_at",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows_by_id.values())
    missing_body = sum(1 for row in rows_by_id.values() if not row.get("body", ""))
    print(json.dumps({"status": "ok", "project_id": args.project_id, "annotated_records": len(rows_by_id), "missing_body": missing_body, "output": str(output), "exported_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
