from __future__ import annotations

from claim_extraction.collection import (
    canonicalize_url,
    extract_body_from_html,
    normalize_newsdata_articles,
    record_id_for_url,
)


def test_newsdata_rows_filter_and_deduplicate_urls() -> None:
    rows = normalize_newsdata_articles(
        [
            {
                "language": "es",
                "category": ["health"],
                "country": ["Peru"],
                "link": "https://example.org/noticia?utm_source=x&id=3",
                "title": "Título médico",
                "description": "Bajada",
                "source_name": "Fuente",
                "pubDate": "2026-11-01 00:00:00",
            },
            {
                "language": "es",
                "category": ["health"],
                "country": ["Peru"],
                "link": "https://example.org/noticia?id=3&fbclid=x",
            },
            {"language": "en", "category": ["health"], "link": "https://example.org/en"},
            {"language": "es", "category": ["sports"], "link": "https://example.org/sports"},
        ],
        retrieved_at="2026-11-01T00:00:00+00:00",
        requested_country="pe",
    )
    assert len(rows) == 1
    assert rows[0]["canonical_url"] == "https://example.org/noticia?id=3"
    assert rows[0]["country"] == "pe"
    assert rows[0]["record_id"] == record_id_for_url(rows[0]["canonical_url"])
    assert canonicalize_url("https://example.org/x?gclid=y") == "https://example.org/x"


def test_beautifulsoup_body_extractor_prefers_article_and_removes_navigation() -> None:
    body, method = extract_body_from_html(
        "<html><body><nav>Menú</nav><article>"
        "Contenido médico verificable con suficiente detalle.</article>"
        "<footer>Pie</footer></body></html>"
    )
    assert method == "beautifulsoup"
    assert body == "Contenido médico verificable con suficiente detalle."
