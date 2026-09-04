"""Conservative timestamp handling for snapshot-retention operations."""

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from .errors import ConfigurationError, VerificationError


_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LONG_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating dates and naive values as UTC."""

    text = value.strip()
    if not text:
        raise ConfigurationError("deletion cutoff must not be empty")
    if _DATE_ONLY.fullmatch(text):
        text += "T00:00:00+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python 3.9 accepts microseconds but Kopia emits nanosecond timestamps.
    text = _LONG_FRACTION.sub(r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ConfigurationError(
            "invalid deletion cutoff; use YYYY-MM-DD or an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def snapshot_timestamp(snapshot: Dict[str, Any]) -> datetime:
    for key in ("end_time", "endTime", "start_time", "startTime"):
        value = snapshot.get(key)
        if value:
            try:
                return parse_timestamp(str(value))
            except ConfigurationError as exc:
                raise VerificationError(
                    "stored snapshot has an invalid timestamp: {}".format(value)
                ) from exc
    raise VerificationError("stored snapshot does not contain a timestamp")


def snapshots_before(
    snapshots: Iterable[Dict[str, Any]], cutoff_value: str
) -> List[Dict[str, Any]]:
    cutoff = parse_timestamp(cutoff_value)
    return [item for item in snapshots if snapshot_timestamp(item) < cutoff]


def snapshots_through(
    snapshots: Iterable[Dict[str, Any]], anchor: Dict[str, Any]
) -> List[Dict[str, Any]]:
    cutoff = snapshot_timestamp(anchor)
    return [item for item in snapshots if snapshot_timestamp(item) <= cutoff]
