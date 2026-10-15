#!/usr/bin/env python3
"""Export one latest human annotation per record_id as a Gold-ready CSV.

The exporter supports two input modes:

* API mode, which fetches the project export from Label Studio.
* Offline mode, which reads a JSON export downloaded manually from the
  Label Studio interface. Offline mode is the recommended local workflow.
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


def _read_json_export(path: Path) -> Any:
    """Read a JSON export downloaded from the Label Studio interface."""

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"Could not read Label Studio JSON export: {path}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Label Studio export is not valid JSON: {path}") from exc


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


GOLD_FIELDS = [
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

CLAIM_REVIEW_FIELDS = [
    "record_id",
    "claim_id",
    "claim_key",
    "country",
    "source_name",
    "topic",
    "canonical_url",
    "published_at",
    "title",
    "claim_text",
    "claim_type",
    "best_pmid",
    "best_embedding_similarity",
    "best_tfidf_similarity",
    "evidence_status",
    "evidence_run_id",
    "claim_materiality",
    "evidence_relation",
    "claim_verdict",
    "selected_pmids",
    "verification_note",
    "annotation_id",
    "annotator",
    "annotated_at",
]


def _gold_rows(
    payload: Any,
    evidence_rows: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Build one Gold row from the latest annotation for each record_id."""

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
        annotation = sorted(
            completed,
            key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
        )[-1]
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
    return list(rows_by_id.values())


def _claim_review_rows(payload: Any) -> list[dict[str, Any]]:
    """Export one latest completed human annotation per claim task."""

    rows_by_key: dict[str, dict[str, Any]] = {}
    for task in _task_rows(payload):
        data = task.get("data", {})
        if not isinstance(data, dict):
            continue
        record_id = str(data.get("record_id", "")).strip()
        claim_id = str(data.get("claim_id", "")).strip()
        claim_key = str(data.get("claim_key", "") or f"{record_id}::{claim_id}").strip()
        if not record_id or not claim_id or not claim_key:
            continue
        annotations = task.get("annotations", [])
        if not isinstance(annotations, list):
            continue
        completed = [item for item in annotations if isinstance(item, dict) and item.get("completed")]
        if not completed:
            completed = [item for item in annotations if isinstance(item, dict)]
        if not completed:
            continue
        annotation = sorted(
            completed,
            key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
        )[-1]
        results = annotation.get("result", [])
        if not isinstance(results, list):
            results = []
        rows_by_key[claim_key] = {
            "record_id": record_id,
            "claim_id": claim_id,
            "claim_key": claim_key,
            "country": data.get("country", ""),
            "source_name": data.get("source_name", ""),
            "topic": data.get("topic", ""),
            "canonical_url": data.get("canonical_url", ""),
            "published_at": data.get("published_at", ""),
            "title": data.get("title", ""),
            "claim_text": data.get("claim_text", ""),
            "claim_type": data.get("claim_type", ""),
            "best_pmid": data.get("best_pmid", ""),
            "best_embedding_similarity": data.get("best_embedding_similarity", ""),
            "best_tfidf_similarity": data.get("best_tfidf_similarity", ""),
            "evidence_status": data.get("evidence_status", ""),
            "evidence_run_id": data.get("evidence_run_id", ""),
            "claim_materiality": _value_for(results, "claim_materiality", "choices"),
            "evidence_relation": _value_for(results, "evidence_relation", "choices"),
            "claim_verdict": _value_for(results, "claim_verdict", "choices"),
            "selected_pmids": _value_for(results, "selected_pmids", "text"),
            "verification_note": _value_for(results, "verification_note", "text"),
            "annotation_id": annotation.get("id", ""),
            "annotator": annotation.get("completed_by", annotation.get("created_username", "")),
            "annotated_at": annotation.get("updated_at", annotation.get("created_at", "")),
        }
    return [rows_by_key[key] for key in sorted(rows_by_key)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://localhost:8080"))
    parser.add_argument(
        "--project-id",
        type=int,
        default=0,
        help="Label Studio project ID for API mode; not needed with --input-json",
    )
    parser.add_argument("--token", default=os.getenv("LABEL_STUDIO_TOKEN", ""))
    parser.add_argument("--output", default="gold_labels.csv")
    parser.add_argument(
        "--mode",
        choices=("auto", "legacy", "multiclaim"),
        default="auto",
        help="auto detects claim tasks; multiclaim writes one review row per claim",
    )
    parser.add_argument("--raw-export", default="")
    parser.add_argument(
        "--input-json",
        type=Path,
        help="Local JSON export downloaded from Label Studio; skips the API and token",
    )
    parser.add_argument(
        "--evidence-input",
        nargs="*",
        default=[],
        help="Evidence CSV file(s) or directories used to restore body/topic metadata by record_id",
    )
    args = parser.parse_args()

    if args.input_json is not None:
        payload = _read_json_export(args.input_json)
    else:
        if args.project_id <= 0:
            parser.error("--project-id must be positive unless --input-json is used")
        payload = _request(args.url, f"/api/projects/{args.project_id}/export?exportType=JSON", args.token)

    evidence_rows = _evidence_rows(args.evidence_input)
    if args.raw_export:
        raw_path = Path(args.raw_export)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if args.input_json is None or raw_path.resolve() != args.input_json.resolve():
            raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    has_claim_tasks = any(
        isinstance(task.get("data"), dict) and str(task["data"].get("claim_id", "")).strip()
        for task in _task_rows(payload)
    )
    mode = "multiclaim" if args.mode == "auto" and has_claim_tasks else "legacy" if args.mode == "auto" else args.mode
    if mode == "multiclaim":
        rows = _claim_review_rows(payload)
        output_fields = CLAIM_REVIEW_FIELDS
        count_name = "annotated_claims"
    else:
        rows = _gold_rows(payload, evidence_rows)
        output_fields = GOLD_FIELDS
        count_name = "annotated_records"

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        writer.writerows(rows)
    missing_body = sum(1 for row in rows if not row.get("body", ""))
    print(
        json.dumps(
            {
                "status": "ok",
                "source": str(args.input_json) if args.input_json is not None else "api",
                "project_id": args.project_id or None,
                count_name: len(rows),
                "mode": mode,
                "missing_body": missing_body,
                "output": str(output),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
