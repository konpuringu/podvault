"""Azure container SAS parsing, metadata access, and Kopia token creation."""

import base64
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .errors import AzureStorageError, CredentialError


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

    def require_azcopy_permissions(self, write: bool) -> None:
        required = {"r", "l"}
        if write:
            required.update({"c", "w"})
        missing = required - self.permissions
        if missing:
            raise CredentialError(
                "Azure container SAS is missing permission(s) required by AzCopy: {}".format(
                    ", ".join(sorted(missing))
                )
            )

    def blob_url(self, blob_path: str, wildcard: bool = False) -> str:
        parsed = urlsplit(self.url)
        safe = "/" + ("*" if wildcard else "")
        encoded = quote(blob_path.strip("/"), safe=safe)
        path = parsed.path.rstrip("/")
        if encoded:
            path += "/" + encoded
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

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


class AzureBlobClient:
    """Minimal Azure Blob REST client for Podvault's small JSON control files."""

    def __init__(self, sas: AzureSAS, timeout: int = 60):
        self.sas = sas
        self.timeout = timeout

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {"x-ms-version": "2023-11-03"}

    def put_json(self, blob_path: str, value: Dict[str, Any]) -> None:
        body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        headers = self._headers()
        headers.update(
            {
                "Content-Type": "application/json",
                "x-ms-blob-type": "BlockBlob",
            }
        )
        request = Request(
            self.sas.blob_url(blob_path), data=body, headers=headers, method="PUT"
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise AzureStorageError(
                "unable to write Azure project metadata at {}: {}".format(blob_path, exc)
            ) from exc

    def get_json(self, blob_path: str, missing_ok: bool = False) -> Optional[Dict[str, Any]]:
        request = Request(self.sas.blob_url(blob_path), headers=self._headers(), method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            raise AzureStorageError(
                "unable to read Azure project metadata at {}: HTTP {}".format(
                    blob_path, exc.code
                )
            ) from exc
        except (URLError, OSError) as exc:
            raise AzureStorageError(
                "unable to read Azure project metadata at {}: {}".format(blob_path, exc)
            ) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise AzureStorageError(
                "Azure project metadata is invalid JSON: {}".format(blob_path)
            ) from exc
        if not isinstance(value, dict):
            raise AzureStorageError(
                "Azure project metadata must be an object: {}".format(blob_path)
            )
        return value

    def list_blobs(self, prefix: str) -> List[Dict[str, Any]]:
        parsed = urlsplit(self.sas.url)
        result: List[Dict[str, Any]] = []
        marker = ""
        while True:
            query = parsed.query + "&restype=container&comp=list&prefix=" + quote(
                prefix, safe="/"
            )
            if marker:
                query += "&marker=" + quote(marker, safe="")
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
            request = Request(url, headers=self._headers(), method="GET")
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
            except HTTPError as exc:
                raise AzureStorageError(
                    "unable to list Azure blobs: HTTP {}".format(exc.code)
                ) from exc
            except (URLError, OSError) as exc:
                raise AzureStorageError("unable to list Azure blobs: {}".format(exc)) from exc
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as exc:
                raise AzureStorageError("Azure blob listing returned invalid XML") from exc
            for item in root.findall("./Blobs/Blob"):
                name = item.findtext("Name")
                if not name:
                    continue
                length = item.findtext("./Properties/Content-Length")
                result.append(
                    {
                        "name": name,
                        "size": int(length) if length and length.isdigit() else None,
                    }
                )
            marker = root.findtext("NextMarker") or ""
            if not marker:
                break
        return result
