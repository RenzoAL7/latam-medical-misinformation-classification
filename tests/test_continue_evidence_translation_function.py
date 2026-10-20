from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import types


ROOT = Path(__file__).parents[1]
FUNCTION_DIR = ROOT / "oci" / "functions" / "continue_evidence_translation"
MODULE_PATH = FUNCTION_DIR / "func.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "continue_evidence_translation_function",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_translation_entrypoint_delegates_with_translation_continuation_mode(monkeypatch) -> None:
    module = _load_module()
    calls: list[str | None] = []

    def fake_handler(_ctx, _data):
        calls.append(os.environ.get("PIPELINE_MODE"))
        return "translation-result"

    monkeypatch.setattr(module._SHARED_FUNCTION, "handler", fake_handler)
    monkeypatch.setenv("PIPELINE_MODE", "retrieve")

    assert module.handler(None, b"{}") == "translation-result"
    assert calls == ["translation_continuation"]
    assert os.environ["PIPELINE_MODE"] == "retrieve"


def test_translation_entrypoint_restores_missing_mode(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.delenv("PIPELINE_MODE", raising=False)

    def fake_handler(_ctx, _data):
        assert os.environ["PIPELINE_MODE"] == "translation_continuation"
        return "ok"

    monkeypatch.setattr(module._SHARED_FUNCTION, "handler", fake_handler)

    assert module.handler(None, None) == "ok"
    assert "PIPELINE_MODE" not in os.environ
