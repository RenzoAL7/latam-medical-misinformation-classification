#!/usr/bin/env python3
"""Build auditable article labels and a binary training CSV in one command.

Put the three local inputs for one review batch in a directory:

* ``labelstudio_export.json`` or ``labelstudio_export.jsonl``: the completed
  Label Studio task export; and
* ``evidence.jsonl`` (or an ``evidence/`` directory): every Evidence JSONL
  that supplied claims for that Label Studio export; and
* ``body.csv`` (or a ``body/`` directory with Body CSV files): the Body output
  that contains the article text.

Evidence and Label Studio serve different safety checks: Evidence is the
complete inventory of extracted claims and says whether each was reviewable;
Label Studio contains the human decisions for reviewable claims.  Evidence
alone is intentionally not enough because it has no human annotations and
does not repeat the full article body.  This script joins both inputs to Body
by ``record_id``, aggregates all claims of each article, and writes three CSVs
in ``output/`` by default:

* ``claim_reviews.csv``: one completed human review per claim;
* ``article_labels.csv``: one auditable aggregation per article; and
* ``training.csv``: only complete ``RESPALDADA``/``REFUTADA`` articles with
  ``record_id,title,body,label,dataset_split`` for the classifier.

If an article has an unreviewed task, an Evidence claim that was not
reviewable, or an invalid review, the reports are still written, but the
command exits with status 2 and excludes that article from ``training.csv``.
This prevents a partially reviewed article from being silently treated as Gold
data.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
LABELSTUDIO_DIR = ROOT / "labelstudio"


def _load_module(name: str, filename: str) -> Any:
    """Load the existing, tested claim export/aggregation helpers."""

    sys.path.insert(0, str(LABELSTUDIO_DIR))
    path = LABELSTUDIO_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORTER = _load_module("export_annotations", "export_annotations.py")
AGGREGATOR = _load_module("aggregate_article_labels", "aggregate_article_labels.py")
VALIDATOR = _load_module("validate_gold", "validate_gold.py")


EXPORT_FILENAMES = ("labelstudio_export.json", "labelstudio_export.jsonl")
EVIDENCE_FILENAMES = ("evidence.jsonl",)
BODY_FILENAMES = ("body.csv", "body.jsonl")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL in {path} at line {line_number}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"JSONL record {line_number} in {path} is not an object")
        records.append(record)
    return records


def _read_labelstudio_export(path: Path) -> Any:
    if path.suffix.lower() == ".jsonl":
        return _read_jsonl(path)
    return EXPORTER._read_json_export(path)


def _default_export_path(input_dir: Path) -> Path:
    found = [input_dir / filename for filename in EXPORT_FILENAMES if (input_dir / filename).is_file()]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ValueError(
            "Found both labelstudio_export.json and labelstudio_export.jsonl. "
            "Keep only one, or pass --labelstudio-export explicitly."
        )
    expected = " or ".join(EXPORT_FILENAMES)
    raise ValueError(f"Missing Label Studio export in {input_dir}. Expected {expected}.")


def _default_body_inputs(input_dir: Path) -> list[Path]:
    body_dir = input_dir / "body"
    if body_dir.is_dir():
        return [body_dir]
    found = [input_dir / filename for filename in BODY_FILENAMES if (input_dir / filename).is_file()]
    if len(found) == 1:
        return found
    if len(found) > 1:
        raise ValueError(
            "Found both body.csv and body.jsonl. Keep one, or pass --body-input explicitly."
        )
    raise ValueError(
        f"Missing Body input in {input_dir}. Add body.csv or a body/ directory. "
        "Evidence JSONL intentionally does not duplicate article bodies."
    )


def _default_evidence_inputs(input_dir: Path) -> list[Path]:
    evidence_dir = input_dir / "evidence"
    if evidence_dir.is_dir():
        return [evidence_dir]
    found = [input_dir / filename for filename in EVIDENCE_FILENAMES if (input_dir / filename).is_file()]
    if len(found) == 1:
        return found
    raise ValueError(
        f"Missing Evidence input in {input_dir}. Add evidence.jsonl or an evidence/ directory. "
        "It is required to prove that every extracted claim was considered."
    )


def _body_paths(values: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        if value.is_dir():
            paths.extend(sorted(value.rglob("*.csv")))
            paths.extend(sorted(value.rglob("*.jsonl")))
        elif value.is_file():
            paths.append(value)
        else:
            raise FileNotFoundError(f"Body input does not exist: {value}")
    return sorted(set(paths))


def _evidence_paths(values: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        if value.is_dir():
            paths.extend(sorted(value.rglob("*.jsonl")))
        elif value.is_file() and value.suffix.lower() == ".jsonl":
            paths.append(value)
        elif value.exists():
            raise ValueError(f"Evidence input must be a JSONL file or directory: {value}")
        else:
            raise FileNotFoundError(f"Evidence input does not exist: {value}")
    paths = sorted(set(paths))
    if not paths:
        raise ValueError("No Evidence JSONL files were found in the provided input.")
    return paths


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _body_candidate(record: dict[str, Any]) -> dict[str, str]:
    """Accept Body CSV rows and an optional direct JSONL body envelope."""

    article = record.get("article")
    nested = article if isinstance(article, dict) else {}
    source = {**nested, **record}
    return {str(key): _clean(value) for key, value in source.items() if key}


def _load_body_rows(values: Iterable[Path]) -> dict[str, dict[str, str]]:
    rows_by_id: dict[str, dict[str, str]] = {}
    for path in _body_paths(values):
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                source_rows: Iterable[dict[str, Any]] = list(csv.DictReader(handle))
        elif path.suffix.lower() == ".jsonl":
            source_rows = _read_jsonl(path)
        else:  # pragma: no cover - _body_paths currently constrains suffixes
            continue
        for source in source_rows:
            row = _body_candidate(source)
            record_id = row.get("record_id", "")
            if not record_id or not row.get("body", ""):
                continue
            existing = rows_by_id.get(record_id, {})
            if not existing or len(row["body"]) >= len(existing.get("body", "")):
                rows_by_id[record_id] = row
    if not rows_by_id:
        raise ValueError("No Body rows with both record_id and body were found in the provided input.")
    return rows_by_id


def _task_identity(task: dict[str, Any]) -> tuple[str, str, str] | None:
    data = task.get("data")
    if not isinstance(data, dict):
        return None
    record_id = _clean(data.get("record_id"))
    claim_id = _clean(data.get("claim_id"))
    claim_key = _clean(data.get("claim_key")) or f"{record_id}::{claim_id}"
    if not record_id or not claim_id:
        return None
    return record_id, claim_id, claim_key


def _expected_claim_keys(payload: Any) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = defaultdict(set)
    for task in EXPORTER._task_rows(payload):
        identity = _task_identity(task)
        if identity is None:
            continue
        record_id, _claim_id, claim_key = identity
        expected[record_id].add(claim_key)
    if not expected:
        raise ValueError(
            "The export has no multi-claim tasks (record_id + claim_id). "
            "Use the MedNews LATAM — Multi-Claim project export."
        )
    return expected


def _evidence_identity(record: dict[str, Any], path: Path, line_number: int) -> tuple[str, str, str]:
    if _clean(record.get("schema_version")) != "evidence.v1":
        raise ValueError(f"Unsupported Evidence schema in {path} at line {line_number}; expected evidence.v1.")
    record_id = _clean(record.get("record_id"))
    claim_id = _clean(record.get("claim_id"))
    if not record_id or not claim_id:
        raise ValueError(f"Evidence record in {path} at line {line_number} lacks record_id or claim_id.")
    return record_id, claim_id, f"{record_id}::{claim_id}"


def _evidence_block_reason(record: dict[str, Any]) -> str:
    """Return why one claim cannot support a decisive human verdict yet."""

    llm_status = _clean(record.get("llm_status")).upper()
    evidence_status = _clean(record.get("evidence_status")).upper()
    embedding_status = _clean(record.get("embedding_status")).upper()
    translation_status = _clean(record.get("translation_status")).upper()

    # No candidates/usable abstracts is a terminal research outcome, not a
    # technical failure. The reviewer can triage the claim and, if decisive,
    # record NO_DETERMINABLE without inventing a PMID.
    if (
        llm_status == "OK"
        and evidence_status in {"NO_RESULTS", "NO_ABSTRACT"}
        and embedding_status == "SKIPPED_NO_EVIDENCE"
        and translation_status == "SKIPPED_NO_EVIDENCE"
    ):
        return ""

    non_ok = [
        field
        for field in ("llm_status", "evidence_status", "embedding_status", "translation_status")
        if _clean(record.get(field)).upper() != "OK"
    ]
    return "" if not non_ok else "not_reviewable_" + "_and_".join(non_ok)


def _evidence_revision_priority(record: dict[str, Any], path: Path, line_number: int) -> tuple[int, int, str, str, int]:
    """Prefer a recovered/reviewable revision when a claim appears in many runs."""

    reviewable = not _evidence_block_reason(record)
    translation_ok = _clean(record.get("translation_status")).upper() == "OK"
    retrieved_at = _clean(record.get("retrieved_at"))
    # A final continuation artifact normally has a later retrieved_at value.
    # Path/line make the decision deterministic when older records lack it.
    return (int(reviewable), int(translation_ok), retrieved_at, str(path), line_number)


def _load_evidence_inventory(
    values: Iterable[Path],
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]], list[Path], int]:
    """Read every extracted claim, including claims excluded from Label Studio.

    A claim absent from Label Studio is not equivalent to a claim that was
    reviewed. The Evidence inventory lets the builder preserve that distinction
    and block a binary article label until its complete set of claims is
    accounted for.
    """

    expected: dict[str, set[str]] = defaultdict(set)
    blocked: dict[str, dict[str, str]] = defaultdict(dict)
    records_by_key: dict[str, tuple[dict[str, Any], Path, int]] = {}
    superseded_revisions = 0
    paths = _evidence_paths(values)
    for path in paths:
        for line_number, record in enumerate(_read_jsonl(path), start=1):
            record_id, _claim_id, claim_key = _evidence_identity(record, path, line_number)
            previous = records_by_key.get(claim_key)
            if previous is None or _evidence_revision_priority(record, path, line_number) >= _evidence_revision_priority(
                *previous
            ):
                records_by_key[claim_key] = (record, path, line_number)
            if previous is not None:
                superseded_revisions += 1
    for claim_key, (record, path, line_number) in sorted(records_by_key.items()):
        record_id, _claim_id, validated_key = _evidence_identity(record, path, line_number)
        if validated_key != claim_key:
            raise ValueError(f"Evidence claim identity changed while loading {path}")
        expected[record_id].add(claim_key)
        reason = _evidence_block_reason(record)
        if reason:
            blocked[record_id][claim_key] = reason
    if not expected:
        raise ValueError("No claim records were found in the supplied Evidence JSONL input.")
    return expected, blocked, paths, superseded_revisions


def _pending_claims(expected: dict[str, set[str]], reviews: list[dict[str, Any]]) -> dict[str, set[str]]:
    reviewed_by_record: dict[str, set[str]] = defaultdict(set)
    for row in reviews:
        record_id = _clean(row.get("record_id"))
        claim_key = _clean(row.get("claim_key"))
        if record_id and claim_key:
            reviewed_by_record[record_id].add(claim_key)
    return {
        record_id: claim_keys - reviewed_by_record.get(record_id, set())
        for record_id, claim_keys in expected.items()
        if claim_keys - reviewed_by_record.get(record_id, set())
    }


def _exclude_reviewed_secondary_from_evidence_blocks(
    blocked: dict[str, dict[str, str]], reviews: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, str]], int]:
    """Let a complete secondary triage resolve a technical evidence block."""

    secondary_keys = {
        (_clean(row.get("record_id")), _clean(row.get("claim_key")))
        for row in reviews
        if _clean(row.get("claim_materiality")) == "secondary"
        and not AGGREGATOR._claim_issues(row)
    }
    remaining = {
        record_id: {
            claim_key: reason
            for claim_key, reason in claim_reasons.items()
            if (record_id, claim_key) not in secondary_keys
        }
        for record_id, claim_reasons in blocked.items()
    }
    remaining = {record_id: reasons for record_id, reasons in remaining.items() if reasons}
    blocked_keys = {
        (record_id, claim_key)
        for record_id, claim_reasons in blocked.items()
        for claim_key in claim_reasons
    }
    return remaining, len(secondary_keys & blocked_keys)


def _mark_pending_articles(
    articles: list[dict[str, str]],
    expected: dict[str, set[str]],
    pending: dict[str, set[str]],
    blocked: dict[str, dict[str, str]],
    body_rows: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    by_id = {row["record_id"]: dict(row) for row in articles}
    for record_id, claim_keys in expected.items():
        article = by_id.get(record_id)
        if article is None:
            body = body_rows.get(record_id, {})
            article = {
                field: _clean(body.get(field, ""))
                for field in AGGREGATOR.OUTPUT_FIELDS
            }
            article["record_id"] = record_id
            article["title"] = _clean(body.get("title", ""))
            article["body"] = _clean(body.get("body", ""))
            article["country"] = _clean(body.get("country", ""))
            article["source_name"] = _clean(body.get("source_name", ""))
            article["canonical_url"] = _clean(body.get("canonical_url", ""))
            article["published_at"] = _clean(body.get("published_at", ""))
            article["topic"] = _clean(body.get("topic", ""))
            by_id[record_id] = article
        article["claims_total"] = str(len(claim_keys))
        article["claims_reviewed_total"] = str(
            len(claim_keys) - len(pending.get(record_id, set())) - len(blocked.get(record_id, {}))
        )
        if record_id in blocked:
            blocked_reasons = Counter(blocked[record_id].values())
            reason_summary = ", ".join(
                f"{count} {reason}" for reason, count in sorted(blocked_reasons.items())
            )
            article.update(
                {
                    "label": "NO_DETERMINABLE",
                    "evidence_relation": "unclear",
                    "evidence_status": "PENDIENTE_EVIDENCIA",
                    "aggregation_status": "PENDIENTE_EVIDENCIA",
                    "aggregation_reason": (
                        f"{len(blocked[record_id])} claim(s) sin evidencia revisable: {reason_summary}."
                    ),
                }
            )
            article["verification_note"] = article["aggregation_reason"]
            continue
        if record_id not in pending:
            continue
        pending_count = len(pending[record_id])
        article.update(
            {
                "label": "NO_DETERMINABLE",
                "evidence_relation": "unclear",
                "evidence_status": "PENDIENTE_REVISION",
                "aggregation_status": "PENDIENTE_REVISION",
                "aggregation_reason": f"{pending_count} claim(s) sin revisión humana completa.",
            }
        )
        article["verification_note"] = article["aggregation_reason"]
    return [by_id[record_id] for record_id in sorted(by_id)]


def _article_rejection_reasons(row: dict[str, str]) -> list[str]:
    reasons: list[str] = []
    if row.get("aggregation_status") != "COMPLETADA":
        reasons.append(row.get("aggregation_status", "REQUIERE_CORRECCION"))
    if row.get("label") in {"RESPALDADA", "REFUTADA"} and not row.get("body"):
        reasons.append("missing_body_for_binary_label")
    if row.get("label") in {"RESPALDADA", "REFUTADA"} and not row.get("title"):
        reasons.append("missing_title_for_binary_label")
    return reasons


def _write_csv(path: Path, fields: Iterable[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_dataset(
    export_path: Path,
    evidence_inputs: list[Path],
    body_inputs: list[Path],
    output_dir: Path,
) -> dict[str, Any]:
    """Build all local CSV artifacts and return a machine-readable summary."""

    payload = _read_labelstudio_export(export_path)
    task_claims = _expected_claim_keys(payload)
    expected, blocked, evidence_paths, superseded_revisions = _load_evidence_inventory(evidence_inputs)
    task_keys = {claim_key for claim_keys in task_claims.values() for claim_key in claim_keys}
    evidence_keys = {claim_key for claim_keys in expected.values() for claim_key in claim_keys}
    task_keys_without_evidence = sorted(task_keys - evidence_keys)
    if task_keys_without_evidence:
        preview = ", ".join(task_keys_without_evidence[:5])
        raise ValueError(
            "The supplied Evidence JSONL does not cover Label Studio task(s): "
            f"{preview}. Add the corresponding Evidence run before building Gold data."
        )
    body_rows = _load_body_rows(body_inputs)
    reviews = EXPORTER._claim_review_rows(payload)
    raw_blocked_count = sum(len(claims) for claims in blocked.values())
    blocked, secondary_without_evidence = _exclude_reviewed_secondary_from_evidence_blocks(
        blocked, reviews
    )
    reviewable = {
        record_id: claim_keys - set(blocked.get(record_id, {}))
        for record_id, claim_keys in expected.items()
    }
    pending = _pending_claims(reviewable, reviews)
    articles = AGGREGATOR.aggregate_rows(reviews, body_rows)
    articles = _mark_pending_articles(articles, expected, pending, blocked, body_rows)

    # ``validate_rows`` is deliberately still the final safety gate for the
    # two binary labels. Pending/invalid articles remain visible in the report,
    # but can never flow into training.csv.
    reviewed, training, rejected = VALIDATOR.validate_rows(articles)
    article_rejections = [
        {**row, "rejection_reasons": ";".join(_article_rejection_reasons(row))}
        for row in articles
        if _article_rejection_reasons(row)
    ]
    rejected_by_id = {row.get("record_id", ""): row for row in rejected}
    for row in article_rejections:
        existing = rejected_by_id.get(row.get("record_id", ""))
        if existing is None:
            rejected.append(row)
        else:
            existing["rejection_reasons"] = ";".join(
                sorted(
                    {
                        *filter(None, existing.get("rejection_reasons", "").split(";")),
                        *filter(None, row.get("rejection_reasons", "").split(";")),
                    }
                )
            )

    completed_by_id = {row.get("record_id", ""): row for row in reviewed}
    rejected_by_id = {row.get("record_id", ""): row for row in rejected}
    article_output_rows: list[dict[str, str]] = []
    for article in articles:
        record_id = article.get("record_id", "")
        completed = completed_by_id.get(record_id)
        rejected_row = rejected_by_id.get(record_id)
        if completed is not None:
            article_output_rows.append(
                {
                    **article,
                    "final_training_eligible": completed.get("final_training_eligible", "NO"),
                    "validation_status": completed.get("validation_status", "COMPLETADA"),
                }
            )
        else:
            article_output_rows.append(
                {
                    **article,
                    "final_training_eligible": "NO",
                    "validation_status": "REQUIERE_CORRECCION",
                    "aggregation_reason": rejected_row.get("rejection_reasons", article.get("aggregation_reason", ""))
                    if rejected_row
                    else article.get("aggregation_reason", ""),
                }
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "claim_reviews.csv", EXPORTER.CLAIM_REVIEW_FIELDS, reviews)
    article_fields = (*AGGREGATOR.OUTPUT_FIELDS, "claims_reviewed_total", "final_training_eligible", "validation_status")
    _write_csv(output_dir / "article_labels.csv", article_fields, article_output_rows)
    _write_csv(output_dir / "training.csv", VALIDATOR.TRAINING_FIELDS, training)
    _write_csv(output_dir / "rejected.csv", VALIDATOR.REJECT_FIELDS, rejected)

    status_counts = Counter(row.get("aggregation_status", "") for row in articles)
    summary = {
        "status": "ok" if not pending and not blocked and not rejected else "requires_review",
        "source_export": str(export_path),
        "evidence_inputs": [str(path) for path in evidence_paths],
        "body_inputs": [str(path) for path in body_inputs],
        "output_dir": str(output_dir),
        "claims_total": sum(len(keys) for keys in expected.values()),
        "tasks_in_export": sum(len(keys) for keys in task_claims.values()),
        "claim_reviews_completed": len(reviews),
        "claims_pending": sum(len(keys) for keys in pending.values()),
        "claims_without_reviewable_evidence": sum(len(claims) for claims in blocked.values()),
        "claims_secondary_without_evidence_review": secondary_without_evidence,
        "claims_with_technical_evidence_blocks": raw_blocked_count,
        "evidence_revisions_superseded": superseded_revisions,
        "articles_total": len(articles),
        "articles_by_aggregation_status": dict(sorted(status_counts.items())),
        "training_records": len(training),
        "rejected_records": len(rejected),
        "outputs": {
            "claim_reviews": str(output_dir / "claim_reviews.csv"),
            "article_labels": str(output_dir / "article_labels.csv"),
            "training": str(output_dir / "training.csv"),
            "rejected": str(output_dir / "rejected.csv"),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Folder containing the Label Studio export, Evidence JSONL, and Body input",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination for CSVs; defaults to <input_dir>/output",
    )
    parser.add_argument(
        "--labelstudio-export",
        type=Path,
        default=None,
        help="Use a differently named Label Studio JSON/JSONL export",
    )
    parser.add_argument(
        "--body-input",
        type=Path,
        action="append",
        default=[],
        help="Body CSV/JSONL file or directory; repeat if needed",
    )
    parser.add_argument(
        "--evidence-input",
        type=Path,
        action="append",
        default=[],
        help="Evidence JSONL file or directory; repeat if needed",
    )
    args = parser.parse_args()

    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"Input directory does not exist: {input_dir}")
    try:
        export_path = (args.labelstudio_export or _default_export_path(input_dir)).expanduser().resolve()
        body_inputs = [path.expanduser().resolve() for path in args.body_input] or _default_body_inputs(input_dir)
        evidence_inputs = [path.expanduser().resolve() for path in args.evidence_input] or _default_evidence_inputs(input_dir)
        output_dir = (args.output_dir or input_dir / "output").expanduser().resolve()
        summary = build_dataset(export_path, evidence_inputs, body_inputs, output_dir)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
