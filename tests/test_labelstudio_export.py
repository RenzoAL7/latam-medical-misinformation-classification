from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
LABELSTUDIO_DIR = ROOT / "labelstudio"
sys.path.insert(0, str(LABELSTUDIO_DIR))
spec = importlib.util.spec_from_file_location(
    "labelstudio_export",
    LABELSTUDIO_DIR / "export_annotations_to_object_storage.py",
)
assert spec and spec.loader
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def test_token_candidates_fall_back_to_existing_vm_refresh_token(monkeypatch) -> None:
    monkeypatch.setenv("LABEL_STUDIO_SECRET_OCID", "secret-ocid")
    monkeypatch.setenv("LABEL_STUDIO_REFRESH_TOKEN", "refresh.token.value")
    monkeypatch.delenv("LABEL_STUDIO_TOKEN", raising=False)
    monkeypatch.setattr(exporter, "_secret_value", lambda _secret_id: "legacy-vault-token")
    monkeypatch.setattr(exporter, "_refresh_access_token", lambda _url, token: f"access-for-{token}")

    candidates = exporter._token_candidates("http://labelstudio")

    assert candidates == [
        ("vault", "legacy-vault-token"),
        ("LABEL_STUDIO_REFRESH_TOKEN", "access-for-refresh.token.value"),
        ("LABEL_STUDIO_REFRESH_TOKEN", "refresh.token.value"),
    ]


def test_token_candidates_do_not_require_vault_when_secret_read_fails(monkeypatch) -> None:
    monkeypatch.setenv("LABEL_STUDIO_SECRET_OCID", "secret-ocid")
    monkeypatch.setenv("LABEL_STUDIO_REFRESH_TOKEN", "plain-refresh-token")
    monkeypatch.delenv("LABEL_STUDIO_TOKEN", raising=False)

    def fail(_secret_id: str) -> str:
        raise RuntimeError("secret unavailable")

    monkeypatch.setattr(exporter, "_secret_value", fail)

    assert exporter._token_candidates("http://labelstudio") == [
        ("LABEL_STUDIO_REFRESH_TOKEN", "plain-refresh-token"),
    ]
