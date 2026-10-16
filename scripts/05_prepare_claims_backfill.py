#!/usr/bin/env python3
"""Prepare a controlled Claims backfill from already extracted Body CSVs.

This command deliberately does *not* fetch NewsData or crawl article URLs.
It selects valid ``extraction_status=OK`` rows from existing Body artifacts,
creates small flat CSVs under a local campaign directory, and writes a
manifest that records every requested, selected, missing, or unusable
``record_id``. Uploading each generated CSV to ``silver/body/`` starts only
the existing Body -> Claims -> Evidence event chain.

Use a Label Studio project export as ``--record-ids`` when a precise legacy
scope is required. The export is used only to recover its ``record_id`` list;
it is never treated as the source of the article text.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


BRONZE_FIELDS = [
    "record_id",
    "source_name",
    "canonical_url",
    "published_at",
    "title",
    "subtitle_or_bajada",
    "topic",
    "selection_status",
    "retrieved_at",
    "country",
]
BODY_FIELDS = [
    "body",
    "http_status",
    "extraction_method",
    "extraction_status",
    "body_char_count",
    "body_word_count",
    "extracted_at",
    "source_bronze_object",
    "extraction_run_id",
    "extraction_error",
]
OUTPUT_FIELDS = BRONZE_FIELDS + BODY_FIELDS
CAMPAIGN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,80}$")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _csv_paths(values: Iterable[str | Path]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value)
        if path.is_dir():
            paths.extend(sorted(candidate for candidate in path.rglob("*.csv") if candidate.is_file()))
        elif path.is_file() and path.suffix.lower() == ".csv":
            paths.append(path)
        elif not path.exists():
            raise FileNotFoundError(f"Body input does not exist: {path}")
        else:
            raise ValueError(f"Body input must be a CSV file or directory: {path}")
    paths = sorted(set(paths))
    if not paths:
        raise ValueError("No Body CSV files were found.")
    return paths


def _body_rows(values: Iterable[str | Path]) -> tuple[dict[str, dict[str, str]], set[str], dict[str, str], list[Path]]:
    """Read Body artifacts and keep the longest valid body per record_id."""

    selected: dict[str, dict[str, str]] = {}
    seen_ids: set[str] = set()
    rejected_reasons: dict[str, str] = {}
    paths = _csv_paths(values)
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                row = {field: _clean(raw.get(field, "")) for field in OUTPUT_FIELDS}
                record_id = row["record_id"]
                if not record_id:
                    continue
                seen_ids.add(record_id)
                if not row["body"]:
                    rejected_reasons.setdefault(record_id, "missing_body")
                    continue
                if row["extraction_status"].upper() != "OK":
                    status = row["extraction_status"].upper() or "MISSING"
                    rejected_reasons.setdefault(record_id, f"extraction_status_{status}")
                    continue
                current = selected.get(record_id)
                if current is None or (len(row["body"]), row["extracted_at"]) > (
                    len(current["body"]),
                    current["extracted_at"],
                ):
                    selected[record_id] = row
    return selected, seen_ids, rejected_reasons, paths


def _record_id_from_item(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    data = value.get("data")
    if isinstance(data, dict) and _clean(data.get("record_id")):
        return _clean(data.get("record_id"))
    return _clean(value.get("record_id"))


def _record_ids_from_json(value: Any) -> list[str]:
    if isinstance(value, list):
        return [record_id for item in value if (record_id := _record_id_from_item(item))]
    if isinstance(value, dict):
        direct = _record_id_from_item(value)
        if direct:
            return [direct]
        for key in ("tasks", "results", "items", "data"):
            nested = value.get(key)
            if isinstance(nested, (list, dict)):
                return _record_ids_from_json(nested)
    raise ValueError("The JSON input does not contain Label Studio tasks or record_id values.")


def _record_ids(path_value: str | Path) -> list[str]:
    path = Path(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"Record-id input does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows or "record_id" not in (rows[0] or {}):
            raise ValueError(f"CSV {path} must contain a record_id column.")
        values = [_clean(row.get("record_id")) for row in rows]
    elif suffix == ".jsonl":
        values = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                values.extend(_record_ids_from_json(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL in {path} at line {line_number}") from exc
    elif suffix == ".json":
        try:
            values = _record_ids_from_json(json.loads(path.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}") from exc
    else:
        raise ValueError("--record-ids must be a CSV, JSON, or JSONL file.")
    result = list(dict.fromkeys(record_id for record_id in values if record_id))
    if not result:
        raise ValueError(f"No record_id values were found in {path}.")
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_backfill(
    body_inputs: Iterable[str | Path],
    output_dir: str | Path,
    campaign_id: str,
    batch_size: int,
    record_ids_path: str | Path | None = None,
    include_all_valid: bool = False,
    object_prefix: str = "silver/body",
) -> dict[str, Any]:
    """Materialize event-ready Body CSV batches and an auditable manifest."""

    if not CAMPAIGN_PATTERN.fullmatch(campaign_id):
        raise ValueError("campaign_id must use lowercase letters, digits, _ or -, with 3-81 characters.")
    if batch_size < 1 or batch_size > 50:
        raise ValueError("batch_size must be between 1 and 50.")
    if bool(record_ids_path) == include_all_valid:
        raise ValueError("Specify exactly one of record_ids_path or include_all_valid.")

    valid_rows, seen_ids, rejected_reasons, source_paths = _body_rows(body_inputs)
    requested_ids = sorted(valid_rows) if include_all_valid else _record_ids(record_ids_path or "")
    selected_ids = [record_id for record_id in requested_ids if record_id in valid_rows]
    missing_ids = [record_id for record_id in requested_ids if record_id not in seen_ids]
    # A repeated historical row can have failed while another extraction for
    # the same record_id is valid. The valid body wins; report only records
    # for which no reusable body exists at all.
    unusable_ids = [
        record_id
        for record_id in requested_ids
        if record_id not in valid_rows and record_id in rejected_reasons
    ]

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    normalized_prefix = object_prefix.strip("/")
    batches: list[dict[str, Any]] = []
    for batch_index, offset in enumerate(range(0, len(selected_ids), batch_size), start=1):
        batch_ids = selected_ids[offset : offset + batch_size]
        filename = f"body_backfill_{campaign_id}_{batch_index:03d}.csv"
        path = destination / filename
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(valid_rows[record_id] for record_id in batch_ids)
        batches.append(
            {
                "batch_index": batch_index,
                "file": filename,
                "object_name": f"{normalized_prefix}/{filename}",
                "record_count": len(batch_ids),
                "record_ids": batch_ids,
                "sha256": _sha256(path),
            }
        )

    manifest = {
        "schema_version": "claims-backfill.v1",
        "campaign_id": campaign_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "all_valid_body_rows" if include_all_valid else "label_studio_record_ids",
        "source_body_files": [str(path) for path in source_paths],
        "requested_record_ids": len(requested_ids),
        "valid_body_records_available": len(valid_rows),
        "selected_record_ids": len(selected_ids),
        "missing_record_ids": missing_ids,
        "unusable_record_ids": [
            {"record_id": record_id, "reason": rejected_reasons[record_id]}
            for record_id in unusable_ids
        ],
        "batch_size": batch_size,
        "batches": batches,
    }
    (destination / "campaign.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body-input", nargs="+", required=True, help="Body CSV file(s) or directories")
    parser.add_argument("--output-dir", required=True, help="Local directory for generated CSV batches")
    parser.add_argument("--campaign-id", required=True, help="Stable identifier, e.g. historical-20261015")
    parser.add_argument("--batch-size", type=int, default=20, help="Rows per event-ready CSV (1-50; default: 20)")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--record-ids", help="Label Studio export / CSV / JSONL with the exact record_id scope")
    selection.add_argument("--all-valid", action="store_true", help="Explicitly select every valid Body row")
    parser.add_argument("--object-prefix", default="silver/body", help="Target Object Storage prefix for the manifest")
    args = parser.parse_args()

    try:
        manifest = prepare_backfill(
            body_inputs=args.body_input,
            output_dir=args.output_dir,
            campaign_id=args.campaign_id,
            batch_size=args.batch_size,
            record_ids_path=args.record_ids,
            include_all_valid=args.all_valid,
            object_prefix=args.object_prefix,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": "ok",
                "campaign_id": manifest["campaign_id"],
                "selected_record_ids": manifest["selected_record_ids"],
                "batches": len(manifest["batches"]),
                "missing_record_ids": len(manifest["missing_record_ids"]),
                "unusable_record_ids": len(manifest["unusable_record_ids"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
