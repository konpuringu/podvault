"""Snapshot listing, selection, saving, verification, and pins."""

import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import __version__
from .errors import ConfigurationError, KopiaCommandError, VerificationError
from .kopia import KopiaRunner, parse_json_document, parse_json_lines
from .output import Console
from .project import canonical_source, risky_files, snapshot_tags, validate_project_name
from .receipts import ReceiptStore, utc_now


def _tag(snapshot: Dict[str, Any], name: str) -> Optional[str]:
    tags = snapshot.get("tags") or {}
    return tags.get("tag:" + name) or tags.get(name)


def list_snapshots(
    runner: KopiaRunner,
    project: Optional[str] = None,
    include_incomplete: bool = False,
) -> List[Dict[str, Any]]:
    args = [
        "--no-progress",
        "snapshot",
        "list",
        "--all",
        "--show-identical",
        "--json",
        "--tags=podvault.schema:1",
    ]
    if include_incomplete:
        args.append("--incomplete")
    if project:
        args.append("--tags=podvault.project:{}".format(validate_project_name(project)))
    result = runner.run(args)
    value = parse_json_document(result.stdout)
    if not isinstance(value, list):
        raise KopiaCommandError("unexpected Kopia snapshot list output")
    return [item for item in value if isinstance(item, dict)]


def _sort_time(snapshot: Dict[str, Any]) -> str:
    return str(snapshot.get("endTime") or snapshot.get("startTime") or "")


def select_snapshot(
    snapshots: Iterable[Dict[str, Any]], identifier: Optional[str] = None
) -> Dict[str, Any]:
    values = list(snapshots)
    if not values:
        raise ConfigurationError("no snapshots found for project")
    if identifier is None:
        return sorted(values, key=_sort_time, reverse=True)[0]
    matches = []
    for item in values:
        manifest_id = str(item.get("id", ""))
        stable_id = str(_tag(item, "podvault.snapshot") or "")
        if manifest_id == identifier or stable_id == identifier:
            return item
        if manifest_id.startswith(identifier) or stable_id.startswith(identifier):
            matches.append(item)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigurationError("snapshot identifier is ambiguous")
    raise ConfigurationError("snapshot not found: {}".format(identifier))


def verify_manifest(
    runner: KopiaRunner,
    manifest_id: str,
    sample_percent: float = 0.0,
    show_progress: bool = True,
) -> Dict[str, Any]:
    if sample_percent < 0 or sample_percent > 100:
        raise ConfigurationError("sample percentage must be between 0 and 100")
    args = [
        "--progress" if show_progress else "--no-progress",
        "snapshot",
        "verify",
        manifest_id,
        "--verify-files-percent={}".format(sample_percent),
        "--json",
    ]
    result = runner.run_streaming(args) if show_progress else runner.run(args)
    values = parse_json_lines(result.stdout)
    final = values[-1] if values else {}
    if final.get("errorCount", 0) != 0:
        raise VerificationError(
            "Kopia reported {} verification error(s)".format(final.get("errorCount"))
        )
    return {
        "mode": "structural" if sample_percent == 0 else "sample-content",
        "sample_percent": sample_percent,
        "error_count": final.get("errorCount", 0),
        "stats": final.get("stats", final),
        "verified_at": utc_now(),
    }


class SnapshotService:
    def __init__(self, runner: KopiaRunner, console: Console, receipts: ReceiptStore):
        self.runner = runner
        self.console = console
        self.receipts = receipts

    def dry_run(self, source: Path, project: str, show_progress: bool = True) -> Dict[str, Any]:
        self.console.info("Dry run for {} ({})".format(project, source))
        args = [
            "--progress" if show_progress else "--no-progress",
            "snapshot",
            "estimate",
            str(source),
            "--show-files",
            "--max-examples-per-bucket=100",
        ]
        result = self.runner.run_streaming(args) if show_progress else self.runner.run(args)
        text = result.stdout.strip()
        return {"operation": "dry-run", "project": project, "source": str(source), "estimate": text}

    def save(
        self,
        source: Path,
        project: str,
        description: str = "",
        show_progress: bool = True,
    ) -> Dict[str, Any]:
        stable_id = uuid.uuid4().hex
        kopia_version = ".".join(str(part) for part in self.runner.version())
        tags = snapshot_tags(project, source, __version__, stable_id)
        tags["podvault.kopia-version"] = kopia_version
        self.console.info("Checking {} for recently changing files...".format(project))
        warnings = risky_files(source)
        self.console.info("Saving {} from {}...".format(project, source))
        args = [
            "--progress" if show_progress else "--no-progress",
            "snapshot",
            "create",
            str(source),
        ]
        args.extend(
            [
                "--override-source={}".format(canonical_source(project)),
                "--pin=podvault.retain-v1",
                "--force-disable-actions",
                "--no-send-snapshot-report",
                "--json",
            ]
        )
        if description:
            args.append("--description={}".format(description))
        for key, value in tags.items():
            args.append("--tags={}:{}".format(key, value))
        manifest = None
        try:
            result = self.runner.run_streaming(args) if show_progress else self.runner.run(args)
            manifest = parse_json_document(result.stdout)
            if not isinstance(manifest, dict) or not manifest.get("id"):
                raise KopiaCommandError("snapshot creation did not return a manifest ID")
            summary = (manifest.get("rootEntry") or {}).get("summ") or {}
            if summary.get("numFailed", 0):
                raise KopiaCommandError("snapshot contains failed entries")
            self.console.info("Verifying uploaded snapshot...")
            verification = verify_manifest(
                self.runner, manifest["id"], 0, show_progress=show_progress
            )
        except Exception as exc:
            receipt = {
                "status": "failed",
                "safe_to_terminate": False,
                "source": str(source),
                "podvault_snapshot_id": stable_id,
                "kopia_manifest_id": manifest.get("id") if isinstance(manifest, dict) else None,
                "error": str(exc),
                "warnings": warnings,
            }
            receipt_path = self.receipts.write(project, "save", receipt)
            setattr(exc, "receipt_path", receipt_path)
            raise
        root = manifest.get("rootEntry") or {}
        receipt = {
            "status": "success",
            "safe_to_terminate": True,
            "engine": "kopia",
            "application_quiescence_established": False,
            "source": str(source),
            "canonical_source": canonical_source(project),
            "description": description,
            "podvault_version": __version__,
            "kopia_version": kopia_version,
            "podvault_snapshot_id": stable_id,
            "kopia_manifest_id": manifest.get("id"),
            "kopia_root_object_id": root.get("obj"),
            "snapshot_start_time": manifest.get("startTime"),
            "snapshot_end_time": manifest.get("endTime"),
            "summary": root.get("summ", {}),
            "verification": verification,
            "warnings": warnings,
        }
        receipt_path = self.receipts.write(project, "save", receipt)
        receipt["receipt_path"] = str(receipt_path)
        return receipt

    def verify(
        self,
        project: str,
        identifier: Optional[str],
        sample_percent: float,
        show_progress: bool = True,
    ) -> Dict[str, Any]:
        snapshot = select_snapshot(list_snapshots(self.runner, project), identifier)
        self.console.info("Verifying {}...".format(project))
        verification = verify_manifest(
            self.runner, snapshot["id"], sample_percent, show_progress=show_progress
        )
        return {
            "status": "success",
            "engine": "kopia",
            "project": project,
            "podvault_snapshot_id": _tag(snapshot, "podvault.snapshot"),
            "kopia_manifest_id": snapshot["id"],
            "verification": verification,
        }

    def pin(self, project: str, identifier: Optional[str], label: str) -> Dict[str, Any]:
        if not label or len(label) > 128 or any(ord(char) < 32 for char in label):
            raise ConfigurationError("pin label must be 1-128 printable characters")
        snapshot = select_snapshot(list_snapshots(self.runner, project), identifier)
        stable_id = _tag(snapshot, "podvault.snapshot")
        self.runner.run(
            ["snapshot", "pin", snapshot["id"], "--add=podvault.label:{}".format(label)]
        )
        current = snapshot
        if stable_id:
            current = select_snapshot(list_snapshots(self.runner, project), stable_id)
        return {
            "status": "success",
            "engine": "kopia",
            "project": project,
            "label": label,
            "podvault_snapshot_id": stable_id,
            "previous_kopia_manifest_id": snapshot["id"],
            "kopia_manifest_id": current["id"],
        }

    def delete_project(
        self,
        project: str,
        snapshots: Optional[List[Dict[str, Any]]] = None,
        show_progress: bool = True,
        run_maintenance: bool = True,
    ) -> Dict[str, Any]:
        selected = list(snapshots) if snapshots is not None else list_snapshots(
            self.runner, project, include_incomplete=True
        )
        manifest_ids = [str(item.get("id") or "") for item in selected]
        if any(not manifest_id for manifest_id in manifest_ids):
            raise VerificationError("Kopia returned a snapshot without a manifest ID")

        for index, manifest_id in enumerate(manifest_ids, start=1):
            self.console.info(
                "Deleting Kopia snapshot {}/{}...".format(index, len(manifest_ids))
            )
            self.runner.run(
                ["--no-progress", "snapshot", "delete", manifest_id, "--delete"]
            )

        remaining = list_snapshots(self.runner, project, include_incomplete=True)
        if remaining:
            raise VerificationError(
                "Kopia deletion left {} snapshot(s) for project {}".format(
                    len(remaining), project
                )
            )

        if run_maintenance:
            self.console.info(
                "Running safe full Kopia maintenance to reclaim unreferenced storage..."
            )
            try:
                args = [
                    "--progress" if show_progress else "--no-progress",
                    "maintenance",
                    "run",
                    "--full",
                ]
                if show_progress:
                    self.runner.run_streaming(args)
                else:
                    self.runner.run(args)
                maintenance = {
                    "status": "completed",
                    "safety": "full",
                    "note": "Kopia safety windows may defer physical blob reclamation.",
                }
            except KopiaCommandError as exc:
                maintenance = {
                    "status": "failed",
                    "safety": "full",
                    "error": str(exc),
                    "note": "Snapshots are deleted, but unreferenced storage may remain until full maintenance succeeds.",
                }
        else:
            maintenance = {
                "status": "skipped",
                "note": "Unreferenced storage remains until full Kopia maintenance runs.",
            }

        return {
            "status": "success",
            "operation": "delete",
            "engine": "kopia",
            "project": project,
            "deleted_snapshot_count": len(manifest_ids),
            "deleted_kopia_manifest_ids": manifest_ids,
            "maintenance": maintenance,
        }


def snapshot_view(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    root = snapshot.get("rootEntry") or {}
    return {
        "engine": "kopia",
        "project": _tag(snapshot, "podvault.project"),
        "podvault_snapshot_id": _tag(snapshot, "podvault.snapshot"),
        "kopia_manifest_id": snapshot.get("id"),
        "kopia_root_object_id": root.get("obj"),
        "start_time": snapshot.get("startTime"),
        "end_time": snapshot.get("endTime"),
        "description": snapshot.get("description"),
        "summary": root.get("summ", {}),
        "pins": snapshot.get("pins", []),
    }
