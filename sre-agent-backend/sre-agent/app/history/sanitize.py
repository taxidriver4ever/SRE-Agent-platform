"""Conservative redaction for search copies; original MySQL records stay intact."""
import re
from typing import Any

_SECRET = re.compile(r"""(?i)(password|passwd|token|authorization|api[_-]?key|secret)(["']?\s*[:=]\s*["']?)([^\s,;"'}]+)""")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub(r"\1\2[REDACTED]", _BEARER.sub("Bearer [REDACTED]", value))
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()
                if key.lower() not in {"password", "passwd", "token", "authorization", "api_key", "secret"}}
    return value
