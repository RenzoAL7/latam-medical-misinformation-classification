#!/usr/bin/env python3
"""Audita un export JSON de anotaciones antes de congelar las referencias de Sem1.

El script no selecciona ni descarta noticias "parecidas". Separa los
duplicados exactos (que no deben entrar dos veces al conjunto de referencia) de las parejas
similares, que se exportan para revisión humana antes de cerrar el manifiesto.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Any


APTA = {"APTA_CON_CLAIM", "APTA_CON_CLAIM_LEGACY"}
WORD_RE = re.compile(r"\w+", flags=re.UNICODE)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _tasks_from_export(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return [item for item in payload["tasks"] if isinstance(item, dict)]
    raise ValueError("El export debe ser un JSON de tareas de anotación.")


def _latest_completed_annotation(task: dict[str, Any]) -> dict[str, Any] | None:
    completed = [
        annotation
        for annotation in task.get("annotations", [])
        if not annotation.get("was_cancelled") and annotation.get("result")
    ]
    return max(completed, key=lambda annotation: str(annotation.get("created_at") or "")) if completed else None


def _result_choice(results: list[dict[str, Any]], names: tuple[str, ...]) -> str:
    for name in names:
        choices = [
            (result.get("value") or {}).get("choices")
            for result in results
            if result.get("from_name") == name
        ]
        for choice in reversed(choices):
            if isinstance(choice, list) and choice:
                return _clean(choice[0])
    return ""


def _result_text(results: list[dict[str, Any]], name: str) -> str:
    values: list[str] = []
    for result in results:
        if result.get("from_name") != name:
            continue
        text = (result.get("value") or {}).get("text")
        if isinstance(text, list):
            values.extend(_clean(item) for item in text if _clean(item))
        elif _clean(text):
            values.append(_clean(text))
    return " ".join(values)


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", _clean(value).lower())
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return "".join(
        character if character.isalnum() else " " for character in without_accents
    ).strip()


def _normalize_words(value: str) -> str:
    return " ".join(_normalized(value).split())


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _token_set(value: str, *, limit: int | None = None) -> set[str]:
    tokens = WORD_RE.findall(_normalize_words(value))
    return set(tokens[:limit]) if limit else set(tokens)


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _canonical_eligibility(value: str, claim: str) -> str:
    if value == "APTA":
        return "APTA_CON_CLAIM_LEGACY"
    if not value and claim:
        return "APTA_CON_CLAIM_LEGACY"
    return value


def _group_duplicates(
    rows: list[dict[str, str]], field: str, prefix: str
) -> tuple[dict[str, str], list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        value = row[field]
        if value:
            groups[value].append(row)

    assignments: dict[str, str] = {}
    reviews: list[dict[str, str]] = []
    group_number = 0
    for value, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        group_number += 1
        group_id = f"{prefix}-{group_number:03d}"
        for member in members:
            assignments[member["record_id"]] = group_id
        for left, right in combinations(sorted(members, key=lambda row: row["record_id"]), 2):
            reviews.append(
                {
                    "duplicate_type": f"exact_{field.removesuffix('_hash').removesuffix('_normalized')}",
                    "group_id": group_id,
                    "record_id_a": left["record_id"],
                    "record_id_b": right["record_id"],
                    "similarity": "1.000",
                    "title_a": left["title"],
                    "title_b": right["title"],
                    "claim_a": left["reference_claim"],
                    "claim_b": right["reference_claim"],
                    "recommended_action": "No incluir ambas filas en el conjunto de referencia.",
                }
            )
    return assignments, reviews


def _near_duplicate_reviews(
    rows: list[dict[str, str]], claim_threshold: float, source_threshold: float
) -> list[dict[str, str]]:
    candidates = [row for row in rows if row["eligible_for_reference_set"] == "yes"]
    for row in candidates:
        row["_claim_tokens"] = _token_set(row["reference_claim"])
        source_for_review = f"{row['title']} {row['body']}"
        row["_source_tokens"] = _token_set(source_for_review, limit=240)

    reviews: list[dict[str, str]] = []
    for left, right in combinations(candidates, 2):
        if left["exact_claim_duplicate_group"] or left["exact_source_duplicate_group"]:
            if left["exact_claim_duplicate_group"] == right["exact_claim_duplicate_group"]:
                continue
            if left["exact_source_duplicate_group"] == right["exact_source_duplicate_group"]:
                continue

        claim_ratio = SequenceMatcher(
            None, left["normalized_claim"], right["normalized_claim"]
        ).ratio()
        claim_jaccard = _jaccard(left["_claim_tokens"], right["_claim_tokens"])
        source_jaccard = _jaccard(left["_source_tokens"], right["_source_tokens"])
        if max(claim_ratio, claim_jaccard) >= claim_threshold:
            reviews.append(
                {
                    "duplicate_type": "near_claim",
                    "group_id": "",
                    "record_id_a": left["record_id"],
                    "record_id_b": right["record_id"],
                    "similarity": f"{max(claim_ratio, claim_jaccard):.3f}",
                    "title_a": left["title"],
                    "title_b": right["title"],
                    "claim_a": left["reference_claim"],
                    "claim_b": right["reference_claim"],
                    "recommended_action": "Revisar si ambos claims representan ejemplos distintos.",
                }
            )
        elif source_jaccard >= source_threshold:
            reviews.append(
                {
                    "duplicate_type": "near_source",
                    "group_id": "",
                    "record_id_a": left["record_id"],
                    "record_id_b": right["record_id"],
                    "similarity": f"{source_jaccard:.3f}",
                    "title_a": left["title"],
                    "title_b": right["title"],
                    "claim_a": left["reference_claim"],
                    "claim_b": right["reference_claim"],
                    "recommended_action": "Revisar si son la misma noticia republicada o sindicada.",
                }
            )
    for row in rows:
        row.pop("_claim_tokens", None)
        row.pop("_source_tokens", None)
    return reviews


def _write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Export JSON de anotaciones.")
    parser.add_argument("--audit-output", required=True, help="CSV con las 700 tareas auditadas.")
    parser.add_argument(
        "--duplicates-output", required=True, help="CSV de pares a revisar por duplicidad."
    )
    parser.add_argument("--summary-output", required=True, help="Resumen JSON de la auditoría.")
    parser.add_argument("--claim-similarity-threshold", type=float, default=0.90)
    parser.add_argument("--source-similarity-threshold", type=float, default=0.92)
    parser.add_argument("--min-claim-words", type=int, default=5)
    parser.add_argument("--max-claim-words", type=int, default=80)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.min_claim_words <= 0 or args.max_claim_words < args.min_claim_words:
        raise ValueError("Los límites de palabras para el claim no son válidos.")
    payload = json.loads(Path(args.input).expanduser().read_text(encoding="utf-8"))
    tasks = _tasks_from_export(payload)
    seen_record_ids: set[str] = set()
    rows: list[dict[str, str]] = []

    for task in tasks:
        data = task.get("data") or {}
        record_id = _clean(data.get("record_id"))
        if not record_id:
            raise ValueError("Una tarea exportada no contiene data.record_id.")
        if record_id in seen_record_ids:
            raise ValueError(f"record_id duplicado en el export: {record_id}")
        seen_record_ids.add(record_id)

        annotation = _latest_completed_annotation(task)
        results = annotation.get("result", []) if annotation else []
        claim = _result_text(results, "reference_claim")
        raw_eligibility = _result_choice(results, ("news_eligibility", "news_utility"))
        eligibility = _canonical_eligibility(raw_eligibility, claim)
        title = _clean(data.get("title"))
        body = _clean(data.get("body"))
        source_text = "\n\n".join(part for part in (title, body) if part)
        issues: list[str] = []
        if annotation is None:
            issues.append("sin_anotacion_completa")
        elif not raw_eligibility and claim:
            issues.append("elegibilidad_legacy_inferida_desde_reference_claim")
        elif eligibility in APTA and not claim:
            issues.append("apta_sin_reference_claim")
        elif eligibility not in APTA and claim:
            issues.append("reference_claim_sin_elegibilidad_apta")
        elif eligibility in APTA and not source_text:
            issues.append("apta_sin_title_ni_body")
        claim_word_count = len(claim.split())
        if claim and claim_word_count < args.min_claim_words:
            issues.append("reference_claim_menos_de_minimo_palabras")
        if claim_word_count > args.max_claim_words:
            issues.append("reference_claim_excede_maximo_palabras")

        rows.append(
            {
                "task_id": _clean(task.get("id")),
                "record_id": record_id,
                "annotation_id": _clean(annotation.get("id")) if annotation else "",
                "annotation_created_at": _clean(annotation.get("created_at")) if annotation else "",
                "annotation_count": str(len(task.get("annotations", []))),
                "completed_annotation_count": str(
                    sum(
                        not item.get("was_cancelled") and bool(item.get("result"))
                        for item in task.get("annotations", [])
                    )
                ),
                "country": _clean(data.get("country")),
                "published_at": _clean(data.get("published_at")),
                "source_name": _clean(data.get("source_name")),
                "topic": _clean(data.get("topic")),
                "title": title,
                "subtitle_or_bajada": _clean(data.get("subtitle_or_bajada")),
                "body": body,
                "news_eligibility_raw": raw_eligibility,
                "news_eligibility": eligibility,
                "reference_claim": claim,
                "reference_claim_word_count": str(claim_word_count),
                "source_hash": _short_hash(source_text),
                "normalized_title": _normalize_words(title),
                "normalized_claim": _normalize_words(claim),
                "claim_hash": _short_hash(_normalize_words(claim)),
                "eligible_for_reference_set": "yes"
                if eligibility in APTA
                and claim
                and source_text
                and args.min_claim_words <= claim_word_count <= args.max_claim_words
                else "no",
                "quality_issue": ";".join(issues),
                "exact_source_duplicate_group": "",
                "exact_title_duplicate_group": "",
                "exact_claim_duplicate_group": "",
            }
        )

    source_assignments, source_reviews = _group_duplicates(rows, "source_hash", "source")
    title_assignments, title_reviews = _group_duplicates(rows, "normalized_title", "title")
    claim_assignments, claim_reviews = _group_duplicates(rows, "claim_hash", "claim")
    for row in rows:
        row["exact_source_duplicate_group"] = source_assignments.get(row["record_id"], "")
        row["exact_title_duplicate_group"] = title_assignments.get(row["record_id"], "")
        row["exact_claim_duplicate_group"] = claim_assignments.get(row["record_id"], "")

    near_reviews = _near_duplicate_reviews(
        rows, args.claim_similarity_threshold, args.source_similarity_threshold
    )
    reviews = source_reviews + title_reviews + claim_reviews + near_reviews
    reviews.sort(key=lambda row: (row["duplicate_type"], row["record_id_a"], row["record_id_b"]))

    audit_fields = list(rows[0]) if rows else []
    review_fields = [
        "duplicate_type",
        "group_id",
        "record_id_a",
        "record_id_b",
        "similarity",
        "title_a",
        "title_b",
        "claim_a",
        "claim_b",
        "recommended_action",
    ]
    _write_csv(Path(args.audit_output).expanduser(), rows, audit_fields)
    _write_csv(Path(args.duplicates_output).expanduser(), reviews, review_fields)

    eligible_rows = [row for row in rows if row["eligible_for_reference_set"] == "yes"]
    unique_by_source: dict[str, dict[str, str]] = {}
    for row in sorted(eligible_rows, key=lambda candidate: candidate["record_id"]):
        unique_by_source.setdefault(row["source_hash"], row)
    unique_by_claim: dict[str, dict[str, str]] = {}
    for row in sorted(unique_by_source.values(), key=lambda candidate: candidate["record_id"]):
        unique_by_claim.setdefault(row["claim_hash"], row)
    summary = {
        "tasks": len(rows),
        "tasks_with_completed_annotation": sum(bool(row["annotation_id"]) for row in rows),
        "annotation_records": sum(int(row["annotation_count"]) for row in rows),
        "eligibility_counts": dict(sorted(Counter(row["news_eligibility"] for row in rows).items())),
        "with_reference_claim": sum(bool(row["reference_claim"]) for row in rows),
        "eligible_for_reference_set_before_exact_deduplication": len(eligible_rows),
        "eligible_after_exact_source_and_claim_deduplication": len(unique_by_claim),
        "quality_issue_counts": dict(
            sorted(Counter(row["quality_issue"] for row in rows if row["quality_issue"]).items())
        ),
        "exact_duplicate_groups": {
            "source": len(set(source_assignments.values())),
            "title": len(set(title_assignments.values())),
            "claim": len(set(claim_assignments.values())),
        },
        "duplicate_review_pairs": dict(
            sorted(Counter(row["duplicate_type"] for row in reviews).items())
        ),
        "thresholds": {
            "claim_similarity": args.claim_similarity_threshold,
            "source_similarity": args.source_similarity_threshold,
            "min_claim_words": args.min_claim_words,
            "max_claim_words": args.max_claim_words,
        },
    }
    summary_path = Path(args.summary_output).expanduser()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
