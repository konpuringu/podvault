"""Generation-based direct project storage implemented with AzCopy."""

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .azure import AzureBlobClient, AzureSAS
from .azcopy import AzCopyRunner
from .catalog import ProjectCatalog
from .config import ConfigStore, atomic_json_write
from .errors import ConfigurationError, VerificationError
from .output import Console
from .paths import validate_destination, validate_relative_path
from .project import risky_files, validate_project_name
from .receipts import ReceiptStore, utc_now
from .restore import compare_summary, local_tree_summary, promote_staging


AZCOPY_PREFIX = ".podvault/azcopy/v1/projects"


def snapshot_prefix(project: str, snapshot_id: str) -> str:
    return "{}/{}/snapshots/{}".format(AZCOPY_PREFIX, project, snapshot_id)


def manifest_path(project: str, snapshot_id: str) -> str:
    return snapshot_prefix(project, snapshot_id) + "/manifest.json"


def select_direct_snapshot(
    snapshots: List[Dict[str, Any]], identifier: Optional[str]
) -> Dict[str, Any]:
    if not snapshots:
        raise ConfigurationError("no AzCopy snapshots found for project")
    ordered = sorted(snapshots, key=lambda item: str(item.get("end_time") or ""), reverse=True)
    if not identifier:
        return ordered[0]
    exact = [item for item in ordered if item.get("podvault_snapshot_id") == identifier]
    if exact:
        return exact[0]
    matches = [
        item
        for item in ordered
        if str(item.get("podvault_snapshot_id") or "").startswith(identifier)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigurationError("snapshot identifier is ambiguous")
    raise ConfigurationError("snapshot not found: {}".format(identifier))


def _blob_type(item: Dict[str, Any]) -> str:
    if item.get("type") == "directory":
        return "directory"
    metadata = {
        str(key).lower(): str(value).lower()
        for key, value in (item.get("metadata") or {}).items()
    }
    if metadata.get("hdi_isfolder") == "true":
        return "directory"
    if metadata.get("is_symlink") == "true":
        return "symlink"
    return "file"


def _join_prefix(prefix: str, relative_path: str) -> str:
    return prefix.rstrip("/") + ("/" + relative_path if relative_path else "")


class AzCopyService:
    def __init__(
        self,
        runner: AzCopyRunner,
        sas: AzureSAS,
        blobs: AzureBlobClient,
        catalog: ProjectCatalog,
        console: Console,
        receipts: ReceiptStore,
        config: ConfigStore,
    ):
        self.runner = runner
        self.sas = sas
        self.blobs = blobs
        self.catalog = catalog
        self.console = console
        self.receipts = receipts
        self.config = config

    def list(self, project: str) -> List[Dict[str, Any]]:
        prefix = "{}/{}/snapshots/".format(AZCOPY_PREFIX, project)
        result = []
        for item in self.blobs.list_blobs(prefix):
            name = str(item.get("name") or "")
            if not name.endswith("/manifest.json"):
                continue
            value = self.blobs.get_json(name)
            if value and value.get("format") == "podvault-azcopy-v1":
                result.append(value)
        return sorted(result, key=lambda value: str(value.get("end_time") or ""), reverse=True)

    def _select_manifest(
        self, project: str, identifier: Optional[str]
    ) -> tuple[str, Dict[str, Any]]:
        record = self.catalog.get(project)
        if not record or record.get("engine") != "azcopy":
            raise ConfigurationError("AzCopy project record not found: {}".format(project))
        selected_id = record.get("current_snapshot")
        if identifier:
            manifest = select_direct_snapshot(self.list(project), identifier)
            selected_id = manifest.get("podvault_snapshot_id")
        elif selected_id:
            manifest = self.blobs.get_json(manifest_path(project, str(selected_id)))
        else:
            manifest = None
        if not selected_id:
            raise ConfigurationError("AzCopy project has no committed snapshot")
        if not manifest:
            raise VerificationError("AzCopy snapshot manifest is missing")
        if manifest.get("project") != project or manifest.get("format") != "podvault-azcopy-v1":
            raise VerificationError("AzCopy snapshot manifest is invalid")
        expected_prefix = snapshot_prefix(project, str(selected_id)) + "/data"
        if manifest.get("data_prefix") != expected_prefix:
            raise VerificationError("AzCopy manifest contains an unexpected data path")
        return str(selected_id), manifest

    def _require_directory(self, data_prefix: str, relative_path: str) -> None:
        if not relative_path:
            return
        selected_prefix = _join_prefix(data_prefix, relative_path)
        listing = self.blobs.list_blobs(
            selected_prefix, include_metadata=True, delimiter="/"
        )
        exact = [
            item
            for item in listing
            if str(item.get("name") or "") == selected_prefix
        ]
        if exact:
            if _blob_type(exact[0]) != "directory":
                raise ConfigurationError(
                    "snapshot path is not a directory: {}".format(relative_path)
                )
            return
        if any(
            str(item.get("name") or "").rstrip("/") == selected_prefix
            and _blob_type(item) == "directory"
            for item in listing
        ):
            return
        children = self.blobs.list_blobs(
            selected_prefix + "/", include_metadata=True, delimiter="/"
        )
        if not children:
            raise ConfigurationError(
                "snapshot directory not found: {}".format(relative_path)
            )

    def _tree_entries(
        self, data_prefix: str, relative_path: str, recursive: bool
    ) -> List[Dict[str, Any]]:
        self._require_directory(data_prefix, relative_path)
        selected_prefix = _join_prefix(data_prefix, relative_path)
        child_prefix = selected_prefix.rstrip("/") + "/"
        blobs = self.blobs.list_blobs(
            child_prefix,
            include_metadata=True,
            delimiter=None if recursive else "/",
        )
        data_root = data_prefix.rstrip("/") + "/"
        selected_root = relative_path.rstrip("/")
        entries: Dict[str, Dict[str, Any]] = {}
        for item in blobs:
            name = str(item.get("name") or "")
            if not name.startswith(data_root):
                raise VerificationError("Azure returned a blob outside the snapshot data path")
            path = name[len(data_root) :]
            kind = _blob_type(item)
            if kind == "directory":
                path = path.rstrip("/")
            if not path or path == selected_root:
                continue
            entries[path] = {
                "path": path,
                "type": kind,
                "size": item.get("size"),
                "mtime": item.get("last_modified"),
            }
            if recursive:
                components = path.split("/")
                for index in range(1, len(components)):
                    parent = "/".join(components[:index])
                    if parent == selected_root or (
                        selected_root and not parent.startswith(selected_root + "/")
                    ):
                        continue
                    entries.setdefault(
                        parent,
                        {"path": parent, "type": "directory", "size": None, "mtime": None},
                    )
        return [entries[key] for key in sorted(entries)]

    def _remote_summary(self, data_prefix: str, relative_path: str) -> Dict[str, int]:
        entries = self._tree_entries(data_prefix, relative_path, recursive=True)
        return {
            "size": sum(
                int(item.get("size") or 0)
                for item in entries
                if item.get("type") == "file"
            ),
            "files": sum(item.get("type") == "file" for item in entries),
            "symlinks": sum(item.get("type") == "symlink" for item in entries),
            "dirs": 1 + sum(item.get("type") == "directory" for item in entries),
        }

    def tree(
        self,
        project: str,
        identifier: Optional[str],
        relative_path: Optional[str],
        recursive: bool,
    ) -> Dict[str, Any]:
        selected_path = validate_relative_path(relative_path)
        selected_id, manifest = self._select_manifest(project, identifier)
        entries = self._tree_entries(
            str(manifest["data_prefix"]), selected_path, recursive
        )
        return {
            "status": "success",
            "operation": "tree",
            "engine": "azcopy",
            "project": project,
            "podvault_snapshot_id": selected_id,
            "path": selected_path or ".",
            "recursive": recursive,
            "entries": entries,
            "count": len(entries),
        }

    def dry_run(self, source: Path, project: str) -> Dict[str, Any]:
        self.console.info("Scanning {} ({})...".format(project, source))
        summary = local_tree_summary(source)
        return {
            "operation": "dry-run",
            "engine": "azcopy",
            "project": project,
            "source": str(source),
            "summary": summary,
            "note": "AzCopy mode transfers the complete tree as a new immutable generation.",
        }

    def save(
        self,
        source: Path,
        project: str,
        description: str,
        show_progress: bool,
    ) -> Dict[str, Any]:
        operation_started = time.monotonic()
        snapshot_id = uuid.uuid4().hex
        started_at = utc_now()
        summary: Dict[str, int] = {}
        warnings: List[str] = []
        try:
            self.console.info("Scanning {} before direct upload...".format(project))
            scan_started = time.monotonic()
            summary = local_tree_summary(source)
            warnings = risky_files(source)
            scan_seconds = time.monotonic() - scan_started
            version = ".".join(map(str, self.runner.require_supported_version()))
            data_prefix = snapshot_prefix(project, snapshot_id) + "/data"
            self.console.info("Uploading {} with AzCopy...".format(project))
            transfer_started = time.monotonic()
            self.runner.upload_tree(
                source,
                self.sas.blob_url(data_prefix),
                show_progress=show_progress,
            )
            transfer_seconds = time.monotonic() - transfer_started
            post_scan_started = time.monotonic()
            after_upload = local_tree_summary(source)
            compare_summary(summary, after_upload)
            scan_seconds += time.monotonic() - post_scan_started
            manifest = {
                "schema_version": 1,
                "format": "podvault-azcopy-v1",
                "engine": "azcopy",
                "project": project,
                "podvault_snapshot_id": snapshot_id,
                "podvault_version": __version__,
                "azcopy_version": version,
                "source": str(source),
                "description": description,
                "start_time": started_at,
                "end_time": utc_now(),
                "data_prefix": data_prefix,
                "summary": summary,
                "warnings": warnings,
            }
            # The manifest and project record are committed only after AzCopy
            # reports success. The record is the atomic pointer to the new version.
            self.blobs.put_json(manifest_path(project, snapshot_id), manifest)
            self.catalog.commit(project, "azcopy", current_snapshot=snapshot_id)
        except Exception as exc:
            failure = {
                "status": "failed",
                "safe_to_terminate": False,
                "engine": "azcopy",
                "source": str(source),
                "podvault_snapshot_id": snapshot_id,
                "summary": summary,
                "warnings": warnings,
                "error": str(exc),
            }
            receipt_path = self.receipts.write(project, "save", failure)
            setattr(exc, "receipt_path", receipt_path)
            raise
        self.config.set_project(project, source, engine="azcopy")
        receipt = {
            "status": "success",
            "safe_to_terminate": True,
            "application_quiescence_established": False,
            "engine": "azcopy",
            "source": str(source),
            "description": description,
            "podvault_snapshot_id": snapshot_id,
            "azcopy_version": version,
            "summary": summary,
            "warnings": warnings,
            "timings_seconds": {
                "tree_scan": round(scan_seconds, 3),
                "transfer": round(transfer_seconds, 3),
                "total": round(time.monotonic() - operation_started, 3),
            },
        }
        receipt_path = self.receipts.write(project, "save", receipt)
        receipt["receipt_path"] = str(receipt_path)
        return receipt

    def restore(
        self,
        project: str,
        identifier: Optional[str],
        destination_value: Optional[str],
        show_progress: bool,
        relative_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        operation_started = time.monotonic()
        selected_path = validate_relative_path(relative_path)
        if selected_path and not destination_value:
            raise ConfigurationError("--path requires an explicit --to destination")
        selected_id, manifest = self._select_manifest(project, identifier)
        data_prefix = str(manifest["data_prefix"])
        expected_summary = manifest.get("summary") or {}
        if selected_path:
            self.console.info("Inspecting remote directory {}...".format(selected_path))
            expected_summary = self._remote_summary(data_prefix, selected_path)
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
            "engine": "azcopy",
            "project": project,
            "destination": str(destination),
            "staging": str(staging),
            "podvault_snapshot_id": selected_id,
            "path": selected_path or ".",
            "selective": bool(selected_path),
            "started_at": utc_now(),
        }
        atomic_json_write(marker, state, 0o600)
        selection = ":{}".format(selected_path) if selected_path else ""
        self.console.info(
            "Restoring {}{} with AzCopy into a temporary directory...".format(
                project, selection
            )
        )
        try:
            transfer_started = time.monotonic()
            self.runner.download_tree(
                self.sas.blob_url(_join_prefix(data_prefix, selected_path)),
                staging,
                show_progress=show_progress,
            )
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
            self.config.set_project(project, destination, engine="azcopy")
        receipt = {
            "status": "success",
            "engine": "azcopy",
            "destination": str(destination),
            "podvault_snapshot_id": selected_id,
            "path": selected_path or ".",
            "selective": bool(selected_path),
            "restored_summary": actual,
            "content_md5_validation": "AzCopy FailIfDifferent",
            "timings_seconds": {
                "transfer": round(transfer_seconds, 3),
                "tree_scan": round(scan_seconds, 3),
                "total": round(time.monotonic() - operation_started, 3),
            },
        }
        receipt_path = self.receipts.write(project, "restore", receipt)
        receipt["receipt_path"] = str(receipt_path)
        return receipt

    def verify(self, project: str, identifier: Optional[str], sample_percent: float) -> Dict[str, Any]:
        if sample_percent:
            raise ConfigurationError(
                "AzCopy projects validate Content-MD5 during restore; sampled remote verification is unavailable"
            )
        manifest = select_direct_snapshot(self.list(project), identifier)
        data_prefix = str(manifest.get("data_prefix") or "") + "/"
        blobs = self.blobs.list_blobs(data_prefix)
        if not blobs and int((manifest.get("summary") or {}).get("files", 0)):
            raise VerificationError("AzCopy snapshot data is missing")
        return {
            "status": "success",
            "engine": "azcopy",
            "project": project,
            "podvault_snapshot_id": manifest.get("podvault_snapshot_id"),
            "verification": {
                "mode": "structural",
                "manifest": "readable",
                "remote_blob_count": len(blobs),
                "verified_at": utc_now(),
            },
        }

    def delete_project(self, project: str, show_progress: bool) -> Dict[str, Any]:
        project = validate_project_name(project)
        record = self.catalog.get(project)
        prefix = "{}/{}".format(AZCOPY_PREFIX, project)
        generation_count = len(self.list(project))
        if record and record.get("engine") != "azcopy":
            raise ConfigurationError(
                "project {} is not stored with AzCopy".format(project)
            )
        if not record and not self.blobs.list_blobs(prefix + "/"):
            raise ConfigurationError("AzCopy project not found: {}".format(project))

        self.console.info(
            "Deleting every stored generation of {} with AzCopy...".format(project)
        )
        self.runner.delete_tree(
            self.sas.blob_url(prefix), show_progress=show_progress
        )
        remaining = self.blobs.list_blobs(prefix + "/")
        if remaining:
            raise VerificationError(
                "AzCopy deletion left {} blob(s) under the project prefix".format(
                    len(remaining)
                )
            )
        catalog_record_deleted = self.catalog.delete(project)
        return {
            "status": "success",
            "operation": "delete",
            "engine": "azcopy",
            "project": project,
            "deleted_generations": "all",
            "deleted_generation_count": generation_count,
            "remaining_snapshot_count": 0,
            "project_deleted": True,
            "catalog_record_deleted": catalog_record_deleted,
        }

    def delete_snapshots(
        self,
        project: str,
        snapshots: List[Dict[str, Any]],
        show_progress: bool,
    ) -> Dict[str, Any]:
        project = validate_project_name(project)
        if not snapshots:
            raise ConfigurationError("no AzCopy generations selected for deletion")
        available = self.list(project)
        available_by_id = {
            str(item.get("podvault_snapshot_id") or ""): item for item in available
        }
        selected_ids = [
            str(item.get("podvault_snapshot_id") or "") for item in snapshots
        ]
        if any(not value or value not in available_by_id for value in selected_ids):
            raise ConfigurationError("selected AzCopy generation is no longer available")
        selected_set = set(selected_ids)
        remaining = [
            item
            for item in available
            if str(item.get("podvault_snapshot_id") or "") not in selected_set
        ]
        record = self.catalog.get(project)
        current = str((record or {}).get("current_snapshot") or "")
        next_current: Optional[str] = current or None
        if current in selected_set and remaining:
            # Repoint before deleting the current generation. If deletion is
            # interrupted, the project still resolves to a complete version.
            self.sas.require_delete_permissions(repository_write=True)
            newest = select_direct_snapshot(remaining, None)
            next_current = str(newest["podvault_snapshot_id"])
            self.catalog.commit(project, "azcopy", current_snapshot=next_current)

        for index, snapshot_id in enumerate(selected_ids, start=1):
            self.console.info(
                "Deleting AzCopy generation {}/{}...".format(index, len(selected_ids))
            )
            prefix = snapshot_prefix(project, snapshot_id)
            self.runner.delete_tree(
                self.sas.blob_url(prefix), show_progress=show_progress
            )
            leftovers = self.blobs.list_blobs(prefix + "/")
            if leftovers:
                raise VerificationError(
                    "AzCopy deletion left {} blob(s) in generation {}".format(
                        len(leftovers), snapshot_id
                    )
                )

        catalog_record_deleted = False
        if not remaining:
            catalog_record_deleted = self.catalog.delete(project)
            next_current = None
        return {
            "status": "success",
            "operation": "delete",
            "engine": "azcopy",
            "project": project,
            "deleted_generation_count": len(selected_ids),
            "deleted_podvault_snapshot_ids": selected_ids,
            "remaining_snapshot_count": len(remaining),
            "project_deleted": not remaining,
            "current_snapshot": next_current,
            "catalog_record_deleted": catalog_record_deleted,
        }
