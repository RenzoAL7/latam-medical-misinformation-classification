"""Anotación del proyecto 9 en una hoja de cálculo, sin abrir Label Studio.

Flujo:

1. ``plantilla``: convierte un export CSV de Label Studio en una hoja con las
   columnas de anotación vacías al lado del título.
2. Se llena la hoja (Google Sheets o Excel, guardando como CSV UTF-8).
3. ``validar``: revisa valores, campos condicionales y que las oraciones copiadas
   existan en el título o el cuerpo.
4. ``convertir``: escribe un CSV con las mismas columnas que el export de Label
   Studio (lo lee el notebook 01) y un JSON importable en Label Studio.
5. ``subir``: agrega las anotaciones a las tareas que ya existen en el proyecto,
   emparejando por ``record_id``. Sin ``--enviar`` solo muestra qué haría.

Los valores permitidos salen de ``configs/labelstudio_proyecto9.xml``, así que la
hoja no puede desalinearse de la interfaz. Uso::

    python -m claim_extraction.anotacion_csv plantilla EXPORT.csv HOJA.csv
    python -m claim_extraction.anotacion_csv validar HOJA.csv
    python -m claim_extraction.anotacion_csv convertir HOJA.csv --notebook N.csv --json L.json
    python -m claim_extraction.anotacion_csv subir HOJA.csv --proyecto 9 [--enviar]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import sys
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

csv.field_size_limit(sys.maxsize)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPOSITORY_ROOT / "configs" / "labelstudio_proyecto9.xml"

DATA_COLUMNS = [
    "record_id", "title", "body", "subtitle_or_bajada", "country",
    "source_name", "published_at", "canonical_url",
]
ANNOTATION_COLUMNS = [
    "news_eligibility", "motivo_sin_claim", "tipo_claim", "tema",
    "reference_claim", "oracion_central", "oraciones_contexto",
    "revisar_en_pareja", "anotador", "notas",
]
# Lo que se llena va junto al título; el cuerpo, largo, queda a la derecha.
TEMPLATE_COLUMNS = ["record_id", "title", *ANNOTATION_COLUMNS, *DATA_COLUMNS[2:]]
CONTEXT_SEPARATOR = "||"
APT = "APTA_CON_CLAIM"
NOT_MEDICAL = "SIN_CLAIM_MEDICO"
CLAIM_WORDS = (5, 80)
# En la hoja se escribe la oración; en Label Studio pasa a ser un span del campo.
SPAN_FIELDS = {"title": ("evidencia_titulo", "title"), "body": ("evidencia_cuerpo", "article_body")}


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def fold(value: str) -> str:
    """Minúsculas y sin tildes, para aceptar «cancer» como «Cáncer»."""
    decomposed = unicodedata.normalize("NFKD", clean(value))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


@dataclass(frozen=True)
class Interface:
    """Campos de elección de la interfaz y el campo de texto al que apuntan."""

    choices: dict[str, list[str]]
    to_name: dict[str, str]

    @classmethod
    def from_xml(cls, path: str | Path = DEFAULT_CONFIG) -> "Interface":
        root = ET.parse(path).getroot()
        choices: dict[str, list[str]] = {}
        to_name: dict[str, str] = {}
        for tag in root.iter():
            if tag.tag in {"Choices", "Labels", "TextArea"} and tag.get("name"):
                to_name[tag.get("name")] = tag.get("toName", "")
            if tag.tag == "Choices" and tag.get("name"):
                choices[tag.get("name")] = [c.get("value") for c in tag.iter("Choice")]
        for name in ("news_eligibility", "motivo_sin_claim", "tipo_claim", "tema", "revisar_en_pareja"):
            if name not in choices:
                raise ValueError(f"La configuración no define el campo {name}: {path}")
        return cls(choices, to_name)

    def canonical(self, field_name: str, value: str) -> str | None:
        wanted = fold(value)
        return next((c for c in self.choices[field_name] if fold(c) == wanted), None)


def read_csv(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: str | Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_template(export_path: str | Path, output_path: str | Path) -> int:
    """Escribe la hoja vacía. Falla si el export tiene record_id vacío o repetido."""
    columns, rows = read_csv(export_path)
    missing = sorted({"record_id", "title", "body"} - set(columns))
    if missing:
        raise ValueError(f"Al export le faltan columnas: {', '.join(missing)}")
    counts = Counter(clean(r.get("record_id")) for r in rows)
    problems = [rid or "<vacío>" for rid, n in counts.items() if n > 1 or not rid]
    if problems:
        raise ValueError(f"record_id vacíos o repetidos: {problems[:10]}")
    sheet = [{c: r.get(c, "") for c in DATA_COLUMNS} for r in rows]
    write_csv(output_path, sheet, TEMPLATE_COLUMNS)
    return len(sheet)


def _whitespace_map(text: str) -> tuple[str, list[int]]:
    """Texto con espacios colapsados y, por cada carácter, su posición original."""
    chars: list[str] = []
    positions: list[int] = []
    previous_space = True
    for index, char in enumerate(text):
        if char.isspace():
            if not previous_space:
                chars.append(" ")
                positions.append(index)
            previous_space = True
        else:
            chars.append(char)
            positions.append(index)
            previous_space = False
    return "".join(chars), positions


def locate(text: str, sentence: str) -> tuple[int, int] | None:
    """Posición de ``sentence`` en ``text`` (índices de Python), tolerando espacios."""
    sentence = sentence.strip()
    if not sentence:
        return None
    start = text.find(sentence)
    if start >= 0:
        return start, start + len(sentence)
    collapsed, positions = _whitespace_map(text)
    target = clean(sentence)
    start = collapsed.find(target)
    if start < 0:
        return None
    return positions[start], positions[start + len(target) - 1] + 1


def js_offset(text: str, index: int) -> int:
    """Label Studio cuenta en unidades UTF-16 (como JavaScript): un emoji vale 2."""
    return len(text[:index].encode("utf-16-le")) // 2


@dataclass
class Row:
    record_id: str
    data: dict[str, str]
    values: dict[str, Any] = field(default_factory=dict)
    spans: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def annotated(self) -> bool:
        return bool(self.values.get("news_eligibility"))


def _span(row: dict[str, str], sentence: str, label: str) -> dict[str, Any] | None:
    for column in ("title", "body"):
        text = row.get(column, "") or ""
        found = locate(text, sentence)
        if found:
            start, end = found
            return {
                "campo": column,
                "start": js_offset(text, start),
                "end": js_offset(text, end),
                "text": text[start:end],
                "labels": [label],
            }
    return None


def parse_row(row: dict[str, str], interface: Interface) -> Row:
    """Normaliza una fila de la hoja y anota sus errores; no lanza excepciones."""
    parsed = Row(clean(row.get("record_id")), {c: row.get(c, "") or "" for c in DATA_COLUMNS})
    raw = {c: clean(row.get(c)) for c in ANNOTATION_COLUMNS}
    if not any(raw[c] for c in ANNOTATION_COLUMNS if c not in {"anotador", "notas"}):
        return parsed

    def choice(name: str) -> str:
        if not raw[name]:
            return ""
        value = interface.canonical(name, raw[name])
        if value is None:
            parsed.errors.append(f"{name}: «{raw[name]}» no es una opción ({', '.join(interface.choices[name])})")
            return ""
        return value

    eligibility = choice("news_eligibility")
    if not raw["news_eligibility"]:
        parsed.errors.append("news_eligibility vacío, pero la fila tiene otras columnas llenas")
    values: dict[str, Any] = {"news_eligibility": eligibility}
    values["revisar_en_pareja"] = choice("revisar_en_pareja")
    values["anotador"] = raw["anotador"]
    values["notas"] = raw["notas"]

    if eligibility == NOT_MEDICAL:
        values["motivo_sin_claim"] = choice("motivo_sin_claim")
        if not raw["motivo_sin_claim"]:
            parsed.errors.append("SIN_CLAIM_MEDICO exige motivo_sin_claim")
    if eligibility == APT:
        for name in ("tipo_claim", "tema"):
            values[name] = choice(name)
            if not raw[name]:
                parsed.errors.append(f"APTA_CON_CLAIM exige {name}")
        claim = raw["reference_claim"]
        words = len(claim.split())
        if not claim:
            parsed.errors.append("APTA_CON_CLAIM exige reference_claim")
        elif not CLAIM_WORDS[0] <= words <= CLAIM_WORDS[1]:
            parsed.errors.append(f"reference_claim tiene {words} palabras (debe tener entre {CLAIM_WORDS[0]} y {CLAIM_WORDS[1]})")
        values["reference_claim"] = claim
        if not raw["oracion_central"]:
            parsed.errors.append("APTA_CON_CLAIM exige oracion_central")
        sentences = [("CENTRAL", row.get("oracion_central", ""))] if raw["oracion_central"] else []
        sentences += [
            ("CONTEXTO", s) for s in (row.get("oraciones_contexto") or "").split(CONTEXT_SEPARATOR) if s.strip()
        ]
        for label, sentence in sentences:
            span = _span(row, sentence, label)
            if span is None:
                parsed.errors.append(f"{label}: no se encontró en el título ni en el cuerpo: «{clean(sentence)[:80]}»")
            else:
                parsed.spans.append(span)
    else:
        extra = [c for c in ("tipo_claim", "tema", "reference_claim", "oracion_central", "oraciones_contexto") if raw[c]]
        if extra and eligibility:
            parsed.errors.append(f"{eligibility} no lleva {', '.join(extra)}; bórralas o cambia news_eligibility")
    if eligibility != NOT_MEDICAL and raw["motivo_sin_claim"]:
        parsed.errors.append("motivo_sin_claim solo va con SIN_CLAIM_MEDICO")
    parsed.values = values
    return parsed


def parse_sheet(path: str | Path, interface: Interface | None = None) -> list[Row]:
    interface = interface or Interface.from_xml()
    columns, rows = read_csv(path)
    missing = sorted({"record_id", "title", "body", *ANNOTATION_COLUMNS} - set(columns))
    if missing:
        raise ValueError(f"A la hoja le faltan columnas: {', '.join(missing)}")
    parsed = [parse_row(r, interface) for r in rows]
    counts = Counter(r.record_id for r in parsed)
    for item in parsed:
        if not item.record_id:
            item.errors.append("record_id vacío")
        elif counts[item.record_id] > 1:
            item.errors.append("record_id repetido en la hoja")
    return parsed


def labelstudio_result(row: Row, interface: Interface) -> list[dict[str, Any]]:
    """Lista ``result`` de una anotación, con el mismo formato que guarda la interfaz."""
    def item(name: str, kind: str, value: dict[str, Any], to_name: str | None = None) -> dict[str, Any]:
        return {
            "id": secrets.token_urlsafe(8)[:10], "from_name": name,
            "to_name": to_name or interface.to_name.get(name, "article_body"),
            "type": kind, "value": value, "origin": "manual",
        }

    result = []
    for name in ("news_eligibility", "motivo_sin_claim", "tipo_claim", "tema", "revisar_en_pareja"):
        if row.values.get(name):
            result.append(item(name, "choices", {"choices": [row.values[name]]}))
    for span in row.spans:
        from_name, to_name = SPAN_FIELDS[span["campo"]]
        value = {k: span[k] for k in ("start", "end", "text", "labels")}
        result.append(item(from_name, "labels", value, to_name))
    if row.values.get("reference_claim"):
        result.append(item("reference_claim", "textarea", {"text": [row.values["reference_claim"]]}))
    return result


def _check(rows: list[Row]) -> list[Row]:
    bad = [r for r in rows if r.errors]
    if bad:
        raise ValueError(f"{len(bad)} filas con errores; corre «validar» y corrígelas antes de convertir.")
    return [r for r in rows if r.annotated]


def notebook_rows(rows: list[Row]) -> list[dict[str, Any]]:
    """Filas con las columnas del export CSV de Label Studio (las lee el notebook 01)."""
    output = []
    for row in rows:
        out: dict[str, Any] = dict(row.data)
        out["annotation_id"] = f"csv:{row.record_id}" if row.annotated else ""
        out["annotator"] = row.values.get("anotador", "")
        for name in ("news_eligibility", "motivo_sin_claim", "tipo_claim", "tema", "reference_claim", "revisar_en_pareja"):
            out[name] = row.values.get(name, "")
        for column, (from_name, _) in SPAN_FIELDS.items():
            spans = [{k: s[k] for k in ("start", "end", "text", "labels")} for s in row.spans if s["campo"] == column]
            out[from_name] = json.dumps(spans, ensure_ascii=False) if spans else ""
        output.append(out)
    return output


NOTEBOOK_COLUMNS = [
    "annotation_id", "annotator", *DATA_COLUMNS, "news_eligibility", "motivo_sin_claim",
    "tipo_claim", "tema", "reference_claim", "evidencia_titulo", "evidencia_cuerpo", "revisar_en_pareja",
]


def labelstudio_tasks(rows: list[Row], interface: Interface) -> list[dict[str, Any]]:
    """Tareas con su anotación, en el formato JSON de importación de Label Studio."""
    return [
        {"data": dict(row.data), "annotations": [{"result": labelstudio_result(row, interface)}]}
        for row in rows
    ]


class LabelStudio:
    """Cliente mínimo. Acepta el token clásico o un Personal Access Token (JWT)."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        if token.startswith("eyJ"):
            access = self._call("/api/token/refresh/", None, {"refresh": token})["access"]
            self.auth = f"Bearer {access}"
        else:
            self.auth = f"Token {token}"

    def _call(self, path: str, auth: str | None, payload: Any = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read() or b"null")

    def call(self, path: str, payload: Any = None) -> Any:
        return self._call(path, self.auth, payload)

    def tasks(self, project: int) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        page = 1
        while True:
            try:
                batch = self.call(f"/api/projects/{project}/tasks/?page={page}&page_size=500")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # la API responde 404 al pasar de la última página
                    return found
                raise
            items = batch["tasks"] if isinstance(batch, dict) else batch
            for task in items:
                found[str(task["data"].get("record_id", ""))] = {
                    "id": task["id"], "annotations": task.get("total_annotations", 0),
                    "title": task["data"].get("title", ""), "body": task["data"].get("body", ""),
                }
            if len(items) < 500:
                return found
            page += 1


def upload(rows: list[Row], interface: Interface, client: LabelStudio, project: int, send: bool) -> Counter:
    """Agrega una anotación por fila a la tarea con el mismo record_id."""
    existing = client.tasks(project)
    summary: Counter = Counter()
    for row in rows:
        task = existing.get(row.record_id)
        if task is None:
            summary["sin_tarea_en_proyecto"] += 1
            print(f"  sin tarea: {row.record_id}")
            continue
        if task["annotations"]:
            summary["ya_anotada"] += 1
            continue
        if clean(task["title"]) != clean(row.data.get("title")) or clean(task["body"]) != clean(row.data.get("body")):
            # Los spans se calcularon sobre el texto de la hoja; si cambió, quedarían corridos.
            summary["texto_distinto"] += 1
            print(f"  texto distinto al del proyecto, no se sube: {row.record_id}")
            continue
        if send:
            client.call(f"/api/tasks/{task['id']}/annotations/", {"result": labelstudio_result(row, interface)})
        summary["subida" if send else "se_subiria"] += 1
    return summary


def _report(rows: list[Row]) -> int:
    annotated = [r for r in rows if r.annotated]
    bad = [r for r in rows if r.errors]
    print(f"Filas: {len(rows)} | anotadas: {len(annotated)} | con errores: {len(bad)}")
    print("Elegibilidad:", dict(Counter(r.values.get("news_eligibility") for r in annotated)))
    apt = [r for r in annotated if r.values.get("news_eligibility") == APT]
    if apt:
        print("Tema (aptas):", dict(Counter(r.values.get("tema") for r in apt)))
    for row in bad[:50]:
        for error in row.errors:
            print(f"  {row.record_id}: {error}")
    if len(bad) > 50:
        print(f"  … y {len(bad) - 50} filas más con errores")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m claim_extraction.anotacion_csv", description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="XML de la interfaz de Label Studio")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plantilla", help="hoja vacía desde un export CSV de Label Studio")
    p.add_argument("export")
    p.add_argument("salida")
    p = sub.add_parser("validar", help="revisa la hoja sin escribir nada")
    p.add_argument("hoja")
    p = sub.add_parser("convertir", help="CSV para el notebook 01 y JSON para Label Studio")
    p.add_argument("hoja")
    p.add_argument("--notebook", help="CSV con columnas de export (ponlo en artifacts/01_labelstudio/exports/)")
    p.add_argument("--json", help="JSON de tareas con anotación para importar en un proyecto nuevo")
    p = sub.add_parser("subir", help="agrega las anotaciones a las tareas existentes del proyecto")
    p.add_argument("hoja")
    p.add_argument("--proyecto", type=int, default=9)
    p.add_argument("--url", default=os.getenv("LABEL_STUDIO_URL", "http://localhost:18080"))
    p.add_argument("--enviar", action="store_true", help="sin esta opción solo muestra qué haría")
    args = parser.parse_args(argv)

    interface = Interface.from_xml(args.config)
    if args.command == "plantilla":
        print(f"Hoja con {build_template(args.export, args.salida)} noticias: {args.salida}")
        return 0
    rows = parse_sheet(args.hoja, interface)
    if args.command == "validar":
        return _report(rows)
    if _report(rows):
        return 1
    annotated = _check(rows)
    if args.command == "convertir":
        if not (args.notebook or args.json):
            parser.error("indica --notebook, --json o ambos")
        if args.notebook:
            write_csv(args.notebook, notebook_rows(annotated), NOTEBOOK_COLUMNS)
            print(f"CSV para el notebook 01 ({len(annotated)} filas): {args.notebook}")
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(
                json.dumps(labelstudio_tasks(annotated, interface), ensure_ascii=False, indent=1), encoding="utf-8"
            )
            print(f"JSON para Label Studio ({len(annotated)} tareas): {args.json}")
        return 0
    token = os.getenv("LABEL_STUDIO_TOKEN", "").strip()
    if not token:
        print("Define LABEL_STUDIO_TOKEN con tu token de Label Studio (Account & Settings).", file=sys.stderr)
        return 2
    summary = upload(annotated, interface, LabelStudio(args.url, token), args.proyecto, args.enviar)
    print(dict(summary))
    if not args.enviar:
        print("Nada se envió. Repite con --enviar para subir las anotaciones.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
