"""Secret redaction for the config payload sent to the browser (v1.63).

The original approach was a DENY-LIST: clone the whole config, then `pop` the
three secrets someone had remembered. That fails OPEN — every new secret field
is shipped until a human notices and adds another `pop`. The history showed the
list growing reactively (v1.41, v1.51, v1.52), which is a bug waiting to happen.

This fails CLOSED instead: anything whose key *looks* like a credential is
stripped by pattern, so a field added tomorrow is redacted without anyone
remembering anything. The UI still needs to know whether a value is stored, so
each redacted key is replaced with a `<key>_set` boolean.

Three holes were found by an adversarial review of the FIRST cut of this file
and are closed here — that version was only fail-closed for the exact
substrings it happened to list:

  * bare ``key`` suffixes slipped through. ``api_key`` matched, but
    ``openai_key``, ``access_key`` and ``llm_external_key`` did not — the same
    reactive failure as the deny-list, just relocated.
  * it was SHALLOW. Nested containers were copied by reference, so a secret
    inside ``zones``/``providers``/``headers`` was never inspected.
  * ``*_set`` was a blanket bypass, so a real secret named ``token_set`` was
    exempt. The exemption now applies only to booleans, which is what a flag
    always is and a credential never is.

Pure + HA-free so it is directly testable.
"""

from __future__ import annotations

import re
from typing import Any

# A key is secret if any of these appears in it (case-insensitive).
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

# ...plus any key that IS "key"/"keys" or ends in "_key"/"_keys". Anchored so
# ordinary words containing those letters (keyword, monkey, keypad) do not match.
_KEY_RE = re.compile(r"(?:^|_)keys?$", re.IGNORECASE)

# Keys that MATCH a pattern but are not secrets and are needed by the UI.
# Every entry is a deliberate exception, which is the point: adding one is a
# visible decision, whereas forgetting a `pop` was invisible.
_NOT_SECRET = frozenset(
    {
        "admin_only_services",
    }
)

# How deep to walk before giving up. Config is a few levels at most; the bound
# exists so a pathological structure cannot spin forever.
_MAX_DEPTH = 6


def is_secret_key(key: Any) -> bool:
    """True if this config key should never reach the browser."""
    if not isinstance(key, str):
        return False
    if key in _NOT_SECRET:
        return False
    return bool(_SECRET_RE.search(key) or _KEY_RE.search(key))


def _is_set(value: Any) -> bool:
    """Whether a stored value counts as configured.

    Whitespace-only counts as NOT set — an accidental " " should not read as
    configured in the UI.
    """
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != "" and value is not False


def _redact(value: Any, depth: int) -> Any:
    """Recursively strip secret-looking keys from nested containers."""
    if depth > _MAX_DEPTH:
        # Deeper than any real config; refuse to ship what we did not inspect.
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            # A *_set flag we emitted is a bool; a credential never is. This
            # keeps redact_config idempotent without exempting every incoming
            # key that merely ends in "_set".
            if isinstance(key, str) and key.endswith("_set") and isinstance(val, bool):
                out[key] = val
                continue
            if is_secret_key(key):
                out[f"{key}_set"] = _is_set(val)
                continue
            out[key] = _redact(val, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value]
    return value


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``config`` with every secret-looking value removed.

    Each stripped key ``k`` gains ``k + "_set"``: a boolean saying whether a
    non-empty value is stored, so the settings UI can show "key saved" and pick
    its placeholder without the secret leaving the server.

    NOTE on URL fields: ``vision_url`` / ``llm_external_url`` ship whole,
    including any query string. That is deliberate. The panel renders them in an
    editable text input, so scrubbing would make the admin's next save clobber
    their own URL with the redacted form — a data-loss bug traded for no real
    gain, since this command is admin-only and the value is going back to the
    person who typed it. Contrast ``safe_error_text`` in llm_client: that text
    is LOGGED and surfaced in user-facing errors, so there the query string is
    scrubbed.
    """
    if not isinstance(config, dict):
        return {}
    return _redact(config, 0)
