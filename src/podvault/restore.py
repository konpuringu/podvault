"""Non-destructive, staged project restoration."""

import os
import stat
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .browse import resolve_kopia_directory
from .config import ConfigStore, atomic_json_write
from .errors import ConfigurationError, DestinationConflictError, VerificationError
from .kopia import KopiaRunner
from .output import Console
from .paths import validate_destination, validate_relative_path
from .receipts import ReceiptStore, utc_now
from .snapshots import _tag, list_snapshots, select_snapshot, verify_manifest


def local_tree_summary(root: Path) -> Dict[str, int]:
    summary = {"size": 0, "files": 0, "symlinks": 0, "dirs": 1}
    for current, directories, files in os.walk(str(root), followlinks=False):
        retained_dirs = []
        for name in directories:
            path = Path(current, name)
            if path.is_symlink():
                summary["symlinks"] += 1
            else:
                summary["dirs"] += 1
                retained_dirs.append(name)
        directories[:] = retained_dirs
        for name in files:
            path = Path(current, name)
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                raise VerificationError("unable to inspect restored path: {}".format(path)) from exc
            if stat.S_ISLNK(mode):
                summary["symlinks"] += 1
            elif stat.S_ISREG(mode):
                summary["files"] += 1
                summary["size"] += path.lstat().st_size
            else:
                raise VerificationError("restored tree contains unsupported file type: {}".format(path))
    return summary


def compare_summary(expected: Dict[str, Any], actual: Dict[str, int]) -> None:
    differences = []
    for key in ("size", "files", "symlinks", "dirs"):
        if key in expected and int(expected.get(key, 0)) != actual[key]:
            differences.append("{} expected {}, got {}".format(key, expected.get(key), actual[key]))
    if differences:
        raise VerificationError("restored tree verification failed: " + "; ".join(differences))


def promote_staging(staging: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise DestinationConflictError(
            "destination became a symbolic link during restore: {}".format(destination)
        )
    if destination.exists():
        if not destination.is_dir():
            raise DestinationConflictError(
                "destination became a non-directory during restore: {}".format(destination)
            )
        try:
            next(destination.iterdir())
        except StopIteration:
            pass
        else:
            raise DestinationConflictError(
                "destination became nonempty during restore: {}".format(destination)
            )
    os.replace(str(staging), str(destination))


class RestoreService:
    def __init__(
        self,
        runner: KopiaRunner,
        console: Console,
        receipts: ReceiptStore,
        config: ConfigStore,
    ):
        self.runner = runner
        self.console = console
        self.receipts = receipts
        self.config = config

    def restore(
        self,
        project: str,
        identifier: Optional[str],
        destination_value: Optional[str],
        show_progress: bool = True,
        parallel: int = 32,
        durable: bool = False,
        relative_path: Optional[str] = None,
        preserve_owners: bool = False,
    ) -> Dict[str, Any]:
        operation_started = time.monotonic()
        selected_path = validate_relative_path(relative_path)
        if selected_path and not destination_value:
            raise ConfigurationError("--path requires an explicit --to destination")
        snapshot = select_snapshot(list_snapshots(self.runner, project), identifier)
        root_entry = snapshot.get("rootEntry") or {}
        root_object_id = root_entry.get("obj")
        if not root_object_id:
            raise VerificationError("snapshot does not contain a root object ID")
        self.console.info("Verifying selected snapshot...")
        verify_started = time.monotonic()
        verification = verify_manifest(
            self.runner, snapshot["id"], 0, show_progress=show_progress
        )
        verify_seconds = time.monotonic() - verify_started
        restore_object_id = str(root_object_id)
        restore_source = restore_object_id
        expected_summary = root_entry.get("summ") or {}
        if selected_path:
            self.console.info("Resolving snapshot directory {}...".format(selected_path))
            selected = resolve_kopia_directory(
                self.runner, restore_object_id, selected_path
            )
            restore_object_id = str(selected["object_id"])
            restore_source = str(root_object_id).rstrip("/") + "/" + selected_path
            expected_summary = selected["summary"]
        configured = self.config.get_project(project)
        default_destination = configured.get("path") if configured else "/workspace/{}".format(project)
        destination = validate_destination(destination_value or default_destination)
        staging = destination.parent / ".{}.podvault-restore-{}".format(
            destination.name, uuid.uuid4().hex
        )
        marker = destination.parent / ".{}.podvault-restore-state-{}.json".format(
            destination.name, uuid.uuid4().hex
        )
        state = {
            "schema_version": 1,
            "status": "restoring",
            "project": project,
            "destination": str(destination),
            "staging": str(staging),
            "kopia_manifest_id": snapshot["id"],
            "kopia_root_object_id": root_object_id,
            "restore_object_id": restore_object_id,
            "restore_source": restore_source,
            "path": selected_path or ".",
            "selective": bool(selected_path),
            "started_at": utc_now(),
        }
        atomic_json_write(marker, state, 0o600)
        selection = ":{}".format(selected_path) if selected_path else ""
        self.console.info(
            "Restoring {}{} into a temporary directory...".format(project, selection)
        )
        try:
            transfer_started = time.monotonic()
            args = [
                "--progress" if show_progress else "--no-progress",
                "snapshot",
                "restore",
                restore_source,
                str(staging),
                "--no-overwrite-files",
                "--no-overwrite-directories",
                "--no-overwrite-symlinks",
                "--parallel={}".format(parallel),
                "--no-ignore-permission-errors",
            ]
            if not preserve_owners:
                # Project snapshots commonly move from a root-run GPU pod to
                # an unprivileged cluster account. Preserve modes and times,
                # but make the restored tree belong to the invoking user.
                args.append("--skip-owners")
            if durable:
                args.extend(["--write-files-atomically", "--flush-files"])
            if show_progress:
                self.runner.run_streaming(args)
            else:
                self.runner.run(args)
            transfer_seconds = time.monotonic() - transfer_started
            scan_started = time.monotonic()
            actual = local_tree_summary(staging)
            compare_summary(expected_summary, actual)
            scan_seconds = time.monotonic() - scan_started
            promote_staging(staging, destination)
        except BaseException:
            state["status"] = "interrupted-or-failed"
            state["updated_at"] = utc_now()
            atomic_json_write(marker, state, 0o600)
            raise
        try:
            marker.unlink()
        except OSError:
            pass
        if not selected_path:
            self.config.set_project(project, destination, engine="kopia")
        total_seconds = time.monotonic() - operation_started
        receipt = {
            "status": "success",
            "engine": "kopia",
            "destination": str(destination),
            "podvault_snapshot_id": _tag(snapshot, "podvault.snapshot"),
            "kopia_manifest_id": snapshot["id"],
            "kopia_root_object_id": root_object_id,
            "restore_object_id": restore_object_id,
            "restore_source": restore_source,
            "path": selected_path or ".",
            "selective": bool(selected_path),
            "repository_verification": verification,
            "restored_summary": actual,
            "restore_parallelism": parallel,
            "durable_restore": durable,
            "ownership_restore": "original" if preserve_owners else "current-user",
            "timings_seconds": {
                "verification": round(verify_seconds, 3),
                "transfer": round(transfer_seconds, 3),
                "tree_scan": round(scan_seconds, 3),
                "total": round(total_seconds, 3),
            },
        }
        receipt_path = self.receipts.write(project, "restore", receipt)
        receipt["receipt_path"] = str(receipt_path)
        return receipt
