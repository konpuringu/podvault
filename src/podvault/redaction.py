"""Best-effort defense against accidental secret disclosure."""

import re
from typing import Iterable


_QUERY_SECRET = re.compile(
    r"(?i)(sig|sas-token|storage-key|client-secret|password)=([^&\s]+)"
)
_AZURE_URL = re.compile(r"https://[^\s?]+\?[^\s]+", re.IGNORECASE)


def redact(value: str, known_secrets: Iterable[str] = ()) -> str:
    result = value
    for secret in known_secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    result = _AZURE_URL.sub("[REDACTED_AZURE_SAS_URL]", result)
    result = _QUERY_SECRET.sub(lambda match: match.group(1) + "=[REDACTED]", result)
    return result
