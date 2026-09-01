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
from .azure import AzureBlobClient, AzureSAS, parse_sas_url
from .azcopy import AzCopyRunner
from .catalog import ProjectCatalog, SUPPORTED_ENGINES
from .config import ConfigStore
from .credentials import CredentialStore
from .direct import AzCopyService
from .errors import ConfigurationError, PodvaultError
from .kopia import KopiaRunner
from .output import Console
from .paths import AppPaths, validate_source
from .project import validate_project_name
from .receipts import ReceiptStore
from .repository import RepositoryManager
from .restore import RestoreService
from .snapshots import SnapshotService, list_snapshots, snapshot_view


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podvault",
        description="Save and restore ephemeral GPU-pod projects with Kopia or AzCopy.",
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
    configure.add_argument("--engine", choices=SUPPORTED_ENGINES, default=None)

    save = commands.add_parser("save", help="save a project, or preview it with --dry-run")
    save.add_argument("target", help="configured project name or source path")
    save.add_argument("--name", help="stable project name when target is a path")
    save.add_argument("--description", default="")
    save.add_argument(
        "--engine",
        choices=SUPPORTED_ENGINES,
        default=None,
        help="storage engine for a new project (default: kopia)",
    )
    save.add_argument("--dry-run", action="store_true")
    save.add_argument(
        "--no-progress", action="store_true", help="disable live transfer progress output"
    )

    listing = commands.add_parser("list", help="list Podvault snapshots")
    listing.add_argument("project", nargs="?")
    listing.add_argument("--engine", choices=SUPPORTED_ENGINES, default=None)

    restore = commands.add_parser("restore", help="restore a project")
    restore.add_argument("project")
    restore.add_argument("--engine", choices=SUPPORTED_ENGINES, default=None)
    restore_selection = restore.add_mutually_exclusive_group()
    restore_selection.add_argument("--latest", action="store_true", help="restore latest snapshot (default)")
    restore_selection.add_argument("--snapshot", help="stable Podvault or current Kopia snapshot ID")
    restore.add_argument("--to", dest="destination")
    restore.add_argument(
        "--parallel",
        type=_positive_int,
        default=None,
        help="Kopia restore parallelism (default: auto, up to 32)",
    )
    restore.add_argument(
        "--durable",
        action="store_true",
        help="Kopia: flush every file and use per-file atomic writes (slower)",
    )
    restore.add_argument(
        "--no-progress", action="store_true", help="disable live transfer progress output"
    )

    verify = commands.add_parser("verify", help="verify a snapshot")
    verify.add_argument("project")
    verify.add_argument("--engine", choices=SUPPORTED_ENGINES, default=None)
    verify_selection = verify.add_mutually_exclusive_group()
    verify_selection.add_argument("--latest", action="store_true", help="verify latest snapshot (default)")
    verify_selection.add_argument("--snapshot")
    verify.add_argument("--sample-percent", type=float, default=0.0)
    verify.add_argument(
        "--no-progress", action="store_true", help="disable live verification progress output"
    )

    pin = commands.add_parser("pin", help="label and retain a snapshot")
    pin.add_argument("project")
    pin.add_argument("--engine", choices=SUPPORTED_ENGINES, default=None)
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
        return project, source

    def _azure_context(self, write: bool, azcopy: bool = False) -> Tuple[AzureSAS, AzureBlobClient, ProjectCatalog]:
        value = self.credentials.require_sas_url(prompt=True)
        sas = parse_sas_url(value)
        if azcopy:
            sas.require_azcopy_permissions(write=write)
        else:
            sas.require_permissions(write=write)
        configured = self.config.repository
        if configured and configured.get("provider") == "azure":
            if (
                configured.get("storage_account") != sas.storage_account
                or configured.get("container") != sas.container
            ):
                raise ConfigurationError(
                    "SAS URL points to a different account or container than Podvault configuration"
                )
        elif not configured:
            self.config.set_repository(sas.repository_config())
        blobs = AzureBlobClient(sas)
        return sas, blobs, ProjectCatalog(blobs)

    def _remote_project_record(self, project: str) -> Optional[Dict[str, Any]]:
        if self.config.repository.get("provider") == "filesystem":
            return None
        if not self.credentials.sas_url():
            return None
        _, _, catalog = self._azure_context(write=False)
        return catalog.get(project)

    def _resolve_engine(self, project: str, requested: Optional[str]) -> str:
        configured = self.config.get_project(project)
        # Configs written before engine support are known Kopia projects.
        local_engine = configured.get("engine") if configured else None
        if configured and not local_engine:
            local_engine = "kopia"
        record = self._remote_project_record(project)
        remote_engine = record.get("engine") if record else None
        selected = requested or local_engine or remote_engine or "kopia"
        for source, value in (("local configuration", local_engine), ("Azure project record", remote_engine)):
            if value and value != selected:
                raise ConfigurationError(
                    "project {} uses engine {} in {}; it cannot be opened as {}".format(
                        project, value, source, selected
                    )
                )
        return selected

    def _azcopy_service(self, write: bool) -> Tuple[AzCopyService, ProjectCatalog]:
        sas, blobs, catalog = self._azure_context(write=write, azcopy=True)
        runner = AzCopyRunner(
            self.paths.state_dir,
            known_secrets=[sas.url, sas.token],
        )
        runner.require_supported_version()
        return (
            AzCopyService(
                runner,
                sas,
                blobs,
                catalog,
                self.console,
                self.receipts,
                self.config,
            ),
            catalog,
        )

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
        engine = self._resolve_engine(project, self.args.engine)
        self.config.set_project(project, source, engine=engine)
        if engine == "kopia" and (self.config.repository or self.credentials.sas_url()):
            self.repository.ensure_connected(write=True, allow_create=False)
        payload = {
            "status": "configured",
            "project": project,
            "path": str(source),
            "engine": engine,
        }
        self.console.result(
            payload, ["Configured {} -> {} ({})".format(project, source, engine)]
        )
        return 0

    def _save(self) -> int:
        project, source = self._project_source(self.args.target, self.args.name)
        engine = self._resolve_engine(project, self.args.engine)
        self.config.set_project(project, source, engine=engine)
        if engine == "azcopy":
            service, _ = self._azcopy_service(write=True)
            if self.args.dry_run:
                payload = service.dry_run(source, project)
                self.console.result(
                    payload,
                    [
                        "AzCopy dry run: {} files, {} bytes".format(
                            payload["summary"]["files"], payload["summary"]["size"]
                        ),
                        payload["note"],
                    ],
                )
                return 0
            payload = service.save(
                source,
                project,
                self.args.description,
                show_progress=not self.args.no_progress,
            )
        else:
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
            payload["engine"] = "kopia"
            # This record makes the fixed engine explicit for new releases.
            # Legacy read/list SAS values may lack create permission, so the
            # already-valid Kopia save remains usable if this hint cannot be written.
            if self.credentials.sas_url():
                try:
                    _, _, catalog = self._azure_context(write=True)
                    catalog.commit(project, "kopia")
                except PodvaultError as exc:
                    self.console.warning(
                        "snapshot is safe, but the optional remote engine record was not written: {}".format(exc)
                    )
        for item in payload.get("warnings", []):
            self.console.warning("recently changing or incomplete-looking file: {}".format(item))
        human = [
            "Snapshot: {} ({})".format(project, engine),
            "Podvault snapshot ID: {}".format(payload["podvault_snapshot_id"]),
            *(
                [
                    "Kopia manifest: {}".format(payload["kopia_manifest_id"]),
                    "Structural verification: PASSED",
                ]
                if engine == "kopia"
                else ["AzCopy transfer and manifest commit: PASSED"]
            ),
            "TRAINING QUIESCENCE: NOT ESTABLISHED BY PODVAULT",
            "SAFE TO TERMINATE: YES",
            "Receipt: {}".format(payload["receipt_path"]),
        ]
        self.console.result(payload, human)
        return 0

    def _list(self) -> int:
        project = validate_project_name(self.args.project) if self.args.project else None
        values: List[Dict[str, Any]] = []
        if project:
            engine = self._resolve_engine(project, self.args.engine)
            if engine == "azcopy":
                service, _ = self._azcopy_service(write=False)
                values = service.list(project)
            else:
                runner = self.repository.ensure_connected(write=False, allow_create=False)
                values = [snapshot_view(item) for item in list_snapshots(runner, project)]
        else:
            if self.args.engine in (None, "azcopy") and self.credentials.sas_url():
                _, _, catalog = self._azure_context(write=False, azcopy=True)
                records = [
                    record
                    for record in catalog.list_records()
                    if record.get("engine") == "azcopy"
                ]
                if records:
                    service, _ = self._azcopy_service(write=False)
                for record in records:
                    if record.get("engine") == "azcopy":
                        values.extend(service.list(str(record["project"])))
            should_list_kopia = self.args.engine in (None, "kopia") and (
                self.credentials.repository_password()
                or self.config.repository.get("provider") == "filesystem"
            )
            if should_list_kopia:
                runner = self.repository.ensure_connected(write=False, allow_create=False)
                values.extend(snapshot_view(item) for item in list_snapshots(runner, None))
        values.sort(key=lambda item: str(item.get("end_time") or ""), reverse=True)
        payload = {"snapshots": values, "count": len(values)}
        human = []
        for item in values:
            summary = item.get("summary") or {}
            human.append(
                "{}  {}  {}  {}  {} files  {}".format(
                    (item.get("podvault_snapshot_id") or "-")[:12],
                    item.get("end_time") or "-",
                    item.get("project") or "-",
                    item.get("engine") or "kopia",
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
        engine = self._resolve_engine(project, self.args.engine)
        if engine == "azcopy":
            if self.args.parallel is not None or self.args.durable:
                raise ConfigurationError(
                    "--parallel and --durable apply only to Kopia projects; tune AzCopy with AZCOPY_CONCURRENCY_VALUE"
                )
            service, _ = self._azcopy_service(write=False)
            payload = service.restore(
                project,
                self.args.snapshot,
                self.args.destination,
                show_progress=not self.args.no_progress,
            )
        else:
            runner = self.repository.ensure_connected(write=False, allow_create=False)
            service = RestoreService(runner, self.console, self.receipts, self.config)
            parallel = self.args.parallel or min(32, max(8, (os.cpu_count() or 8) * 2))
            payload = service.restore(
                project,
                self.args.snapshot,
                self.args.destination,
                show_progress=not self.args.no_progress,
                parallel=parallel,
                durable=self.args.durable,
            )
        human = [
            "Restored {} to {}".format(project, payload["destination"]),
            "Engine: {}".format(engine),
            "Structural verification: PASSED",
            "Restored tree verification: PASSED",
            "Receipt: {}".format(payload["receipt_path"]),
        ]
        self.console.result(payload, human)
        return 0

    def _verify(self) -> int:
        project = validate_project_name(self.args.project)
        engine = self._resolve_engine(project, self.args.engine)
        if engine == "azcopy":
            service, _ = self._azcopy_service(write=False)
            payload = service.verify(project, self.args.snapshot, self.args.sample_percent)
        else:
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
        engine = self._resolve_engine(project, self.args.engine)
        if engine == "azcopy":
            raise ConfigurationError(
                "pin is a Kopia-only operation; AzCopy generations are immutable and retained until manually removed"
            )
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
        project_engines = {
            (value or {}).get("engine") or "kopia"
            for value in self.config.data.get("projects", {}).values()
        }
        try:
            runner = KopiaRunner(self.paths.kopia_config_file)
            version = runner.require_supported_version()
            checks.append({"name": "kopia", "status": "ok", "version": ".".join(map(str, version))})
        except PodvaultError as exc:
            required = "kopia" in project_engines
            healthy = healthy and not required
            checks.append(
                {
                    "name": "kopia",
                    "status": "error" if required else "not-present",
                    "detail": str(exc),
                }
            )
        try:
            runner = AzCopyRunner(self.paths.state_dir)
            version = runner.require_supported_version()
            checks.append(
                {"name": "azcopy", "status": "ok", "version": ".".join(map(str, version))}
            )
        except PodvaultError as exc:
            required = "azcopy" in project_engines
            healthy = healthy and not required
            checks.append(
                {
                    "name": "azcopy",
                    "status": "error" if required else "not-present",
                    "detail": str(exc),
                }
            )
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
        should_check_repository = bool(
            self.credentials.repository_password()
            or self.config.repository.get("provider") == "filesystem"
            or "kopia" in project_engines
        )
        if should_check_repository:
            try:
                connected = self.repository.ensure_connected(write=False, allow_create=False)
                checks.append(
                    {"name": "repository", "status": "ok", **self.repository.status_payload(connected)}
                )
            except PodvaultError as exc:
                healthy = False
                checks.append({"name": "repository", "status": "error", "detail": str(exc)})
        else:
            checks.append(
                {
                    "name": "repository",
                    "status": "not-needed",
                    "detail": "no locally configured Kopia projects",
                }
            )
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
