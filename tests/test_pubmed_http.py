from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "oci"
    / "functions"
    / "retrieve_pubmed_evidence"
    / "func.py"
)


def _load_module():
    fdk = sys.modules.get("fdk")
    if fdk is None:
        import types

        fdk = types.ModuleType("fdk")
        fdk.response = types.SimpleNamespace(Response=object)
        sys.modules["fdk"] = fdk
    spec = importlib.util.spec_from_file_location("retrieve_pubmed_evidence_http_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pubmed_search_uses_post_with_form_body(monkeypatch) -> None:
    module = _load_module()
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    response = module._http_request(
        "https://example.test/esearch.fcgi",
        {"term": "long query", "retmax": "5"},
        12.0,
        "application/json",
        method="POST",
    )

    request = captured["request"]
    assert response == b"{}"
    assert request.full_url == "https://example.test/esearch.fcgi"
    assert request.method == "POST"
    assert request.data == b"term=long+query&retmax=5"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert captured["timeout"] == 12.0


def test_search_pubmed_requests_post(monkeypatch) -> None:
    module = _load_module()
    calls: list[dict[str, object]] = []

    def fake_request(endpoint, params, timeout, accept, *, method="GET"):
        calls.append(
            {
                "endpoint": endpoint,
                "params": params,
                "timeout": timeout,
                "accept": accept,
                "method": method,
            }
        )
        return b'{"esearchresult": {"idlist": ["123", "456"]}}'

    monkeypatch.setattr(module, "_http_request", fake_request)

    result = module._search_pubmed("medical query", 5, "tool", "mail@example.test", "", 8.0)

    assert result == ["123", "456"]
    assert calls == [
        {
            "endpoint": f"{module.PUBMED_BASE_URL}/esearch.fcgi",
            "params": {
                "db": "pubmed",
                "term": "medical query",
                "retmode": "json",
                "retmax": "5",
                "sort": "relevance",
                "tool": "tool",
                "email": "mail@example.test",
            },
            "timeout": 8.0,
            "accept": "application/json",
            "method": "POST",
        }
    ]
