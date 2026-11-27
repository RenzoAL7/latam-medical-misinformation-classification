from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from claim_extraction import anotacion_csv as ac


BODY = (
    "Primera oración de ejemplo.  La sustancia X se asocia con el resultado Y en adultos.\n"
    "Segunda oración con un emoji 🙂. Otra oración final."
)
TITLE = "Título de ejemplo sobre la sustancia X"


@pytest.fixture(scope="module")
def interface() -> ac.Interface:
    return ac.Interface.from_xml()


def sheet_row(**values: str) -> dict[str, str]:
    row = {c: "" for c in ac.TEMPLATE_COLUMNS}
    row.update({"record_id": "r1", "title": TITLE, "body": BODY})
    row.update(values)
    return row


def apt_row(**values: str) -> dict[str, str]:
    base = {
        "news_eligibility": "APTA_CON_CLAIM",
        "tipo_claim": "CIFRA_O_HECHO",
        "tema": "Otra",
        "reference_claim": "La sustancia X se asocia con el resultado Y en adultos.",
        "oracion_central": "La sustancia X se asocia con el resultado Y en adultos.",
    }
    base.update(values)
    return sheet_row(**base)


def test_interface_choices_come_from_project_xml(interface: ac.Interface) -> None:
    assert interface.choices["news_eligibility"] == ["APTA_CON_CLAIM", "SIN_CLAIM_MEDICO", "NO_UTILIZABLE_POR_RUIDO"]
    assert "Cáncer" in interface.choices["tema"]
    assert interface.to_name["evidencia_titulo"] == "title"
    assert interface.to_name["reference_claim"] == "article_body"


def test_template_keeps_tasks_and_adds_empty_annotation_columns(tmp_path: Path) -> None:
    export = tmp_path / "export.csv"
    with export.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "annotation_id", "record_id", "title", "body", "topic"])
        writer.writeheader()
        writer.writerow({"id": "1", "record_id": "a", "title": "T", "body": "B", "topic": "viejo"})
    output = tmp_path / "hoja.csv"
    assert ac.build_template(export, output) == 1
    columns, rows = ac.read_csv(output)
    assert columns == ac.TEMPLATE_COLUMNS
    assert rows[0]["record_id"] == "a" and rows[0]["news_eligibility"] == ""
    assert "topic" not in columns


def test_template_rejects_repeated_record_ids(tmp_path: Path) -> None:
    export = tmp_path / "export.csv"
    export.write_text("record_id,title,body\na,T,B\na,T2,B2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="repetidos"):
        ac.build_template(export, tmp_path / "hoja.csv")


def test_locate_tolerates_collapsed_whitespace() -> None:
    start, end = ac.locate(BODY, "Primera oración de ejemplo. La sustancia")
    assert BODY[start:end] == "Primera oración de ejemplo.  La sustancia"
    assert ac.locate(BODY, "no existe") is None


def test_spans_use_javascript_offsets_after_emoji(interface: ac.Interface) -> None:
    row = ac.parse_row(apt_row(oraciones_contexto="Otra oración final."), interface)
    assert not row.errors
    context = next(s for s in row.spans if s["labels"] == ["CONTEXTO"])
    python_start = BODY.index("Otra oración final.")
    assert context["start"] == python_start + 1  # el emoji ocupa dos unidades UTF-16
    assert context["text"] == "Otra oración final."


def test_central_sentence_can_be_in_title(interface: ac.Interface) -> None:
    row = ac.parse_row(apt_row(oracion_central=TITLE), interface)
    assert not row.errors
    result = ac.labelstudio_result(row, interface)
    span = next(r for r in result if r["type"] == "labels")
    assert span["from_name"] == "evidencia_titulo" and span["to_name"] == "title"
    assert span["value"] == {"start": 0, "end": len(TITLE), "text": TITLE, "labels": ["CENTRAL"]}


def test_choices_accept_case_and_accents(interface: ac.Interface) -> None:
    row = ac.parse_row(apt_row(news_eligibility="apta_con_claim", tema="cancer"), interface)
    assert not row.errors
    assert row.values["tema"] == "Cáncer"


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"tema": ""}, "exige tema"),
        ({"tema": "Dermatología"}, "no es una opción"),
        ({"reference_claim": "Muy corta"}, "palabras"),
        ({"oracion_central": "Esta oración no está en la noticia."}, "no se encontró"),
        ({"motivo_sin_claim": "NO_ES_SALUD"}, "solo va con"),
    ],
)
def test_apt_rows_report_errors(interface: ac.Interface, values: dict[str, str], message: str) -> None:
    row = ac.parse_row(apt_row(**values), interface)
    assert any(message in error for error in row.errors), row.errors


def test_non_medical_requires_reason_and_rejects_claim(interface: ac.Interface) -> None:
    missing = ac.parse_row(sheet_row(news_eligibility="SIN_CLAIM_MEDICO"), interface)
    assert any("motivo_sin_claim" in e for e in missing.errors)
    extra = ac.parse_row(
        sheet_row(news_eligibility="SIN_CLAIM_MEDICO", motivo_sin_claim="NO_ES_SALUD", tema="Otra"), interface
    )
    assert any("no lleva tema" in e for e in extra.errors)
    ok = ac.parse_row(sheet_row(news_eligibility="SIN_CLAIM_MEDICO", motivo_sin_claim="no_es_salud"), interface)
    assert not ok.errors and ok.values["motivo_sin_claim"] == "NO_ES_SALUD"


def test_blank_row_is_not_annotated(interface: ac.Interface) -> None:
    row = ac.parse_row(sheet_row(anotador="Renzo"), interface)
    assert not row.errors and not row.annotated


def test_result_matches_labelstudio_annotation_shape(interface: ac.Interface) -> None:
    row = ac.parse_row(apt_row(revisar_en_pareja="DUDOSA"), interface)
    result = ac.labelstudio_result(row, interface)
    by_name = {r["from_name"]: r for r in result}
    assert by_name["news_eligibility"]["value"] == {"choices": ["APTA_CON_CLAIM"]}
    assert by_name["tema"]["to_name"] == "article_body"
    assert by_name["reference_claim"] == {
        **by_name["reference_claim"],
        "type": "textarea",
        "value": {"text": [row.values["reference_claim"]]},
    }
    assert by_name["evidencia_cuerpo"]["value"]["labels"] == ["CENTRAL"]
    assert by_name["revisar_en_pareja"]["value"] == {"choices": ["DUDOSA"]}
    assert all({"id", "origin"} <= set(r) for r in result)


def test_notebook_output_has_columns_required_by_notebook_01(interface: ac.Interface) -> None:
    rows = [ac.parse_row(apt_row(anotador="Renzo"), interface)]
    output = ac.notebook_rows(rows)[0]
    required = {"annotation_id", "record_id", "title", "body", "news_eligibility", "reference_claim", "tema"}
    assert required <= set(ac.NOTEBOOK_COLUMNS)
    assert output["annotation_id"] == "csv:r1" and output["annotator"] == "Renzo"
    assert json.loads(output["evidencia_cuerpo"])[0]["labels"] == ["CENTRAL"]


def test_convert_refuses_sheets_with_errors(tmp_path: Path, interface: ac.Interface) -> None:
    sheet = tmp_path / "hoja.csv"
    ac.write_csv(sheet, [apt_row(), {**apt_row(tema=""), "record_id": "r2"}], ac.TEMPLATE_COLUMNS)
    code = ac.main(["convertir", str(sheet), "--notebook", str(tmp_path / "n.csv")])
    assert code == 1 and not (tmp_path / "n.csv").exists()


def test_convert_writes_notebook_csv_and_labelstudio_json(tmp_path: Path) -> None:
    sheet = tmp_path / "hoja.csv"
    blank = {**sheet_row(), "record_id": "r2"}
    ac.write_csv(sheet, [apt_row(), blank], ac.TEMPLATE_COLUMNS)
    assert ac.main(["convertir", str(sheet), "--notebook", str(tmp_path / "n.csv"), "--json", str(tmp_path / "l.json")]) == 0
    _, rows = ac.read_csv(tmp_path / "n.csv")
    assert [r["record_id"] for r in rows] == ["r1"]
    tasks = json.loads((tmp_path / "l.json").read_text(encoding="utf-8"))
    assert tasks[0]["data"]["record_id"] == "r1"
    assert tasks[0]["annotations"][0]["result"]


class FakeClient:
    def __init__(self, tasks: dict[str, dict]) -> None:
        self._tasks = tasks
        self.posted: list[tuple[str, dict]] = []

    def tasks(self, project: int) -> dict[str, dict]:
        return self._tasks

    def call(self, path: str, payload: dict | None = None) -> dict:
        self.posted.append((path, payload))
        return {}


def test_upload_pairs_by_record_id_and_skips_annotated_or_changed(interface: ac.Interface) -> None:
    rows = [
        ac.parse_row(apt_row(), interface),
        ac.parse_row({**apt_row(), "record_id": "r2"}, interface),
        ac.parse_row({**apt_row(), "record_id": "r3"}, interface),
        ac.parse_row({**apt_row(), "record_id": "r4"}, interface),
    ]
    client = FakeClient({
        "r1": {"id": 10, "annotations": 0, "title": TITLE, "body": BODY},
        "r2": {"id": 11, "annotations": 1, "title": TITLE, "body": BODY},
        "r3": {"id": 12, "annotations": 0, "title": TITLE, "body": "otro cuerpo"},
    })
    dry = ac.upload(rows, interface, client, 9, send=False)
    assert dry == {"se_subiria": 1, "ya_anotada": 1, "texto_distinto": 1, "sin_tarea_en_proyecto": 1}
    assert client.posted == []
    sent = ac.upload(rows, interface, client, 9, send=True)
    assert sent["subida"] == 1
    assert client.posted[0][0] == "/api/tasks/10/annotations/"
    assert client.posted[0][1]["result"]
