"""Podvault command-line interface."""

import argparse
import getpass
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .azure import parse_sas_url
from .config import ConfigStore
from .credentials import CredentialStore
from .errors import ConfigurationError, PodvaultError
from .kopia import KopiaRunner
from .output import Console
from .paths import AppPaths, validate_source
from .project import validate_project_name
from .receipts import ReceiptStore
from .repository import RepositoryManager
from .restore import RestoreService
from .snapshots import SnapshotService, list_snapshots, snapshot_view


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podvault",
        description="Save and restore ephemeral GPU-pod projects with Kopia.",
    )
    parser.add_argument("--config", help="explicit Podvault configuration path")
    parser.add_argument("--json", action="store_true", help="emit the final result as JSON")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)

    repository = commands.add_parser("repository", help="manage the Kopia repository")
    repository_commands = repository.add_subparsers(dest="repository_command", required=True)
    init = repository_commands.add_parser("init", help="initialize a new repository")
    init_backends = init.add_subparsers(dest="backend", required=True)
    init_azure = init_backends.add_parser("azure", help="initialize from a container SAS URL")
    init_azure.add_argument("--sas-url-file", help="0600 file containing the container SAS URL")
    init_azure.add_argument(
        "--repository-password-file", help="0600 file containing the repository password"
    )
    init_filesystem = init_backends.add_parser("filesystem", help="initialize a local test repository")
    init_filesystem.add_argument("path")
    init_filesystem.add_argument("--password-file", help="0600 repository password file")

    connect = repository_commands.add_parser("connect", help="connect to an existing repository")
    connect_backends = connect.add_subparsers(dest="backend", required=True)
    connect_azure = connect_backends.add_parser("azure", help="connect using a container SAS URL")
    connect_azure.add_argument("--sas-url-file", help="0600 file containing the container SAS URL")
    connect_azure.add_argument(
        "--repository-password-file", help="0600 file containing the repository password"
    )
    connect_filesystem = connect_backends.add_parser("filesystem", help="connect to a local repository")
    connect_filesystem.add_argument("path")
    connect_filesystem.add_argument("--password-file", help="0600 repository password file")
    repository_commands.add_parser("status", help="show safe repository status")

    credentials = commands.add_parser("credentials", help="update protected credentials")
    credential_commands = credentials.add_subparsers(dest="credentials_command", required=True)
    update = credential_commands.add_parser("update", help="replace SAS URL and/or password")
    update.add_argument("--sas-url-file", help="0600 file containing the new SAS URL")
    update.add_argument("--repository-password-file", help="0600 repository password file")

    configure = commands.add_parser("configure", help="remember a project path")
    configure.add_argument("path")
    configure.add_argument("--name", required=True)

    save = commands.add_parser("save", help="save a project, or preview it with --dry-run")
    save.add_argument("target", help="configured project name or source path")
    save.add_argument("--name", help="stable project name when target is a path")
    save.add_argument("--description", default="")
    save.add_argument("--dry-run", action="store_true")
    save.add_argument(
        "--no-progress", action="store_true", help="disable live Kopia progress output"
    )

    listing = commands.add_parser("list", help="list Podvault snapshots")
    listing.add_argument("project", nargs="?")

    restore = commands.add_parser("restore", help="restore a project")
    restore.add_argument("project")
    restore_selection = restore.add_mutually_exclusive_group()
    restore_selection.add_argument("--latest", action="store_true", help="restore latest snapshot (default)")
    restore_selection.add_argument("--snapshot", help="stable Podvault or current Kopia snapshot ID")
    restore.add_argument("--to", dest="destination")
    restore.add_argument(
        "--no-progress", action="store_true", help="disable live Kopia progress output"
    )

    verify = commands.add_parser("verify", help="verify a snapshot")
    verify.add_argument("project")
    verify_selection = verify.add_mutually_exclusive_group()
    verify_selection.add_argument("--latest", action="store_true", help="verify latest snapshot (default)")
    verify_selection.add_argument("--snapshot")
    verify.add_argument("--sample-percent", type=float, default=0.0)
    verify.add_argument(
        "--no-progress", action="store_true", help="disable live Kopia progress output"
    )

    pin = commands.add_parser("pin", help="label and retain a snapshot")
    pin.add_argument("project")
    pin_selection = pin.add_mutually_exclusive_group()
    pin_selection.add_argument("--latest", action="store_true", help="pin latest snapshot (default)")
    pin_selection.add_argument("--snapshot")
    pin.add_argument("--label", required=True)

    commands.add_parser("doctor", help="check dependencies, credentials, and repository access")
    return parser


class Application:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.console = Console(args.json)
        self.paths = AppPaths.discover(args.config)
        self.paths.ensure_directories()
        self.config = ConfigStore(self.paths.config_file)
        self.credentials = CredentialStore(self.paths.credentials_file)
        self.repository = RepositoryManager(
            self.paths, self.config, self.credentials, self.console
        )
        self.receipts = ReceiptStore(self.paths.receipts_dir)

    def _protected_paths(self) -> List[Path]:
        result = [
            self.paths.config_file,
            self.paths.credentials_file,
            self.paths.kopia_config_file,
            self.paths.cache_dir,
            self.paths.state_dir,
        ]
        repository = self.config.repository
        if repository.get("provider") == "filesystem" and repository.get("path"):
            result.append(Path(repository["path"]))
        return result

    def _project_source(self, target: str, explicit_name: Optional[str]) -> Tuple[str, Path]:
        if explicit_name:
            project = validate_project_name(explicit_name)
            source_value = target
        else:
            project = validate_project_name(target)
            configured = self.config.get_project(project)
            if not configured:
                raise ConfigurationError(
                    "project is not configured; use a path with --name for the first save"
                )
            source_value = configured["path"]
        source = validate_source(source_value, self._protected_paths())
        self.config.set_project(project, source)
        return project, source

    def _snapshot_service(self, write: bool, allow_create: bool = False) -> SnapshotService:
        runner = self.repository.ensure_connected(write=write, allow_create=allow_create)
        return SnapshotService(runner, self.console, self.receipts)

    def run(self) -> int:
        command = self.args.command
        if command == "repository":
            return self._repository_command()
        if command == "credentials":
            return self._credentials_command()
        if command == "configure":
            return self._configure()
        if command == "save":
            return self._save()
        if command == "list":
            return self._list()
        if command == "restore":
            return self._restore()
        if command == "verify":
            return self._verify()
        if command == "pin":
            return self._pin()
        if command == "doctor":
            return self._doctor()
        raise ConfigurationError("unknown command")

    def _repository_command(self) -> int:
        action = self.args.repository_command
        if action == "init":
            if self.args.backend == "azure":
                if self.args.repository_password_file:
                    password = self.credentials.read_secret_file(
                        self.args.repository_password_file
                    )
                    self.credentials.set_repository_password(password)
                sas = None
                if self.args.sas_url_file:
                    sas = self.credentials.read_secret_file(self.args.sas_url_file)
                runner = self.repository.initialize_azure(sas)
            else:
                if self.args.password_file:
                    password = self.credentials.read_secret_file(self.args.password_file)
                    self.credentials.set_repository_password(password)
                runner = self.repository.initialize_filesystem(Path(self.args.path))
            payload = self.repository.status_payload(runner)
            self.console.result(payload, ["Repository initialized and connected."])
            return 0
        if action == "connect":
            if self.args.backend == "azure":
                if self.args.repository_password_file:
                    password = self.credentials.read_secret_file(
                        self.args.repository_password_file
                    )
                    self.credentials.set_repository_password(password)
                if self.args.sas_url_file:
                    sas_value = self.credentials.read_secret_file(self.args.sas_url_file)
                else:
                    sas_value = self.credentials.require_sas_url(prompt=True)
                sas = parse_sas_url(sas_value)
                self.credentials.set_sas_url(sas_value)
                self.config.set_repository(sas.repository_config())
            else:
                if self.args.password_file:
                    password = self.credentials.read_secret_file(self.args.password_file)
                    self.credentials.set_repository_password(password)
                self.config.set_repository(
                    {"provider": "filesystem", "path": str(Path(self.args.path).expanduser().resolve())}
                )
            runner = self.repository.connect_existing(write=False)
            payload = self.repository.status_payload(runner)
            self.console.result(payload, ["Repository connected."])
            return 0
        runner = self.repository.connect_existing(write=False)
        payload = self.repository.status_payload(runner)
        human = [
            "Repository connected: YES",
            "Provider: {}".format(payload.get("provider")),
            "Repository ID: {}".format(payload.get("repository_id")),
        ]
        self.console.result(payload, human)
        return 0

    def _credentials_command(self) -> int:
        changed = []
        if self.args.sas_url_file:
            sas_value = self.credentials.read_secret_file(self.args.sas_url_file)
        elif sys.stdin.isatty():
            sas_value = getpass.getpass("New Azure container SAS URL (blank to keep current): ").strip()
        else:
            sas_value = ""
        if sas_value:
            sas = parse_sas_url(sas_value)
            configured = self.config.repository
            if configured and configured.get("provider") == "azure":
                if configured.get("storage_account") != sas.storage_account or configured.get("container") != sas.container:
                    raise ConfigurationError("new SAS URL points to a different account or container")
            self.credentials.set_sas_url(sas_value)
            self.config.set_repository(sas.repository_config(configured.get("prefix", "") if configured else ""))
            changed.append("azure_sas_url")
        if self.args.repository_password_file:
            password = self.credentials.read_secret_file(self.args.repository_password_file)
            self.credentials.set_repository_password(password)
            changed.append("repository_password")
        if not changed:
            raise ConfigurationError("no credentials were updated")
        if os.environ.get("PODVAULT_AZURE_SAS_URL") and "azure_sas_url" in changed:
            self.console.warning(
                "PODVAULT_AZURE_SAS_URL is set and will override the stored SAS on the next command"
            )
        elif "azure_sas_url" in changed:
            self.repository.invalidate_local_connection()
        payload = {"status": "updated", "changed": changed}
        self.console.result(payload, ["Credentials updated: {}".format(", ".join(changed))])
        return 0

    def _configure(self) -> int:
        project = validate_project_name(self.args.name)
        source = validate_source(self.args.path, self._protected_paths())
        self.config.set_project(project, source)
        if self.config.repository or self.credentials.sas_url():
            self.repository.ensure_connected(write=True, allow_create=False)
        payload = {"status": "configured", "project": project, "path": str(source)}
        self.console.result(payload, ["Configured {} -> {}".format(project, source)])
        return 0

    def _save(self) -> int:
        project, source = self._project_source(self.args.target, self.args.name)
        service = self._snapshot_service(write=True, allow_create=True)
        if self.args.dry_run:
            payload = service.dry_run(
                source, project, show_progress=not self.args.no_progress
            )
            if self.console.json_mode:
                self.console.result(payload, [])
            else:
                print(payload["estimate"])
            return 0
        payload = service.save(
            source,
            project,
            self.args.description,
            show_progress=not self.args.no_progress,
        )
        for item in payload.get("warnings", []):
            self.console.warning("recently changing or incomplete-looking file: {}".format(item))
        human = [
            "Snapshot: {}".format(project),
            "Podvault snapshot ID: {}".format(payload["podvault_snapshot_id"]),
            "Kopia manifest: {}".format(payload["kopia_manifest_id"]),
            "Structural verification: PASSED",
            "TRAINING QUIESCENCE: NOT ESTABLISHED BY PODVAULT",
            "SAFE TO TERMINATE: YES",
            "Receipt: {}".format(payload["receipt_path"]),
        ]
        self.console.result(payload, human)
        return 0

    def _list(self) -> int:
        project = validate_project_name(self.args.project) if self.args.project else None
        runner = self.repository.ensure_connected(write=False, allow_create=False)
        values = [snapshot_view(item) for item in list_snapshots(runner, project)]
        payload = {"snapshots": values, "count": len(values)}
        human = []
        for item in values:
            summary = item.get("summary") or {}
            human.append(
                "{}  {}  {}  {} files  {}".format(
                    (item.get("podvault_snapshot_id") or "-")[:12],
                    item.get("end_time") or "-",
                    item.get("project") or "-",
                    summary.get("files", 0),
                    item.get("description") or "",
                ).rstrip()
            )
        if not human:
            human = ["No Podvault snapshots found."]
        self.console.result(payload, human)
        return 0

    def _restore(self) -> int:
        project = validate_project_name(self.args.project)
        runner = self.repository.ensure_connected(write=False, allow_create=False)
        service = RestoreService(runner, self.console, self.receipts, self.config)
        payload = service.restore(
            project,
            self.args.snapshot,
            self.args.destination,
            show_progress=not self.args.no_progress,
        )
        human = [
            "Restored {} to {}".format(project, payload["destination"]),
            "Structural verification: PASSED",
            "Restored tree verification: PASSED",
            "Receipt: {}".format(payload["receipt_path"]),
        ]
        self.console.result(payload, human)
        return 0

    def _verify(self) -> int:
        project = validate_project_name(self.args.project)
        service = self._snapshot_service(write=False)
        payload = service.verify(
            project,
            self.args.snapshot,
            self.args.sample_percent,
            show_progress=not self.args.no_progress,
        )
        human = [
            "Verification passed for {} ({})".format(
                project, payload["verification"]["mode"]
            )
        ]
        self.console.result(payload, human)
        return 0

    def _pin(self) -> int:
        project = validate_project_name(self.args.project)
        service = self._snapshot_service(write=True)
        payload = service.pin(project, self.args.snapshot, self.args.label)
        human = [
            "Pinned {} with label '{}'".format(project, self.args.label),
            "Current Kopia manifest: {}".format(payload["kopia_manifest_id"]),
        ]
        self.console.result(payload, human)
        return 0

    def _doctor(self) -> int:
        checks: List[Dict[str, Any]] = []
        healthy = True
        try:
            runner = KopiaRunner(self.paths.kopia_config_file)
            version = runner.require_supported_version()
            checks.append({"name": "kopia", "status": "ok", "version": ".".join(map(str, version))})
        except PodvaultError as exc:
            healthy = False
            checks.append({"name": "kopia", "status": "error", "detail": str(exc)})
        for name, path in (
            ("configuration", self.paths.config_file),
            ("credentials", self.paths.credentials_file),
            ("kopia-config", self.paths.kopia_config_file),
        ):
            if not path.exists():
                checks.append({"name": name, "status": "not-present", "path": str(path)})
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            ok = not bool(mode & 0o077)
            healthy = healthy and ok
            checks.append(
                {"name": name, "status": "ok" if ok else "error", "mode": oct(mode), "path": str(path)}
            )
        sas_value = self.credentials.sas_url()
        if sas_value:
            try:
                sas = parse_sas_url(sas_value)
                remaining = None
                if sas.expires_at:
                    remaining = int((sas.expires_at - datetime.now(timezone.utc)).total_seconds())
                checks.append(
                    {
                        "name": "azure-sas",
                        "status": "ok",
                        "account": sas.storage_account,
                        "container": sas.container,
                        "expires_at": sas.expires_at.isoformat() if sas.expires_at else None,
                        "seconds_remaining": remaining,
                    }
                )
            except PodvaultError as exc:
                healthy = False
                checks.append({"name": "azure-sas", "status": "error", "detail": str(exc)})
        try:
            connected = self.repository.ensure_connected(write=False, allow_create=False)
            checks.append(
                {"name": "repository", "status": "ok", **self.repository.status_payload(connected)}
            )
        except PodvaultError as exc:
            healthy = False
            checks.append({"name": "repository", "status": "error", "detail": str(exc)})
        payload = {"healthy": healthy, "checks": checks}
        human = [
            "{}: {}".format(item["name"], item["status"].upper()) for item in checks
        ]
        self.console.result(payload, human)
        return 0 if healthy else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return Application(args).run()
    except KeyboardInterrupt:
        print("ERROR: interrupted; any restore staging directory was preserved for recovery", file=sys.stderr)
        return 130
    except PodvaultError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        receipt_path = getattr(exc, "receipt_path", None)
        if receipt_path:
            print("Failure receipt: {}".format(receipt_path), file=sys.stderr)
        return exc.exit_code
    except Exception as exc:
        print("ERROR: unexpected failure: {}".format(exc), file=sys.stderr)
        return 1
