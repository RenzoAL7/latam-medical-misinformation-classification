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
