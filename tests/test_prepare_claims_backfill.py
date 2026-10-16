from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "05_prepare_claims_backfill.py"
SPEC = importlib.util.spec_from_file_location("prepare_claims_backfill", SCRIPT)
assert SPEC and SPEC.loader
BACKFILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKFILL)


def _write_body_csv(path: Path) -> None:
    rows = [
        {
            "record_id": "news-1",
            "title": "Uno",
            "body": "cuerpo corto",
            "extraction_status": "OK",
            "extracted_at": "2026-10-01T00:00:00Z",
        },
        {
            "record_id": "news-1",
            "title": "Uno",
            "body": "cuerpo más largo y válido",
            "extraction_status": "OK",
            "extracted_at": "2026-10-02T00:00:00Z",
        },
        {
            "record_id": "news-1",
            "title": "Uno",
            "body": "",
            "extraction_status": "HTTP_ERROR",
            "extracted_at": "2026-10-03T00:00:00Z",
        },
        {
            "record_id": "news-2",
            "title": "Dos",
            "body": "cuerpo dos",
            "extraction_status": "OK",
            "extracted_at": "2026-10-01T00:00:00Z",
        },
        {
            "record_id": "news-3",
            "title": "Tres",
            "body": "",
            "extraction_status": "HTTP_ERROR",
            "extracted_at": "2026-10-01T00:00:00Z",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BACKFILL.OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_prepare_backfill_uses_exact_labelstudio_scope_and_preserves_best_body(tmp_path: Path) -> None:
    body_csv = tmp_path / "body.csv"
    _write_body_csv(body_csv)
    export = tmp_path / "labelstudio_export.json"
    export.write_text(
        json.dumps(
            [
                {"data": {"record_id": "news-2"}},
                {"data": {"record_id": "news-1"}},
                {"data": {"record_id": "news-3"}},
                {"data": {"record_id": "missing"}},
            ]
        ),
        encoding="utf-8",
    )

    output = tmp_path / "campaign"
    manifest = BACKFILL.prepare_backfill(
        body_inputs=[body_csv],
        output_dir=output,
        campaign_id="historic-20261015",
        batch_size=1,
        record_ids_path=export,
    )

    assert manifest["selected_record_ids"] == 2
    assert manifest["missing_record_ids"] == ["missing"]
    assert manifest["unusable_record_ids"] == [
        {"record_id": "news-3", "reason": "missing_body"}
    ]
    assert [batch["object_name"] for batch in manifest["batches"]] == [
        "silver/body/body_backfill_historic-20261015_001.csv",
        "silver/body/body_backfill_historic-20261015_002.csv",
    ]

    with (output / "body_backfill_historic-20261015_002.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["record_id"] == "news-1"
    assert row["body"] == "cuerpo más largo y válido"
    assert (output / "campaign.json").is_file()
