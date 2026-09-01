"""Repository initialization and reconnection."""

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .azure import AzureSAS, parse_sas_url
from .config import ConfigStore
from .credentials import CredentialStore
from .errors import ConfigurationError, CredentialError, KopiaCommandError
from .kopia import KopiaRunner, parse_json_document
from .output import Console
from .paths import AppPaths


_NOT_INITIALIZED_MARKERS = (
    "repository is not initialized",
    "repository not initialized",
    "blob not found",
    "the specified blob does not exist",
    "unable to read format blob",
)


class RepositoryManager:
    def __init__(
        self,
        paths: AppPaths,
        config: ConfigStore,
        credentials: CredentialStore,
        console: Console,
    ):
        self.paths = paths
        self.config = config
        self.credentials = credentials
        self.console = console
        self._runner: Optional[KopiaRunner] = None
        self._policy_ready = False

    def _base_runner(self, password: Optional[str] = None, secrets=()) -> KopiaRunner:
        runner = KopiaRunner(
            self.paths.kopia_config_file,
            password=password,
            known_secrets=secrets,
        )
        runner.require_supported_version()
        return runner

    def _password(self, allow_generate: bool) -> str:
        return self.credentials.require_repository_password(allow_generate=allow_generate)

    def _sas(self, write: bool, prompt: bool = True) -> AzureSAS:
        value = self.credentials.require_sas_url(prompt=prompt)
        sas = parse_sas_url(value)
        sas.require_permissions(write=write)
        configured = self.config.repository
        if configured and configured.get("provider") == "azure":
            if configured.get("storage_account") != sas.storage_account or configured.get("container") != sas.container:
                raise CredentialError(
                    "SAS URL points to a different Azure repository than the configured account/container"
                )
        if not configured:
            self.config.set_repository(sas.repository_config())
        return sas

    def _status(self, runner: KopiaRunner) -> Optional[Dict[str, Any]]:
        result = runner.run(["repository", "status", "--json"], check=False)
        if result.returncode != 0:
            return None
        value = parse_json_document(result.stdout)
        return value if isinstance(value, dict) else None

    def _disconnect(self, runner: KopiaRunner) -> None:
        runner.run(["repository", "disconnect"], check=False)

    def _connect_azure(self, runner: KopiaRunner, sas: AzureSAS, create: bool) -> None:
        prefix = self.config.repository.get("prefix", "")
        token = sas.kopia_connection_token(prefix=prefix)
        action = "create" if create else "connect"
        args = [
            "repository",
            action,
            "from-config",
            "--token-stdin",
            "--cache-directory={}".format(self.paths.cache_dir),
            "--no-check-for-updates",
        ]
        runner.run(args, input_text=token)
        try:
            os.chmod(str(self.paths.kopia_config_file), 0o600)
        except OSError:
            pass

    def _connect_filesystem(self, runner: KopiaRunner, create: bool) -> None:
        repo_path = self.config.repository.get("path")
        if not repo_path:
            raise ConfigurationError("filesystem repository path is missing")
        action = "create" if create else "connect"
        args = [
            "repository",
            action,
            "filesystem",
            "--path={}".format(repo_path),
            "--cache-directory={}".format(self.paths.cache_dir),
            "--no-check-for-updates",
        ]
        runner.run(args)

    def _connect(self, runner: KopiaRunner, create: bool, write: bool) -> None:
        provider = self.config.repository.get("provider")
        if provider == "azure":
            self._connect_azure(runner, self._sas(write=write), create=create)
        elif provider == "filesystem":
            self._connect_filesystem(runner, create=create)
        else:
            raise ConfigurationError("repository is not configured")

    def ensure_connected(self, write: bool, allow_create: bool = False) -> KopiaRunner:
        if self._runner is not None and self._status(self._runner) is not None:
            if write:
                self.ensure_policy(self._runner)
            return self._runner

        # A fresh pod has no Podvault configuration yet. Treat the available
        # SAS URL as enough information to reconstruct it, prompting only when
        # neither an environment secret nor a protected local credential exists.
        sas_value = self.credentials.sas_url()
        provider = self.config.repository.get("provider")
        if not self.config.repository or provider == "azure":
            if not sas_value:
                sas_value = self.credentials.require_sas_url(prompt=True)
            sas = parse_sas_url(sas_value)
            sas.require_permissions(write=write)
            if not self.config.repository:
                self.config.set_repository(sas.repository_config())
            elif (
                self.config.repository.get("storage_account") != sas.storage_account
                or self.config.repository.get("container") != sas.container
            ):
                raise CredentialError(
                    "SAS URL points to a different Azure repository than the configured account/container"
                )

        password = self._password(allow_generate=allow_create)
        secrets = [password]
        if sas_value:
            secrets.append(sas_value)
        runner = self._base_runner(password=password, secrets=secrets)
        # Reconnect Azure on each new Podvault invocation. Besides validating
        # access, this guarantees a proactively rotated environment SAS takes
        # effect even while the previous token remains valid.
        force_reconnect = self.config.repository.get("provider") == "azure"
        if force_reconnect and self.paths.kopia_config_file.exists():
            self._disconnect(runner)
        if force_reconnect or self._status(runner) is None:
            if self.paths.kopia_config_file.exists():
                self._disconnect(runner)
            try:
                self._connect(runner, create=False, write=write)
            except KopiaCommandError as connect_error:
                detail = (connect_error.stderr + " " + connect_error.stdout + " " + str(connect_error)).lower()
                if not allow_create or not any(marker in detail for marker in _NOT_INITIALIZED_MARKERS):
                    raise
                self.console.info("No Kopia repository found; initializing it now...")
                self._disconnect(runner)
                self._connect(runner, create=True, write=True)
        self._runner = runner
        if write:
            self.ensure_policy(runner)
        return runner

    def initialize_azure(self, sas_url: Optional[str] = None) -> KopiaRunner:
        if sas_url:
            sas = parse_sas_url(sas_url)
            sas.require_permissions(write=True)
            self.credentials.set_sas_url(sas_url)
            self.config.set_repository(sas.repository_config())
        else:
            sas = self._sas(write=True)
            self.config.set_repository(sas.repository_config())
        password = self._password(allow_generate=True)
        runner = self._base_runner(password=password, secrets=[password, sas.url])
        self._connect_azure(runner, sas, create=True)
        self._runner = runner
        self.ensure_policy(runner)
        return runner

    def initialize_filesystem(self, path: Path) -> KopiaRunner:
        target = path.expanduser().resolve(strict=False)
        self.config.set_repository({"provider": "filesystem", "path": str(target)})
        password = self._password(allow_generate=True)
        runner = self._base_runner(password=password, secrets=[password])
        self._connect_filesystem(runner, create=True)
        self._runner = runner
        self.ensure_policy(runner)
        return runner

    def connect_existing(self, write: bool = False) -> KopiaRunner:
        return self.ensure_connected(write=write, allow_create=False)

    def invalidate_local_connection(self) -> None:
        """Discard only Kopia's reproducible local connection configuration.

        This is used after a SAS rotation so a still-valid old SAS cannot stay
        active indefinitely. The repository and its cache contents are not
        modified; the next command reconnects with the current credential.
        """
        self._runner = None
        self._policy_ready = False
        try:
            self.paths.kopia_config_file.unlink()
        except FileNotFoundError:
            pass

    def ensure_policy(self, runner: KopiaRunner) -> None:
        if self._policy_ready:
            return
        runner.run(
            ["policy", "set", "--global", "--add-dot-ignore=.podvaultignore"],
            check=True,
        )
        self._policy_ready = True

    def status_payload(self, runner: KopiaRunner) -> Dict[str, Any]:
        status = self._status(runner)
        if status is None:
            raise ConfigurationError("repository is not connected")
        return {
            "connected": True,
            "provider": self.config.repository.get("provider"),
            "storage_account": self.config.repository.get("storage_account"),
            "container": self.config.repository.get("container"),
            "repository_id": status.get("uniqueIDHex"),
            "kopia_config": str(self.paths.kopia_config_file),
        }
