#!/usr/bin/env python3
"""Validate aggregated article labels before binary training.

This module is called by ``scripts/04_build_training_dataset.py``. It never
infers a medical label; it only separates valid audited rows from the binary
subset and rejects anything incomplete or inconsistent.
"""

from __future__ import annotations

from typing import Iterable


LABEL_RELATION = {
    "RESPALDADA": "supports",
    "REFUTADA": "contradicts",
    "NO_DETERMINABLE": "unclear",
}
ALLOWED_LABELS = frozenset((*LABEL_RELATION, "EXCLUIDA"))
ALLOWED_RELATIONS = frozenset(("supports", "contradicts", "unclear"))
COMMON_REQUIRED_FIELDS = (
    "record_id",
    "claim_text",
    "evidence_relation",
    "label",
    "verification_note",
    "annotation_id",
    "annotator",
    "annotated_at",
)
OUTPUT_FIELDS = (
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
    "best_embedding_similarity",
    "best_tfidf_similarity",
    "embedding_model",
    "evidence_status",
    "evidence_run_id",
    "evidence_relation",
    "label",
    "verification_note",
    "annotation_id",
    "annotator",
    "annotated_at",
    "claims_total",
    "decisive_claims_total",
    "aggregation_status",
    "aggregation_reason",
    "final_training_eligible",
    "validation_status",
    "dataset_split",
)
TRAINING_FIELDS = (
    "record_id",
    "title",
    "body",
    "label",
    "dataset_split",
)
REJECT_FIELDS = (*OUTPUT_FIELDS, "rejection_reasons")


def _clean(row: dict[str, str]) -> dict[str, str]:
    return {key: str(value or "").strip() for key, value in row.items()}


def _issues(row: dict[str, str], seen_ids: set[str]) -> list[str]:
    issues = [f"missing_{field}" for field in COMMON_REQUIRED_FIELDS if not row.get(field, "")]
    if row.get("aggregation_status", "") and row["aggregation_status"] != "COMPLETADA":
        issues.append("aggregation_not_completed")
    record_id = row.get("record_id", "")
    if record_id and record_id in seen_ids:
        issues.append("duplicate_record_id")
    if record_id:
        seen_ids.add(record_id)

    label = row.get("label", "")
    relation = row.get("evidence_relation", "")
    if label and label not in ALLOWED_LABELS:
        issues.append("invalid_label")
    if relation and relation not in ALLOWED_RELATIONS:
        issues.append("invalid_evidence_relation")
    expected_relation = LABEL_RELATION.get(label)
    if expected_relation and relation and relation != expected_relation:
        issues.append("label_relation_mismatch")
    if label in {"RESPALDADA", "REFUTADA"} and not row.get("best_pmid", ""):
        issues.append("missing_best_pmid_for_binary_label")
    if label in {"RESPALDADA", "REFUTADA"} and not row.get("title", ""):
        issues.append("missing_title_for_binary_label")
    if label in {"RESPALDADA", "REFUTADA"} and not row.get("body", ""):
        issues.append("missing_body_for_binary_label")
    return issues


def _output_row(
    row: dict[str, str], training_eligible: bool, validation_status: str = "COMPLETADA"
) -> dict[str, str]:
    result = {field: row.get(field, "") for field in OUTPUT_FIELDS}
    result["final_training_eligible"] = "SI" if training_eligible else "NO"
    result["validation_status"] = validation_status
    return result


def validate_rows(rows: Iterable[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Return reviewed rows, binary-training rows, and rejected rows."""

    reviewed: list[dict[str, str]] = []
    training: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for source_row in rows:
        row = _clean(source_row)
        issues = _issues(row, seen_ids)
        if issues:
            rejected.append(
                {
                    **_output_row(row, False, validation_status="REQUIERE_CORRECCION"),
                    "rejection_reasons": ";".join(issues),
                }
            )
            continue
        is_binary = row["label"] in {"RESPALDADA", "REFUTADA"}
        accepted = _output_row(row, is_binary)
        reviewed.append(accepted)
        if is_binary:
            training.append(accepted)
    return reviewed, training, rejected
