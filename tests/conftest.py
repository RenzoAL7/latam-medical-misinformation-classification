"""Test-only shims for OCI Functions imported outside the OCI runtime image."""

from __future__ import annotations

import sys
import types


try:
    import oci  # type: ignore[import-not-found]
except ModuleNotFoundError:
    oci = types.ModuleType("oci")
    sys.modules["oci"] = oci

# The repository's ``oci/`` directory is a namespace package in the local
# checkout, not the OCI SDK installed in a Function image.  Unit tests exercise
# pure selection/ranking helpers and do not call the SDK clients.
for name in ("auth", "object_storage", "generative_ai_inference", "secrets", "retry"):
    if not hasattr(oci, name):
        setattr(oci, name, types.SimpleNamespace())

if "fdk" not in sys.modules:
    fdk = types.ModuleType("fdk")
    fdk.response = types.SimpleNamespace(Response=object)
    sys.modules["fdk"] = fdk
