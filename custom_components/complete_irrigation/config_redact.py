"""Secret redaction for the config payload sent to the browser (v1.63).

The old approach was a DENY-LIST: clone the whole config, then `pop` the three
secrets someone had remembered. That fails OPEN — every new secret field is
shipped to the browser until a human notices and adds another `pop`. The history
showed the list growing reactively (v1.41, v1.51, v1.52), which is exactly the
shape of a bug waiting to happen.

This fails CLOSED instead: anything whose key *looks* like a credential is
stripped by pattern, so a field added tomorrow is redacted without anyone
remembering anything. The UI still needs to know whether a key is stored, so
each redacted key is replaced with a `<key>_set` boolean.

Pure + HA-free so it is directly testable.
"""

from __future__ import annotations

import re
from typing import Any

# A key is treated as secret if any of these appears in it (case-insensitive).
# Deliberately broad: a false positive costs the UI one boolean, a false
# negative leaks a credential to every browser session.
_SECRET_HINTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "private_key",
    "client_secret",
    "auth",
    "bearer",
    "webhook_url",  # commonly carries an embedded token
)

_SECRET_RE = re.compile("|".join(re.escape(h) for h in _SECRET_HINTS), re.IGNORECASE)

# Keys that MATCH the pattern but are not secrets and are needed by the UI.
# Every entry here is a deliberate exception, which is the point: adding one is
# a visible decision, whereas forgetting a `pop` was invisible.
_NOT_SECRET = frozenset(
    {
        "llm_external_api_key_set",
        "plantnet_api_key_set",
        "perenual_api_key_set",
        "admin_only_services",  # contains "auth"? no — kept explicit for clarity
    }
)


def is_secret_key(key: Any) -> bool:
    """True if this config key should never reach the browser."""
    if not isinstance(key, str):
        return False
    if key in _NOT_SECRET or key.endswith("_set"):
        return False
    return bool(_SECRET_RE.search(key))


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``config`` with every secret-looking value removed.

    Each stripped key ``k`` gains ``k + "_set"``: a boolean saying whether a
    non-empty value is stored, so the settings UI can show "key saved" and pick
    its placeholder without the secret leaving the server. Whitespace-only
    values count as NOT set — an accidental " " should not read as configured.
    """
    if not isinstance(config, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in config.items():
        if is_secret_key(key):
            stored = isinstance(value, str) and bool(value.strip())
            if not isinstance(value, str):
                stored = value is not None and value != "" and value is not False
            out[f"{key}_set"] = bool(stored)
            continue
        out[key] = value
    return out
