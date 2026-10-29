"""Normalización de texto y adaptadores de entrada por modelo."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .registry import ModelSpec


def clean_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    cleaned = clean_text(text)
    if not cleaned:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]


def build_source_text(title: object, body: object) -> str:
    parts = [clean_text(title), clean_text(body)]
    return "\n\n".join(part for part in parts if part)


def build_claimgen_transfer_input(title: object, body: object) -> str:
    """Adapta una noticia a contexto || citancia de forma explícitamente no literal."""
    clean_title = clean_text(title)
    sentences = split_sentences(clean_text(body))
    if not sentences:
        return clean_title

    citance = sentences[0]
    context_parts = [clean_title, *sentences[1:3]]
    context = clean_text(" ".join(part for part in context_parts if part))
    return f"{context} || {citance}" if context else citance


def input_for_model(record: Mapping[str, object], spec: ModelSpec) -> str:
    source_text = clean_text(record.get("source_text"))
    title = record.get("title", "")
    body = record.get("body", "")

    if spec.input_adapter == "claimgen_transfer":
        claimgen_input = clean_text(record.get("claimgen_input"))
        if claimgen_input:
            return claimgen_input
        if clean_text(body):
            return build_claimgen_transfer_input(title, body)
        return source_text

    if source_text:
        return source_text
    return build_source_text(title, body)
