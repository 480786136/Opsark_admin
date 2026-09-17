"""Bounded redacted snapshots. Never collect HTTP headers or raw exception text."""

import json
import re

LIMIT = 64 * 1024
SENSITIVE = re.compile(r"password|passwd|secret|api[_-]?key|authorization|cookie|(?:access|refresh)[_-]?token", re.IGNORECASE)


def sanitize(value, secrets=(), depth=0):
    if depth > 30:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SENSITIVE.search(k) else sanitize(v, secrets, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, secrets, depth + 1) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", value
    )
    value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"\b(?:sk-|omk_|ouc_|our_)[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:password|passwd|secret|api[_-]?key|(?:access|refresh)[_-]?token)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", value)
    return value


def snapshot(value, secrets=()):
    content = json.dumps(sanitize(value, secrets), ensure_ascii=False, indent=2)
    encoded = content.encode()
    return {"text": encoded[:LIMIT].decode("utf-8", errors="ignore"), "truncated": len(encoded) > LIMIT}
