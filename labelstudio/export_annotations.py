#!/usr/bin/env python3
"""Read the completed claim-level annotations in a Label Studio export.

``scripts/04_build_training_dataset.py`` is the only user-facing local
command. This module deliberately contains just the parsing helpers shared by
that command and its tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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


def _latest_completed_annotation(task: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest submitted annotation, never an unfinished draft.

    The JSON export shape differs slightly between Label Studio releases.  A
    submitted annotation can carry ``completed=true`` or a reviewer identity
    plus non-empty results.  A draft has neither signal and must not become a
    human decision in Gold.
    """

    annotations = task.get("annotations", [])
    if not isinstance(annotations, list):
        return None
    completed: list[dict[str, Any]] = []
    for item in annotations:
        if not isinstance(item, dict) or item.get("was_cancelled"):
            continue
        results = item.get("result", [])
        submitted = bool(item.get("completed")) or (
            bool(item.get("completed_by") or item.get("created_username"))
            and isinstance(results, list)
            and bool(results)
        )
        if submitted:
            completed.append(item)
    if not completed:
        return None
    return sorted(
        completed,
        key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
    )[-1]


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
        annotation = _latest_completed_annotation(task)
        if annotation is None:
            continue
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
