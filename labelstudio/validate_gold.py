#!/usr/bin/env python3
"""Validate a Label Studio export and create auditable Gold CSVs.

The script never infers a medical label. It accepts completed human annotations
exported by ``export_annotations.py`` and separates the complete auditable
review set from the binary-training subset. Invalid records are written
separately, so they cannot accidentally reach ``gold/training``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
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


def _write_csv(path: Path, fields: Iterable[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="CSV created by export_annotations.py")
    parser.add_argument("--reviewed-output", required=True, help="Validated audit CSV for gold/reviewed")
    parser.add_argument("--training-output", required=True, help="Binary-only CSV for gold/training")
    parser.add_argument("--rejected-output", required=True, help="Rows that require human correction")
    parser.add_argument(
        "--fail-on-reject",
        action="store_true",
        help="Return a non-zero status if any row fails validation; use before uploading Gold.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            parser.error("Input CSV has no header")
        reviewed, training, rejected = validate_rows(reader)

    _write_csv(Path(args.reviewed_output), OUTPUT_FIELDS, reviewed)
    _write_csv(Path(args.training_output), TRAINING_FIELDS, training)
    _write_csv(Path(args.rejected_output), REJECT_FIELDS, rejected)
    print(
        json.dumps(
            {
                "status": "rejected" if rejected else "ok",
                "reviewed_records": len(reviewed),
                "training_records": len(training),
                "rejected_records": len(rejected),
                "reviewed_output": args.reviewed_output,
                "training_output": args.training_output,
                "rejected_output": args.rejected_output,
            },
            ensure_ascii=False,
        )
    )
    return 2 if args.fail_on_reject and rejected else 0


if __name__ == "__main__":
    sys.exit(main())
