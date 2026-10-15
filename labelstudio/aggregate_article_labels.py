#!/usr/bin/env python3
"""Aggregate reviewed claims into one auditable label per news article.

This module is used by ``scripts/04_build_training_dataset.py``. Reviewers
label evidence at claim level; this helper applies the documented article rule:

* one contradicted central/material claim => ``REFUTADA``;
* all central/material claims supported => ``RESPALDADA``;
* otherwise => ``NO_DETERMINABLE``.

Claims never become duplicate training rows. The builder owns input discovery,
CSV writing, and the final validation gate.
"""

from __future__ import annotations

from collections import defaultdict
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
