#!/usr/bin/env python3
"""Aggregate reviewed claims into one auditable label per news article.

This is deliberately separate from Label Studio. Reviewers label evidence at
claim level; this script applies the documented article-level rule:

* one contradicted central/material claim => ``REFUTADA``;
* all central/material claims supported => ``RESPALDADA``;
* otherwise => ``NO_DETERMINABLE``.

Claims never become duplicate training rows.  The output has one row per
``record_id`` and can be passed to ``validate_gold.py``.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable


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
    "claims_total",
    "decisive_claims_total",
    "aggregation_status",
    "aggregation_reason",
)


def _clean(row: dict[str, str]) -> dict[str, str]:
    return {str(key): str(value or "").strip() for key, value in row.items() if key}


def _csv_paths(values: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.csv")))
        elif path.is_file():
            paths.append(path)
        else:
            raise FileNotFoundError(f"Input does not exist: {path}")
    return sorted(set(paths))


def _body_rows(values: Iterable[str]) -> dict[str, dict[str, str]]:
    rows_by_id: dict[str, dict[str, str]] = {}
    for path in _csv_paths(values):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for source in csv.DictReader(handle):
                row = _clean(source)
                record_id = row.get("record_id", "")
                if not record_id:
                    continue
                existing = rows_by_id.get(record_id, {})
                if not existing or row.get("body") or not existing.get("body"):
                    rows_by_id[record_id] = row
    return rows_by_id


def _claim_issues(row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for field in (
        "claim_id",
        "claim_text",
        "claim_materiality",
        "evidence_relation",
        "claim_verdict",
        "selected_pmids",
        "verification_note",
        "annotation_id",
        "annotator",
        "annotated_at",
    ):
        if not row.get(field, ""):
            issues.append(f"missing_{field}")
    materiality = row.get("claim_materiality", "")
    if materiality and materiality not in {"central", "material", "secondary"}:
        issues.append("invalid_claim_materiality")
    verdict = row.get("claim_verdict", "")
    relation = row.get("evidence_relation", "")
    expected = {
        "RESPALDADO": "supports",
        "REFUTADO": "contradicts",
        "NO_DETERMINABLE": "unclear",
    }.get(verdict)
    if verdict and verdict not in {"RESPALDADO", "REFUTADO", "NO_DETERMINABLE"}:
        issues.append("invalid_claim_verdict")
    if relation and relation not in {"supports", "contradicts", "unclear"}:
        issues.append("invalid_evidence_relation")
    if expected and relation and relation != expected:
        issues.append("claim_relation_mismatch")
    return issues


def _value(rows: list[dict[str, str]], field: str) -> str:
    for row in rows:
        value = row.get(field, "")
        if value:
            return value
    return ""


def _aggregate_record(
    record_id: str,
    claims: list[dict[str, str]],
    body_rows: dict[str, dict[str, str]],
) -> dict[str, str]:
    source = body_rows.get(record_id, {})
    issues = [issue for claim in claims for issue in _claim_issues(claim)]
    decisive = [
        claim
        for claim in claims
        if claim.get("claim_materiality", "") in {"central", "material"}
    ]
    if not decisive:
        issues.append("no_central_or_material_claim")

    if issues:
        label = "NO_DETERMINABLE"
        relation = "unclear"
        aggregation_status = "REQUIERE_CORRECCION"
        aggregation_reason = ";".join(sorted(set(issues)))
        decisive_for_audit = decisive or claims
    else:
        contradicted = [claim for claim in decisive if claim.get("claim_verdict") == "REFUTADO"]
        supported = [claim for claim in decisive if claim.get("claim_verdict") == "RESPALDADO"]
        if contradicted:
            label = "REFUTADA"
            relation = "contradicts"
            aggregation_reason = "Al menos un claim central/material fue refutado."
            decisive_for_audit = contradicted
        elif len(supported) == len(decisive):
            label = "RESPALDADA"
            relation = "supports"
            aggregation_reason = "Todos los claims centrales/materiales fueron respaldados."
            decisive_for_audit = supported
        else:
            label = "NO_DETERMINABLE"
            relation = "unclear"
            aggregation_reason = "Al menos un claim central/material no pudo determinarse."
            decisive_for_audit = decisive
        aggregation_status = "COMPLETADA"

    claim_summary = " | ".join(
        claim.get("claim_text", "") for claim in decisive_for_audit if claim.get("claim_text", "")
    )
    selected_pmids = [claim.get("selected_pmids", "") for claim in decisive_for_audit if claim.get("selected_pmids", "")]
    notes = [claim.get("verification_note", "") for claim in decisive_for_audit if claim.get("verification_note", "")]
    annotation_ids = [claim.get("annotation_id", "") for claim in decisive_for_audit if claim.get("annotation_id", "")]
    annotators = [claim.get("annotator", "") for claim in decisive_for_audit if claim.get("annotator", "")]
    annotated_dates = [claim.get("annotated_at", "") for claim in decisive_for_audit if claim.get("annotated_at", "")]
    value = lambda field: source.get(field, "") or _value(claims, field)
    return {
        "record_id": record_id,
        "country": value("country"),
        "source_name": value("source_name"),
        "topic": value("topic"),
        "canonical_url": value("canonical_url"),
        "published_at": value("published_at"),
        "title": value("title"),
        "body": source.get("body", ""),
        "claim_text": claim_summary,
        "best_pmid": ";".join(dict.fromkeys(selected_pmids)) or _value(decisive_for_audit, "best_pmid"),
        "best_cosine_similarity": _value(decisive_for_audit, "best_embedding_similarity"),
        "best_embedding_similarity": _value(decisive_for_audit, "best_embedding_similarity"),
        "best_tfidf_similarity": _value(decisive_for_audit, "best_tfidf_similarity"),
        "ranking_method": "oci_cohere_embed_v4",
        "embedding_model": "cohere.embed-v4.0",
        "evidence_status": "OK" if not issues else "REQUIERE_CORRECCION",
        "evidence_run_id": _value(claims, "evidence_run_id"),
        "evidence_relation": relation,
        "label": label,
        "verification_note": "\n".join([aggregation_reason, *notes]).strip(),
        "annotation_id": ";".join(dict.fromkeys(annotation_ids)),
        "annotator": ";".join(dict.fromkeys(annotators)),
        "annotated_at": max(annotated_dates, default=""),
        "claims_total": str(len(claims)),
        "decisive_claims_total": str(len(decisive)),
        "aggregation_status": aggregation_status,
        "aggregation_reason": aggregation_reason,
    }


def aggregate_rows(
    claims: Iterable[dict[str, str]], body_rows: dict[str, dict[str, str]] | None = None
) -> list[dict[str, str]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for source in claims:
        row = _clean(source)
        record_id = row.get("record_id", "")
        if record_id:
            grouped[record_id].append(row)
    body_rows = body_rows or {}
    return [_aggregate_record(record_id, grouped[record_id], body_rows) for record_id in sorted(grouped)]


def _write(path: Path, rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims-input", required=True, help="CSV exported with export_annotations.py --mode multiclaim")
    parser.add_argument("--body-input", nargs="*", default=[], help="Body CSV file(s) or directories to restore title/body")
    parser.add_argument("--output", required=True, help="One article-level CSV for validate_gold.py")
    args = parser.parse_args()

    with Path(args.claims_input).open("r", encoding="utf-8-sig", newline="") as handle:
        claims = [_clean(row) for row in csv.DictReader(handle)]
    try:
        bodies = _body_rows(args.body_input) if args.body_input else {}
    except FileNotFoundError as exc:
        parser.error(str(exc))
    rows = aggregate_rows(claims, bodies)
    _write(Path(args.output), rows)
    print(
        json.dumps(
            {
                "status": "requires_correction" if any(row["aggregation_status"] != "COMPLETADA" for row in rows) else "ok",
                "articles": len(rows),
                "requires_correction": sum(row["aggregation_status"] != "COMPLETADA" for row in rows),
                "output": args.output,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
