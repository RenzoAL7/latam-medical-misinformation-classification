"""Explicit entry point for the translation-only OCI Function.

The deployed translation Function intentionally uses the same image as
``retrieve-pubmed-evidence``.  Keeping the implementation in one place avoids
drift between PubMed retrieval and translation.  This entry point makes the
sixth OCI Function visible and testable in the repository while delegating to
the shared module next to it.

The production image is built from ``retrieve_pubmed_evidence/`` and the OCI
Function is configured with ``PIPELINE_MODE=translation``.  The relative load
below is therefore a repository-level source map and local entry point; it is
not a second copy of the implementation.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any


_SHARED_FUNCTION_PATH = (
    Path(__file__).resolve().parents[1] / "retrieve_pubmed_evidence" / "func.py"
)
_SHARED_MODULE_NAME = "retrieve_pubmed_evidence_shared_function"


def _load_shared_function() -> ModuleType:
    """Load the shared implementation from the repository checkout."""

    if not _SHARED_FUNCTION_PATH.is_file():
        raise RuntimeError(
            "The shared retrieve_pubmed_evidence Function source is missing: "
            f"{_SHARED_FUNCTION_PATH}"
        )
    spec = importlib.util.spec_from_file_location(
        _SHARED_MODULE_NAME,
        _SHARED_FUNCTION_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load shared Function source: {_SHARED_FUNCTION_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SHARED_FUNCTION = _load_shared_function()


def handler(ctx: Any, data: Any = None) -> Any:
    """Dispatch only the translation mode of the shared Function."""

    previous_mode = os.environ.get("PIPELINE_MODE")
    os.environ["PIPELINE_MODE"] = "translation"
    try:
        return _SHARED_FUNCTION.handler(ctx, data)
    finally:
        if previous_mode is None:
            os.environ.pop("PIPELINE_MODE", None)
        else:
            os.environ["PIPELINE_MODE"] = previous_mode
