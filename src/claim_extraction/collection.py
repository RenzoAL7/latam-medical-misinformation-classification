"""Utilidades deterministas para recolectar noticias y extraer sus cuerpos."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup


TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
DEFAULT_BODY_SELECTORS = (
    "[itemprop='articleBody']",
    "article",
    ".article-body",
    ".article__body",
    ".article-content",
    ".story-body",
    ".entry-content",
    ".post-content",
    "main",
)

COUNTRY_ALIASES = {
    "ar": {"ar", "argentina"},
    "bo": {"bo", "bolivia", "bolivia plurinational state of"},
    "cl": {"cl", "chile"},
    "co": {"co", "colombia"},
    "cr": {"cr", "costa rica"},
    "cu": {"cu", "cuba"},
    "do": {"do", "dominican republic", "republica dominicana"},
    "ec": {"ec", "ecuador"},
    "gt": {"gt", "guatemala"},
    "hn": {"hn", "honduras"},
    "mx": {"mx", "mexico"},
    "ni": {"ni", "nicaragua"},
    "pa": {"pa", "panama"},
    "pe": {"pe", "peru"},
    "pr": {"pr", "puerto rico"},
    "py": {"py", "paraguay"},
    "sv": {"sv", "el salvador"},
    "uy": {"uy", "uruguay"},
    "ve": {"ve", "venezuela"},
}


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonicalize_url(raw_url: str) -> str:
    """Elimina tracking para asignar una identidad estable a cada noticia."""
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("La URL debe usar http o https y tener un dominio.")
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
    ]
    hostname = parsed.hostname.lower()
    netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", urlencode(query), ""))


def record_id_for_url(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]


def _normalize_country(value: object) -> str:
    text = clean_text(",".join(str(item) for item in value) if isinstance(value, list) else value)
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    parts = {part.strip() for part in re.split(r"[,;/|]", normalized) if part.strip()}
    for code, aliases in COUNTRY_ALIASES.items():
        if parts.intersection(aliases):
            return code
    return ""


def _list_text(value: object) -> str:
    if isinstance(value, list):
        return ",".join(clean_text(item) for item in value if clean_text(item))
    return clean_text(value)


def normalize_newsdata_articles(
    articles: Iterable[object], *, retrieved_at: str, requested_country: str
) -> list[dict[str, str]]:
    """Convierte respuestas NewsData en filas de inventario deduplicadas por URL."""
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        language = clean_text(article.get("language")).casefold()
        if language not in {"es", "spanish"}:
            continue
        categories = {
            item.strip() for item in _list_text(article.get("category")).casefold().split(",")
        }
        if "health" not in categories:
            continue
        raw_url = clean_text(article.get("link"))
        if not raw_url:
            continue
        try:
            canonical_url = canonicalize_url(raw_url)
        except ValueError:
            continue
        record_id = record_id_for_url(canonical_url)
        if record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        country = _normalize_country(article.get("country")) or requested_country
        rows.append(
            {
                "record_id": record_id,
                "source_name": clean_text(article.get("source_name")),
                "canonical_url": canonical_url,
                "published_at": clean_text(article.get("pubDate") or article.get("pub_date")),
                "title": clean_text(article.get("title")),
                "subtitle_or_bajada": clean_text(article.get("description")),
                "topic": "health",
                "selection_status": "PENDIENTE",
                "retrieved_at": retrieved_at,
                "country": country,
            }
        )
    return rows


def extract_body_from_html(html: str) -> tuple[str, str]:
    """Extrae el bloque textual más extenso con BeautifulSoup, sin Trafilatura."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(
        [
            "script", "style", "noscript", "template", "svg", "canvas", "nav",
            "header", "footer", "aside", "form",
        ]
    ):
        tag.decompose()

    candidates: list[str] = []
    for selector in DEFAULT_BODY_SELECTORS:
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                candidates.append(text)
    if not candidates:
        fallback = clean_text((soup.body or soup).get_text(" ", strip=True))
        if fallback:
            candidates.append(fallback)
    if not candidates:
        return "", "none"
    return max(candidates, key=len), "beautifulsoup"
