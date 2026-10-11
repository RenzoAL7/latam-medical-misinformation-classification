"""Authentication-header compatibility for Label Studio API tokens."""

from __future__ import annotations


def authorization_header(token: str) -> dict[str, str]:
    """Return the correct authorization scheme for legacy and JWT tokens.

    Self-hosted Label Studio installations can expose either the legacy
    40-character token API (``Token``) or personal access tokens encoded as a
    JWT (``Bearer``).  Supporting both makes local and VM imports portable.
    """

    token = token.strip()
    if not token:
        return {}
    scheme = "Bearer" if token.count(".") == 2 else "Token"
    return {"Authorization": f"{scheme} {token}"}
