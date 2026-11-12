"""Agent API keys (ADR-024). Format ``ame_agt_<43 url-safe chars>`` (256-bit secret).

Only ``sha256$<hex>`` is persisted. The secret has full entropy, so an unsalted SHA-256 is a
safe verifier (no password-style stretching needed) and allows an indexed exact lookup.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

AGENT_KEY_PREFIX = "ame_agt_"


def digest_key(key: str) -> str:
    return "sha256$" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_agent_key() -> tuple[str, str]:
    key = AGENT_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, digest_key(key)


def looks_like_agent_key(presented: str) -> bool:
    return presented.startswith(AGENT_KEY_PREFIX) and len(presented) >= len(AGENT_KEY_PREFIX) + 32


def digest_matches(stored: str | None, presented: str) -> bool:
    if not stored:
        return False
    return hmac.compare_digest(stored.encode(), digest_key(presented).encode())
