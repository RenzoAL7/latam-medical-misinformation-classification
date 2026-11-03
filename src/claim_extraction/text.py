"""Normalización y construcción del texto de entrada."""

from __future__ import annotations

import re
from collections.abc import Mapping


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def build_source_text(title: object, body: object) -> str:
    parts = [clean_text(title), clean_text(body)]
    return "\n\n".join(part for part in parts if part)


def input_for_model(record: Mapping[str, object]) -> str:
    source_text = clean_text(record.get("source_text"))
    if source_text:
        return source_text
    return build_source_text(record.get("title", ""), record.get("body", ""))
