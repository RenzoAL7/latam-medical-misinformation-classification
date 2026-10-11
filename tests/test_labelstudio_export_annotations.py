from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "labelstudio" / "export_annotations.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("export_annotations", MODULE_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _write(path: Path, body: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["record_id", "body", "topic"])
        writer.writeheader()
        writer.writerow({"record_id": "r1", "body": body, "topic": "health"})


def test_evidence_join_prefers_row_with_body(tmp_path: Path) -> None:
    old_path = tmp_path / "evidence_old.csv"
    new_path = tmp_path / "evidence_new.csv"
    _write(old_path, "")
    _write(new_path, "Article body")

    rows = module._evidence_rows([str(tmp_path)])

    assert rows["r1"]["body"] == "Article body"


def test_offline_json_export_builds_gold_row_from_latest_annotation(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.csv"
    _write(evidence_path, "Article body")
    payload = [
        {
            "data": {
                "record_id": "r1",
                "country": "pe",
                "title": "Article title",
            },
            "annotations": [
                {
                    "id": 11,
                    "completed": True,
                    "updated_at": "2026-10-11T08:00:00Z",
                    "completed_by": "reviewer-old",
                    "result": [
                        {"from_name": "evidence_relation", "value": {"choices": ["unclear"]}},
                        {"from_name": "final_label", "value": {"choices": ["NO_DETERMINABLE"]}},
                    ],
                },
                {
                    "id": 12,
                    "completed": True,
                    "updated_at": "2026-10-11T09:00:00Z",
                    "completed_by": "reviewer-new",
                    "result": [
                        {"from_name": "evidence_relation", "value": {"choices": ["supports"]}},
                        {"from_name": "final_label", "value": {"choices": ["RESPALDADA"]}},
                        {
                            "from_name": "verification_note",
                            "value": {"text": ["Reviewed", "with evidence"]},
                        },
                    ],
                },
            ],
        }
    ]

    rows = module._gold_rows(payload, module._evidence_rows([str(evidence_path)]))

    assert rows == [
        {
            "record_id": "r1",
            "country": "pe",
            "source_name": "",
            "topic": "health",
            "canonical_url": "",
            "published_at": "",
            "title": "Article title",
            "body": "Article body",
            "claim_text": "",
            "best_pmid": "",
            "best_cosine_similarity": "",
            "best_embedding_similarity": "",
            "best_tfidf_similarity": "",
            "ranking_method": "",
            "embedding_model": "",
            "evidence_status": "",
            "evidence_run_id": "",
            "evidence_relation": "supports",
            "label": "RESPALDADA",
            "verification_note": "Reviewed\nwith evidence",
            "annotation_id": 12,
            "annotator": "reviewer-new",
            "annotated_at": "2026-10-11T09:00:00Z",
        }
    ]


def test_read_json_export_accepts_utf8_bom(tmp_path: Path) -> None:
    export_path = tmp_path / "labelstudio-export.json"
    export_path.write_text("[{\"data\": {\"record_id\": \"r1\"}}]", encoding="utf-8-sig")

    payload = module._read_json_export(export_path)

    assert payload[0]["data"]["record_id"] == "r1"
