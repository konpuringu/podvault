"""Azure container SAS parsing and secure Kopia token creation."""

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Set
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import CredentialError


@dataclass(frozen=True)
class AzureSAS:
    url: str
    storage_account: str
    container: str
    storage_domain: str
    token: str
    permissions: Set[str]
    expires_at: Optional[datetime]

    def repository_config(self, prefix: str = "") -> Dict[str, str]:
        value = {
            "provider": "azure",
            "storage_account": self.storage_account,
            "container": self.container,
            "storage_domain": self.storage_domain,
        }
        if prefix:
            value["prefix"] = prefix
        return value

    def kopia_connection_token(self, prefix: str = "") -> str:
        config = {
            "container": self.container,
            "storageAccount": self.storage_account,
            "sasToken": self.token,
            "storageDomain": self.storage_domain,
        }
        if prefix:
            config["prefix"] = prefix
        value = {
            "version": "1",
            "storage": {"type": "azureBlob", "config": config},
        }
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    def require_permissions(self, write: bool) -> None:
        required = {"r", "l"}
        if write:
            required.update({"w"})
        missing = required - self.permissions
        if missing:
            raise CredentialError(
                "Azure container SAS is missing required permission(s): {}".format(
                    ", ".join(sorted(missing))
                )
            )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        return self.expires_at <= current


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    decoded = unquote(value)
    if decoded.endswith("Z"):
        decoded = decoded[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(decoded)
    except ValueError as exc:
        raise CredentialError("Azure SAS has an invalid expiry timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_sas_url(value: str) -> AzureSAS:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise CredentialError("invalid Azure SAS URL") from exc
    if parsed.scheme.lower() != "https":
        raise CredentialError("Azure SAS URL must use HTTPS")
    if not parsed.hostname or ".blob." not in parsed.hostname:
        raise CredentialError("Azure SAS URL must use an Azure Blob Storage hostname")
    account, domain = parsed.hostname.split(".", 1)
    if not account or not domain.startswith("blob."):
        raise CredentialError("unable to determine Azure storage account from SAS URL")
    components = [unquote(part) for part in parsed.path.split("/") if part]
    if len(components) != 1:
        raise CredentialError("SAS URL must target a container, not an individual blob or directory")
    params = parse_qs(parsed.query, keep_blank_values=True)
    if not params.get("sig") or not parsed.query:
        raise CredentialError("Azure SAS URL does not contain a signature")
    resource = params.get("sr", [""])[0]
    if resource != "c":
        raise CredentialError("Azure SAS must be container-scoped (sr=c)")
    protocol = params.get("spr", [""])[0]
    if protocol and protocol.lower() != "https":
        raise CredentialError("Azure SAS must restrict access to HTTPS")
    permissions = set(params.get("sp", [""])[0])
    expires_at = _parse_time(params.get("se", [None])[0])
    result = AzureSAS(
        url=raw,
        storage_account=account,
        container=components[0],
        storage_domain=domain,
        token=parsed.query,
        permissions=permissions,
        expires_at=expires_at,
    )
    if result.is_expired():
        raise CredentialError("Azure SAS has expired")
    return result
