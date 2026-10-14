import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "labelstudio" / "sync_evidence_from_object_storage.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("sync_evidence_from_object_storage", MODULE_PATH)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

AUTH_MODULE_PATH = Path(__file__).parents[1] / "labelstudio" / "api_auth.py"
AUTH_SPEC = importlib.util.spec_from_file_location("api_auth", AUTH_MODULE_PATH)
assert AUTH_SPEC and AUTH_SPEC.loader
auth = importlib.util.module_from_spec(AUTH_SPEC)
AUTH_SPEC.loader.exec_module(auth)


def test_sync_accepts_only_current_evidence_outputs() -> None:
    assert sync._evidence_object_name(
        "silver/evidence/evidence_run_20261009T130636Z_8a27e68e.csv", "silver/evidence"
    )
    assert sync._evidence_object_name(
        "silver/evidence/evidence_run_zz_current_claims_current_a_123456789abc.csv",
        "silver/evidence",
    )
    assert not sync._evidence_object_name(
        "silver/evidence/evidence_translated_run_20261009T020412Z_02085de4.csv", "silver/evidence"
    )
    assert not sync._evidence_object_name(
        "silver/rejected/evidence/rejected_evidence_run.csv", "silver/evidence"
    )


def test_sync_cache_path_is_stable_and_flat(tmp_path: Path) -> None:
    first = sync._local_csv_path(tmp_path, "silver/evidence/evidence_run_20261009T130636Z_a.csv")
    second = sync._local_csv_path(tmp_path, "silver/evidence/evidence_run_20261009T130636Z_a.csv")

    assert first == second
    assert first.parent == tmp_path
    assert first.name.endswith("evidence_run_20261009T130636Z_a.csv")


def test_sync_accepts_only_completed_v1_manifest_reference(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "evidence.manifest.v1",
                "status": "COMPLETE",
                "evidence_object": "silver/evidence/run_id=run_1/evidence.jsonl",
            }
        ),
        encoding="utf-8",
    )

    assert sync._evidence_manifest_object_name(
        "silver/evidence/run_id=run_1/manifest.json", "silver/evidence"
    )
    assert not sync._evidence_manifest_object_name(
        "silver/evidence/run_id=run_1/evidence.jsonl", "silver/evidence"
    )
    assert sync._read_v1_manifest(manifest, "silver/evidence") == (
        "COMPLETE",
        "silver/evidence/run_id=run_1/evidence.jsonl",
    )


def test_api_auth_uses_bearer_only_for_jwt_tokens() -> None:
    assert auth.authorization_header("legacy-token") == {"Authorization": "Token legacy-token"}
    assert auth.authorization_header("header.payload.signature") == {
        "Authorization": "Bearer header.payload.signature"
    }


def test_sync_exchanges_refresh_token_for_access_token(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return json.dumps({"access": "header.payload.signature"}).encode("utf-8")

    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda request, timeout: Response())

    assert sync._refresh_access_token("http://label-studio", "refresh.token.value") == "header.payload.signature"
