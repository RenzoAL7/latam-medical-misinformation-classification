"""Small, dependency-free Gold builder used by the scheduled Function.

The local CLI remains the reference tool for a manual rebuild.  This module
keeps the same claim identity and article-label rules, but emits only the
single training CSV plus a compact rejected report that belongs in Object
Storage.  It intentionally never infers a label from similarity scores.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from typing import Any, Iterable


TRAINING_FIELDS = ("record_id", "title", "body", "label", "dataset_split")
CLAIM_REVIEW_FIELDS = (
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
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _json_records(raw: bytes, name: str) -> list[dict[str, Any]]:
    """Read JSON or JSONL without accepting scalar records."""

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} is not UTF-8") from exc
    if name.lower().endswith(".jsonl"):
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL in {name} at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record {line_number} in {name} is not an object")
            records.append(value)
        return records
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} is not valid JSON") from exc
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        tasks = value.get("tasks", value.get("results"))
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, dict)]
        return [value]
    raise ValueError(f"{name} must contain a JSON object or array")


def _value_for(results: list[dict[str, Any]], name: str, key: str) -> str:
    for result in results:
        if result.get("from_name") != name:
            continue
        value = result.get("value", {})
        if not isinstance(value, dict):
            continue
        values = value.get(key)
        if isinstance(values, list) and values:
            return "\n".join(str(item) for item in values if str(item).strip())
        if isinstance(values, str):
            return values
    return ""


def _latest_completed_annotation(task: dict[str, Any]) -> dict[str, Any] | None:
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


def _claim_reviews(tasks: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    rows_by_key: dict[str, dict[str, str]] = {}
    for task in tasks:
        data = task.get("data", {})
        if not isinstance(data, dict):
            continue
        record_id = _clean(data.get("record_id"))
        claim_id = _clean(data.get("claim_id"))
        claim_key = _clean(data.get("claim_key")) or f"{record_id}::{claim_id}"
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
            "country": _clean(data.get("country")),
            "source_name": _clean(data.get("source_name")),
            "topic": _clean(data.get("topic")),
            "canonical_url": _clean(data.get("canonical_url")),
            "published_at": _clean(data.get("published_at")),
            "title": _clean(data.get("title")),
            "claim_text": _clean(data.get("claim_text")),
            "claim_type": _clean(data.get("claim_type")),
            "best_pmid": _clean(data.get("best_pmid")),
            "best_embedding_similarity": _clean(data.get("best_embedding_similarity")),
            "best_tfidf_similarity": _clean(data.get("best_tfidf_similarity")),
            "evidence_status": _clean(data.get("evidence_status")),
            "evidence_run_id": _clean(data.get("evidence_run_id")),
            "claim_materiality": _value_for(results, "claim_materiality", "choices"),
            "evidence_relation": _value_for(results, "evidence_relation", "choices"),
            "claim_verdict": _value_for(results, "claim_verdict", "choices"),
            "selected_pmids": _value_for(results, "selected_pmids", "text"),
            "verification_note": _value_for(results, "verification_note", "text"),
            "annotation_id": _clean(annotation.get("id")),
            "annotator": _clean(annotation.get("completed_by") or annotation.get("created_username")),
            "annotated_at": _clean(annotation.get("updated_at") or annotation.get("created_at")),
        }
    return [rows_by_key[key] for key in sorted(rows_by_key)]


def _task_claim_keys(tasks: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = defaultdict(set)
    for task in tasks:
        data = task.get("data", {})
        if not isinstance(data, dict):
            continue
        record_id = _clean(data.get("record_id"))
        claim_id = _clean(data.get("claim_id"))
        claim_key = _clean(data.get("claim_key")) or f"{record_id}::{claim_id}"
        if record_id and claim_id and claim_key:
            expected[record_id].add(claim_key)
    if not expected:
        raise ValueError("The export has no multi-claim tasks with record_id and claim_id")
    return expected


def _evidence_block_reason(record: dict[str, Any]) -> str:
    statuses = {
        field: _clean(record.get(field)).upper()
        for field in ("llm_status", "evidence_status", "embedding_status", "translation_status")
    }
    if (
        statuses["llm_status"] == "OK"
        and statuses["evidence_status"] in {"NO_RESULTS", "NO_ABSTRACT"}
        and statuses["embedding_status"] == "SKIPPED_NO_EVIDENCE"
        and statuses["translation_status"] == "SKIPPED_NO_EVIDENCE"
    ):
        return ""
    non_ok = [field for field, value in statuses.items() if value != "OK"]
    return "" if not non_ok else "not_reviewable_" + "_and_".join(non_ok)


def _evidence_inventory(
    evidence_blobs: Iterable[tuple[str, bytes]],
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]], set[str], int]:
    expected: dict[str, set[str]] = defaultdict(set)
    blocked: dict[str, dict[str, str]] = defaultdict(dict)
    records_by_key: dict[str, tuple[dict[str, Any], str, int]] = {}
    body_objects: set[str] = set()
    superseded = 0
    for object_name, raw in evidence_blobs:
        for line_number, record in enumerate(_json_records(raw, object_name), start=1):
            if _clean(record.get("schema_version")) != "evidence.v1":
                raise ValueError(f"Unsupported Evidence schema in {object_name} at line {line_number}")
            record_id = _clean(record.get("record_id"))
            claim_id = _clean(record.get("claim_id"))
            if not record_id or not claim_id:
                raise ValueError(f"Evidence record in {object_name} lacks record_id or claim_id")
            claim_key = f"{record_id}::{claim_id}"
            source_body = _clean(record.get("source_body_object"))
            if source_body:
                body_objects.add(source_body)
            previous = records_by_key.get(claim_key)
            priority = (
                int(not _evidence_block_reason(record)),
                int(_clean(record.get("translation_status")).upper() == "OK"),
                _clean(record.get("retrieved_at")),
                object_name,
                line_number,
            )
            if previous is None or priority >= previous[3]:
                if previous is not None:
                    superseded += 1
                records_by_key[claim_key] = (record, object_name, line_number, priority)
    for claim_key, (record, _name, _line, _priority) in records_by_key.items():
        record_id = _clean(record.get("record_id"))
        expected[record_id].add(claim_key)
        reason = _evidence_block_reason(record)
        if reason:
            blocked[record_id][claim_key] = reason
    if not expected:
        raise ValueError("No Evidence JSONL claim records were found")
    return expected, blocked, body_objects, superseded


def _body_rows(body_blobs: Iterable[tuple[str, bytes]]) -> dict[str, dict[str, str]]:
    rows_by_id: dict[str, dict[str, str]] = {}
    for object_name, raw in body_blobs:
        if not object_name.lower().endswith(".csv"):
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Body object is not UTF-8: {object_name}") from exc
        reader = csv.DictReader(io.StringIO(text, newline=""))
        for source in reader:
            row = {str(key): _clean(value) for key, value in source.items() if key}
            record_id = row.get("record_id", "")
            if not record_id or not row.get("body", ""):
                continue
            previous = rows_by_id.get(record_id)
            if previous is None or len(row["body"]) >= len(previous.get("body", "")):
                rows_by_id[record_id] = row
    if not rows_by_id:
        raise ValueError("No Body rows with both record_id and body were found")
    return rows_by_id


def _claim_issues(row: dict[str, str]) -> list[str]:
    issues = [
        f"missing_{field}"
        for field in ("claim_id", "claim_text", "claim_materiality", "verification_note", "annotation_id", "annotator", "annotated_at")
        if not row.get(field)
    ]
    materiality = row.get("claim_materiality", "")
    if materiality and materiality not in {"central", "material", "secondary"}:
        issues.append("invalid_claim_materiality")
    if materiality == "secondary":
        return issues
    for field in ("evidence_relation", "claim_verdict"):
        if not row.get(field):
            issues.append(f"missing_{field}")
    expected_relation = {"RESPALDADO": "supports", "REFUTADO": "contradicts", "NO_DETERMINABLE": "unclear"}.get(
        row.get("claim_verdict", "")
    )
    if row.get("claim_verdict") not in {"RESPALDADO", "REFUTADO", "NO_DETERMINABLE"}:
        issues.append("invalid_claim_verdict")
    if row.get("evidence_relation") not in {"supports", "contradicts", "unclear"}:
        issues.append("invalid_evidence_relation")
    if expected_relation and row.get("evidence_relation") != expected_relation:
        issues.append("claim_relation_mismatch")
    evidence_status = row.get("evidence_status", "").upper()
    if evidence_status not in {"OK", "NO_RESULTS", "NO_ABSTRACT"}:
        issues.append("decisive_claim_without_reviewable_evidence")
    elif evidence_status in {"NO_RESULTS", "NO_ABSTRACT"}:
        if row.get("claim_verdict") != "NO_DETERMINABLE":
            issues.append("decisive_verdict_without_pubmed_evidence")
        if row.get("evidence_relation") != "unclear":
            issues.append("decisive_relation_without_pubmed_evidence")
        if row.get("selected_pmids"):
            issues.append("pmid_without_pubmed_evidence")
    elif not row.get("selected_pmids"):
        issues.append("missing_selected_pmids")
    return issues


def _aggregate_record(record_id: str, claims: list[dict[str, str]], body: dict[str, str]) -> dict[str, str]:
    issues = [issue for claim in claims for issue in _claim_issues(claim)]
    decisive = [claim for claim in claims if claim.get("claim_materiality") in {"central", "material"}]
    if not decisive:
        issues.append("no_central_or_material_claim")
    if issues:
        label, relation, status, reason = "NO_DETERMINABLE", "unclear", "REQUIERE_CORRECCION", ";".join(sorted(set(issues)))
        decisive_for_audit = decisive or claims
    else:
        contradicted = [claim for claim in decisive if claim.get("claim_verdict") == "REFUTADO"]
        supported = [claim for claim in decisive if claim.get("claim_verdict") == "RESPALDADO"]
        if contradicted:
            label, relation, reason, decisive_for_audit = "REFUTADA", "contradicts", "Al menos un claim central/material fue refutado.", contradicted
        elif len(supported) == len(decisive):
            label, relation, reason, decisive_for_audit = "RESPALDADA", "supports", "Todos los claims centrales/materiales fueron respaldados.", supported
        else:
            label, relation, reason, decisive_for_audit = "NO_DETERMINABLE", "unclear", "Al menos un claim central/material no pudo determinarse.", decisive
        status = "COMPLETADA"
    value = lambda field: body.get(field, "") or next((claim.get(field, "") for claim in claims if claim.get(field)), "")
    selected = [claim.get("selected_pmids", "") for claim in decisive_for_audit if claim.get("selected_pmids")]
    notes = [claim.get("verification_note", "") for claim in decisive_for_audit if claim.get("verification_note")]
    annotated = [claim.get("annotated_at", "") for claim in decisive_for_audit if claim.get("annotated_at")]
    return {
        "record_id": record_id,
        "title": value("title"),
        "body": body.get("body", ""),
        "label": label,
        "evidence_relation": relation,
        "aggregation_status": status,
        "aggregation_reason": reason,
        "claim_text": " | ".join(claim.get("claim_text", "") for claim in decisive_for_audit if claim.get("claim_text")),
        "best_pmid": ";".join(dict.fromkeys(selected)) or next((claim.get("best_pmid", "") for claim in decisive_for_audit if claim.get("best_pmid")), ""),
        "evidence_status": "REQUIERE_CORRECCION" if issues else "OK" if {_clean(claim.get("evidence_status")).upper() for claim in decisive} == {"OK"} else "NO_DETERMINABLE",
        "claims_total": str(len(claims)),
        "decisive_claims_total": str(len(decisive)),
        "claims_reviewed_total": str(len(claims)),
        "verification_note": "\n".join([reason, *notes]).strip(),
        "annotated_at": max(annotated, default=""),
    }


def _secondary_blocked(blocked: dict[str, dict[str, str]], reviews: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], int]:
    resolved = {
        (_clean(row.get("record_id")), _clean(row.get("claim_key")))
        for row in reviews
        if row.get("claim_materiality") == "secondary" and not _claim_issues(row)
    }
    remaining: dict[str, dict[str, str]] = {}
    resolved_count = 0
    for record_id, claim_reasons in blocked.items():
        for claim_key, reason in claim_reasons.items():
            if (record_id, claim_key) in resolved:
                resolved_count += 1
            else:
                remaining.setdefault(record_id, {})[claim_key] = reason
    return remaining, resolved_count


def build_training(export_raw: bytes, export_name: str, evidence_blobs: list[tuple[str, bytes]], body_blobs: list[tuple[str, bytes]]) -> dict[str, Any]:
    tasks = _json_records(export_raw, export_name)
    task_claims = _task_claim_keys(tasks)
    expected, blocked, _body_objects, superseded = _evidence_inventory(evidence_blobs)
    task_keys = {key for keys in task_claims.values() for key in keys}
    evidence_keys = {key for keys in expected.values() for key in keys}
    missing = sorted(task_keys - evidence_keys)
    if missing:
        raise ValueError("Evidence does not cover Label Studio task(s): " + ", ".join(missing[:5]))
    bodies = _body_rows(body_blobs)
    reviews = _claim_reviews(tasks)
    blocked, secondary_without_evidence = _secondary_blocked(blocked, reviews)
    review_by_record: dict[str, list[dict[str, str]]] = defaultdict(list)
    reviewed_keys: dict[str, set[str]] = defaultdict(set)
    for row in reviews:
        review_by_record[row["record_id"]].append(row)
        reviewed_keys[row["record_id"]].add(row["claim_key"])
    articles: list[dict[str, str]] = []
    pending_total = 0
    blocked_total = 0
    for record_id in sorted(set(expected) | set(review_by_record)):
        claim_keys = expected.get(record_id, set())
        reviewable = claim_keys - set(blocked.get(record_id, {}))
        pending = reviewable - reviewed_keys.get(record_id, set())
        blocked_count = len(blocked.get(record_id, {}))
        pending_total += len(pending)
        blocked_total += blocked_count
        article = _aggregate_record(record_id, review_by_record.get(record_id, []), bodies.get(record_id, {}))
        article["claims_total"] = str(len(claim_keys))
        article["claims_reviewed_total"] = str(len(reviewable & reviewed_keys.get(record_id, set())))
        if blocked_count:
            reasons = Counter(blocked[record_id].values())
            article.update({"label": "NO_DETERMINABLE", "evidence_relation": "unclear", "evidence_status": "PENDIENTE_EVIDENCIA", "aggregation_status": "PENDIENTE_EVIDENCIA", "aggregation_reason": f"{blocked_count} claim(s) sin evidencia revisable: " + ", ".join(f"{n} {reason}" for reason, n in sorted(reasons.items())), "verification_note": article.get("aggregation_reason", "")})
        elif pending:
            article.update({"label": "NO_DETERMINABLE", "evidence_relation": "unclear", "evidence_status": "PENDIENTE_REVISION", "aggregation_status": "PENDIENTE_REVISION", "aggregation_reason": f"{len(pending)} claim(s) sin revisión humana completa.", "verification_note": f"{len(pending)} claim(s) sin revisión humana completa."})
        articles.append(article)
    rejected: list[dict[str, str]] = []
    training: list[dict[str, str]] = []
    for article in articles:
        reasons: list[str] = []
        if article.get("aggregation_status") != "COMPLETADA":
            reasons.append(article.get("aggregation_status", "REQUIERE_CORRECCION"))
        if article.get("label") in {"RESPALDADA", "REFUTADA"}:
            for field in ("title", "body", "best_pmid"):
                if not article.get(field):
                    reasons.append(f"missing_{field}_for_binary_label")
        if reasons:
            rejected.append({"record_id": article.get("record_id", ""), "label": article.get("label", ""), "aggregation_status": article.get("aggregation_status", ""), "rejection_reasons": ";".join(dict.fromkeys(reasons))})
        elif article.get("label") in {"RESPALDADA", "REFUTADA"}:
            training.append({field: article.get(field, "") for field in TRAINING_FIELDS})
    return {
        "status": "ok" if pending_total == 0 and blocked_total == 0 and not rejected else "requires_review",
        "tasks_in_export": sum(len(keys) for keys in task_claims.values()),
        "claims_total": sum(len(keys) for keys in expected.values()),
        "claim_reviews_completed": len(reviews),
        "claims_pending": pending_total,
        "claims_without_reviewable_evidence": blocked_total,
        "claims_secondary_without_evidence_review": secondary_without_evidence,
        "evidence_revisions_superseded": superseded,
        "articles_total": len(articles),
        "training_records": len(training),
        "rejected_records": len(rejected),
        "training": training,
        "rejected": rejected,
    }
