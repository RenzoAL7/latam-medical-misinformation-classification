"""Pruebas del código OCI con datos sintéticos: sin bucket ni Label Studio."""

from __future__ import annotations

import csv
import importlib.util
import io
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "oci_pipeline"


def load(name: str, path: Path) -> types.ModuleType:
    sys.modules.setdefault("oci", types.ModuleType("oci"))  # el importador importa oci al cargar
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


func = load("prepare_labelstudio_batch_func", ROOT / "prepare_labelstudio_batch" / "func.py")
importer = load("labelstudio_batch_importer", ROOT / "labelstudio_batch_importer" / "import_labelstudio_batches.py")

GOOD_BODY = "El ministerio informó que la vacuna reduce las hospitalizaciones. " * 10
BINARY_BODY = "\x1f\x8b\x00" + "�" * 500 + "abc"


def row(record_id: str, title: str, body: str, country: str = "pe") -> dict[str, str]:
    return {
        "record_id": record_id, "title": title, "body": body, "country": country,
        "extraction_status": "OK", "canonical_url": "", "source_name": "x",
        "published_at": "", "subtitle_or_bajada": "",
    }


def test_binary_detection() -> None:
    assert func.is_binary_text(BINARY_BODY)
    assert func.is_binary_text("texto normal\x00")
    assert not func.is_binary_text(GOOD_BODY)
    assert not func.is_binary_text("Un carácter � perdido. " + GOOD_BODY)
    assert not func.is_binary_text("")


def test_function_discards_binary_bodies() -> None:
    rows = [row("a", "Noticia uno", GOOD_BODY), row("b", "Noticia dos", BINARY_BODY)]
    selected, unused = func.select_rows(rows, target=50)
    assert [r["record_id"] for r in selected] == ["a"]
    assert [(r["record_id"], r[func.UNUSED_REASON_COLUMN]) for r in unused] == [("b", func.BINARY_REASON)]


def test_importer_tolerates_nul_bytes() -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=func.TASK_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerow(row("a", "Noticia uno", GOOD_BODY))
    writer.writerow(row("b", "Noticia dos", BINARY_BODY))
    writer.writerow(row("c", "Noticia tres", GOOD_BODY + "fin"))
    raw = buffer.getvalue()
    assert "\x00" in raw
    try:
        list(csv.DictReader(io.StringIO(raw, newline="")))
    except csv.Error:
        pass  # así fallaba el importador antes de la corrección
    rows, binary = importer.parse_tasks(raw)
    assert [r["record_id"] for r in rows] == ["a", "c"]
    assert binary == 1
