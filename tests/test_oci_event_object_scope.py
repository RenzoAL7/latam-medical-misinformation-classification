import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).parents[1]
FUNCTIONS = [
    ("extract_news_body", "bronze/newsdata_run.csv"),
    ("extract_claims", "silver/body/body_run.csv"),
    ("retrieve_pubmed_evidence", "silver/claims/claims_run.jsonl"),
]


def _load_module(function_name: str) -> types.ModuleType:
    module_path = REPOSITORY_ROOT / "oci" / "functions" / function_name / "func.py"
    fdk = types.ModuleType("fdk")
    fdk.response = types.SimpleNamespace(Response=object)
    previous_fdk = sys.modules.get("fdk")
    sys.modules["fdk"] = fdk
    try:
        spec = importlib.util.spec_from_file_location(f"{function_name}_event_scope", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_fdk is None:
            sys.modules.pop("fdk", None)
        else:
            sys.modules["fdk"] = previous_fdk


def _event_object(module: types.ModuleType, function_name: str, payload: dict, bucket: str, prefix: str) -> str:
    data = io.BytesIO(json.dumps(payload).encode("utf-8"))
    if function_name == "retrieve_pubmed_evidence":
        return module._event_jsonl_object_name(data, bucket, prefix)
    return module._event_object_name(data, bucket, prefix)


@pytest.mark.parametrize(("function_name", "object_name"), FUNCTIONS)
def test_oci_event_selects_only_its_source_object(function_name: str, object_name: str) -> None:
    module = _load_module(function_name)
    prefix = object_name.rsplit("/", 1)[0]
    payload = {
        "eventType": "com.oraclecloud.objectstorage.createobject",
        "data": {
            "resourceName": object_name,
            "additionalDetails": {"bucketName": "mednews-data"},
        },
    }

    assert _event_object(module, function_name, payload, "mednews-data", prefix) == object_name


@pytest.mark.parametrize(("function_name", "object_name"), FUNCTIONS)
def test_oci_event_rejects_an_object_outside_the_stage_prefix(function_name: str, object_name: str) -> None:
    module = _load_module(function_name)
    prefix = object_name.rsplit("/", 1)[0]
    payload = {"data": {"resourceName": "unrelated/another_run.csv"}}

    with pytest.raises(RuntimeError, match="does not identify"):
        _event_object(module, function_name, payload, "mednews-data", prefix)


@pytest.mark.parametrize(("function_name", "object_name"), FUNCTIONS)
def test_empty_manual_payload_selects_drain_mode(function_name: str, object_name: str) -> None:
    module = _load_module(function_name)
    prefix = object_name.rsplit("/", 1)[0]

    if function_name == "retrieve_pubmed_evidence":
        # evidence.v1 is event-scoped by design; the legacy CSV helper still
        # supports an explicit drain invocation.
        return
    assert module._event_object_name_or_none(io.BytesIO(b"{}"), "mednews-data", prefix) is None
