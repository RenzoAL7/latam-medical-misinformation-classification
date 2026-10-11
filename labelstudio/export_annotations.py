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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://localhost:8080"))
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--output", default="gold_labels.csv")
    parser.add_argument("--raw-export", default="")
    args = parser.parse_args()

    payload = _request(args.url, f"/api/projects/{args.project_id}/export?exportType=JSON", args.token)
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
        rows_by_id[record_id] = {
            "record_id": record_id,
            "country": data.get("country", ""),
            "source_name": data.get("source_name", ""),
            "canonical_url": data.get("canonical_url", ""),
            "title": data.get("title", ""),
            "claim_text": data.get("claim_text", ""),
            "best_pmid": data.get("best_pmid", ""),
            "best_embedding_similarity": data.get("best_embedding_similarity", ""),
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
    fields = ["record_id", "country", "source_name", "canonical_url", "title", "claim_text", "best_pmid", "best_embedding_similarity", "evidence_relation", "label", "verification_note", "annotation_id", "annotator", "annotated_at"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows_by_id.values())
    print(json.dumps({"status": "ok", "project_id": args.project_id, "annotated_records": len(rows_by_id), "output": str(output), "exported_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
