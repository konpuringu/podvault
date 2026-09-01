"""Secret loading and protected local credential storage."""

import getpass
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .config import atomic_json_write
from .errors import CredentialError


class CredentialStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1}
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                raise CredentialError(
                    "credentials file must not be accessible by group or others: {}".format(self.path)
                )
            with self.path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except CredentialError:
            raise
        except (OSError, ValueError) as exc:
            raise CredentialError("invalid credentials file: {}".format(self.path)) from exc
        if value.get("schema_version") != 1:
            raise CredentialError("unsupported credentials schema")
        return value

    def save(self) -> None:
        atomic_json_write(self.path, self.data, 0o600)

    def sas_url(self) -> Optional[str]:
        return os.environ.get("PODVAULT_AZURE_SAS_URL") or self.data.get("azure_sas_url")

    def repository_password(self) -> Optional[str]:
        return (
            os.environ.get("PODVAULT_REPOSITORY_PASSWORD")
            or os.environ.get("KOPIA_PASSWORD")
            or self.data.get("repository_password")
        )

    def set_sas_url(self, value: str) -> None:
        self.data["azure_sas_url"] = value.strip()
        self.save()

    def set_repository_password(self, value: str) -> None:
        if not value:
            raise CredentialError("repository password may not be empty")
        self.data["repository_password"] = value
        self.save()

    def require_sas_url(self, prompt: bool = True) -> str:
        value = self.sas_url()
        if value:
            return value.strip()
        if not prompt or not sys.stdin.isatty():
            raise CredentialError(
                "Azure SAS URL is required; set PODVAULT_AZURE_SAS_URL or run interactively"
            )
        value = getpass.getpass("Azure container SAS URL: ").strip()
        if not value:
            raise CredentialError("Azure SAS URL may not be empty")
        self.set_sas_url(value)
        return value

    def require_repository_password(self, allow_generate: bool, prompt: bool = True) -> str:
        value = self.repository_password()
        if value:
            return value
        if not prompt or not sys.stdin.isatty():
            raise CredentialError(
                "repository password is required; set PODVAULT_REPOSITORY_PASSWORD"
            )
        entered = getpass.getpass(
            "Repository password{}: ".format(" (leave blank to generate)" if allow_generate else "")
        )
        if entered:
            confirm = getpass.getpass("Confirm repository password: ")
            if entered != confirm:
                raise CredentialError("repository passwords did not match")
            value = entered
        elif allow_generate:
            value = "pv1-" + secrets.token_urlsafe(32)
            print("\nGenerated repository recovery key:\n{}\n".format(value), file=sys.stderr)
            print(
                "Store this key in a password manager or RunPod Secret before deleting this pod.",
                file=sys.stderr,
            )
            confirmation = input("Type SAVED to confirm: ").strip()
            if confirmation != "SAVED":
                raise CredentialError("recovery key was not confirmed as saved")
        else:
            raise CredentialError("repository password may not be empty")
        self.set_repository_password(value)
        return value

    @staticmethod
    def read_secret_file(path_value: str) -> str:
        path = Path(path_value).expanduser()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise CredentialError("secret file must have mode 0600 or stricter: {}".format(path))
            value = path.read_text(encoding="utf-8").strip()
        except CredentialError:
            raise
        except OSError as exc:
            raise CredentialError("unable to read secret file: {}".format(path)) from exc
        if not value:
            raise CredentialError("secret file is empty: {}".format(path))
        return value
