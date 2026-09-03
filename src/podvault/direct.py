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
from .paths import validate_destination
from .project import risky_files
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
    ) -> Dict[str, Any]:
        operation_started = time.monotonic()
        record = self.catalog.get(project)
        if not record or record.get("engine") != "azcopy":
            raise ConfigurationError("AzCopy project record not found: {}".format(project))
        selected_id = record.get("current_snapshot")
        if identifier:
            selected = select_direct_snapshot(self.list(project), identifier)
            selected_id = selected.get("podvault_snapshot_id")
            manifest = selected
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
            "started_at": utc_now(),
        }
        atomic_json_write(marker, state, 0o600)
        self.console.info("Restoring {} with AzCopy into a temporary directory...".format(project))
        try:
            transfer_started = time.monotonic()
            data_prefix = str(manifest.get("data_prefix") or "")
            expected_prefix = snapshot_prefix(project, str(selected_id)) + "/data"
            if data_prefix != expected_prefix:
                raise VerificationError("AzCopy manifest contains an unexpected data path")
            self.runner.download_tree(
                self.sas.blob_url(data_prefix),
                staging,
                show_progress=show_progress,
            )
            transfer_seconds = time.monotonic() - transfer_started
            scan_started = time.monotonic()
            actual = local_tree_summary(staging)
            compare_summary(manifest.get("summary") or {}, actual)
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
        self.config.set_project(project, destination, engine="azcopy")
        receipt = {
            "status": "success",
            "engine": "azcopy",
            "destination": str(destination),
            "podvault_snapshot_id": selected_id,
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
