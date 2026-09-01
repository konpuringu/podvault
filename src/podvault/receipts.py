"""Atomic operation receipts."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .config import atomic_json_write


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReceiptStore:
    def __init__(self, root: Path):
        self.root = root

    def write(self, project: str, operation: str, value: Dict[str, Any]) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        identifier = (
            value.get("podvault_snapshot_id")
            or value.get("kopia_manifest_id")
            or "result"
        )
        destination = self.root / project / "{}-{}-{}.json".format(
            timestamp, operation, str(identifier)[:16]
        )
        payload = dict(value)
        payload.setdefault("receipt_schema", 1)
        payload.setdefault("created_at", utc_now())
        payload.setdefault("operation", operation)
        payload.setdefault("project", project)
        atomic_json_write(destination, payload, 0o600)
        return destination
