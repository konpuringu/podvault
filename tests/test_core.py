import base64
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit

from podvault.azure import AzureBlobClient, parse_sas_url
from podvault.azcopy import AzCopyRunner
from podvault.browse import kopia_tree
from podvault.catalog import ProjectCatalog
from podvault.cli import Application, _parser
from podvault.config import ConfigStore
from podvault.direct import AzCopyService, select_direct_snapshot
from podvault.errors import (
    ConfigurationError,
    CredentialError,
    KopiaCommandError,
    SafetyError,
    VerificationError,
)
from podvault.kopia import CommandResult, KopiaRunner
from podvault.output import Console
from podvault.paths import validate_destination, validate_relative_path, validate_source
from podvault.project import canonical_source, snapshot_tags
from podvault.receipts import ReceiptStore
from podvault.redaction import redact
from podvault.retention import parse_timestamp, snapshots_before, snapshots_through
from podvault.restore import RestoreService, compare_summary, local_tree_summary
from podvault.snapshots import SnapshotService, select_snapshot


VALID_SAS = (
    "https://account123.blob.core.windows.net/podvault"
    "?sv=2024-11-04&sr=c&sp=rcwl&spr=https&se=2099-01-01T00%3A00%3A00Z&sig=secret"
)
DELETE_SAS = VALID_SAS.replace("sp=rcwl", "sp=rcwld")


class AzureTests(unittest.TestCase):
    def test_parses_container_sas_and_builds_kopia_token(self):
        sas = parse_sas_url(VALID_SAS)
        self.assertEqual(sas.storage_account, "account123")
        self.assertEqual(sas.container, "podvault")
        sas.require_permissions(write=True)
        token = sas.kopia_connection_token()
        padding = "=" * ((4 - len(token) % 4) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(token + padding))
        self.assertEqual(decoded["storage"]["type"], "azureBlob")
        self.assertEqual(decoded["storage"]["config"]["sasToken"], sas.token)
        self.assertNotIn("password", decoded)

    def test_rejects_blob_sas(self):
        value = VALID_SAS.replace("/podvault?", "/podvault/one.bin?").replace("sr=c", "sr=b")
        with self.assertRaises(CredentialError):
            parse_sas_url(value)

    def test_rejects_expired_sas(self):
        with self.assertRaises(CredentialError):
            parse_sas_url(VALID_SAS.replace("2099-01-01", "2020-01-01"))

    def test_builds_escaped_blob_urls_and_requires_create_for_azcopy(self):
        sas = parse_sas_url(VALID_SAS)
        url = sas.blob_url(".podvault/projects/a project/data/*", wildcard=True)
        self.assertIn("a%20project/data/*?", url)
        sas.require_azcopy_permissions(write=True)
        without_create = parse_sas_url(VALID_SAS.replace("sp=rcwl", "sp=rwl"))
        with self.assertRaises(CredentialError):
            without_create.require_azcopy_permissions(write=True)

    def test_delete_requires_delete_permission(self):
        parse_sas_url(DELETE_SAS).require_delete_permissions(repository_write=True)
        with self.assertRaises(CredentialError):
            parse_sas_url(VALID_SAS).require_delete_permissions()

    def test_relative_snapshot_paths_are_normalized_and_traversal_is_rejected(self):
        self.assertEqual(
            validate_relative_path("./checkpoints//run-42"),
            "checkpoints/run-42",
        )
        self.assertEqual(validate_relative_path("."), "")
        for value in ("/etc", "../checkpoints", "checkpoints/../../etc", "bad\nname"):
            with self.subTest(value=value), self.assertRaises(SafetyError):
                validate_relative_path(value)

    def test_hierarchical_blob_listing_includes_metadata(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
<EnumerationResults><Blobs>
  <BlobPrefix><Name>data/checkpoints/</Name></BlobPrefix>
  <Blob><Name>data/link</Name><Properties><Last-Modified>now</Last-Modified><Content-Length>6</Content-Length></Properties><Metadata><is_symlink>true</is_symlink></Metadata></Blob>
</Blobs><NextMarker /></EnumerationResults>"""
        with mock.patch("podvault.azure.urlopen", return_value=io.BytesIO(xml)) as opened:
            values = AzureBlobClient(parse_sas_url(VALID_SAS)).list_blobs(
                "data/", include_metadata=True, delimiter="/"
            )
        self.assertEqual(values[0]["type"], "directory")
        self.assertEqual(values[1]["metadata"]["is_symlink"], "true")
        request_url = opened.call_args.args[0].full_url
        self.assertIn("include=metadata", request_url)
        self.assertIn("delimiter=%2F", request_url)


class ConfigEngineTests(unittest.TestCase):
    def test_project_engine_is_persisted_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigStore(Path(directory) / "config.json")
            first = Path(directory) / "one"
            second = Path(directory) / "two"
            config.set_project("newlm", first, engine="azcopy")
            config.set_project("newlm", second)
            self.assertEqual(config.get_project("newlm")["engine"], "azcopy")
            self.assertEqual(config.get_project("newlm")["path"], str(second))
            self.assertTrue(config.remove_project("newlm"))
            self.assertIsNone(config.get_project("newlm"))
            self.assertFalse(config.remove_project("newlm"))

    def test_remote_engine_is_discovered_and_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            args = _parser().parse_args(
                ["--config", str(config_path), "restore", "newlm"]
            )
            app = Application(args)
            with mock.patch.object(
                app, "_remote_project_record", return_value={"engine": "azcopy"}
            ):
                self.assertEqual(app._resolve_engine("newlm", None), "azcopy")
                with self.assertRaises(ConfigurationError):
                    app._resolve_engine("newlm", "kopia")

    def test_legacy_local_project_defaults_to_kopia(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config = ConfigStore(config_path)
            config.data["projects"]["newlm"] = {"path": "/workspace/newlm"}
            config.save()
            args = _parser().parse_args(
                ["--config", str(config_path), "restore", "newlm"]
            )
            app = Application(args)
            with mock.patch.object(app, "_remote_project_record", return_value=None):
                self.assertEqual(app._resolve_engine("newlm", None), "kopia")


class SafetyTests(unittest.TestCase):
    def test_rejects_broad_source(self):
        with self.assertRaises(SafetyError):
            validate_source("/")

    def test_rejects_source_containing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            secret = source / "private" / "credentials.json"
            with self.assertRaises(SafetyError):
                validate_source(str(source), [secret])

    def test_rejects_dangerous_destination(self):
        with self.assertRaises(SafetyError):
            validate_destination("/workspace")


class DeleteConfirmationTests(unittest.TestCase):
    def test_requires_exact_project_name(self):
        with tempfile.TemporaryDirectory() as directory:
            args = _parser().parse_args(
                ["--config", str(Path(directory) / "config.json"), "delete", "newlm"]
            )
            app = Application(args)
            with mock.patch.object(sys.stdin, "isatty", return_value=True), mock.patch(
                "builtins.input", return_value="wrong-name"
            ), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(ConfigurationError):
                    app._confirm_delete("newlm", "kopia", "2 Kopia snapshots")

    def test_yes_allows_noninteractive_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            args = _parser().parse_args(
                [
                    "--config",
                    str(Path(directory) / "config.json"),
                    "delete",
                    "newlm",
                    "--yes",
                ]
            )
            app = Application(args)
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                with contextlib.redirect_stderr(io.StringIO()):
                    app._confirm_delete(
                        "newlm", "azcopy", "every stored generation"
                    )


class ProjectIdentityTests(unittest.TestCase):
    def test_identity_is_independent_of_host_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            tags = snapshot_tags("newlm", Path(directory), "0.2.0", "stable-id")
        self.assertEqual(canonical_source("newlm"), "podvault@podvault:/projects/newlm")
        self.assertEqual(tags["podvault.project"], "newlm")
        self.assertEqual(tags["podvault.snapshot"], "stable-id")


class RedactionTests(unittest.TestCase):
    def test_redacts_sas_url_and_known_secret(self):
        value = "failed {} password=hunter2 extra-key".format(VALID_SAS)
        result = redact(value, ["extra-key"])
        self.assertNotIn("sig=secret", result)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("extra-key", result)


class SnapshotSelectionTests(unittest.TestCase):
    def test_selects_latest_and_stable_id(self):
        snapshots = [
            {
                "id": "manifest-old",
                "endTime": "2026-01-01T00:00:00Z",
                "tags": {"tag:podvault.snapshot": "stable-old"},
            },
            {
                "id": "manifest-new",
                "endTime": "2026-02-01T00:00:00Z",
                "tags": {"tag:podvault.snapshot": "stable-new"},
            },
        ]
        self.assertEqual(select_snapshot(snapshots)["id"], "manifest-new")
        self.assertEqual(select_snapshot(snapshots, "stable-old")["id"], "manifest-old")


class RetentionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = [
            {"id": "old", "endTime": "2026-01-01T00:00:00.123456789Z"},
            {"id": "middle", "endTime": "2026-02-01T00:00:00Z"},
            {"id": "new", "endTime": "2026-03-01T00:00:00+00:00"},
        ]

    def test_before_is_strict_and_accepts_utc_dates(self):
        selected = snapshots_before(self.snapshots, "2026-02-01")
        self.assertEqual([item["id"] for item in selected], ["old"])

    def test_through_is_inclusive(self):
        selected = snapshots_through(self.snapshots, self.snapshots[1])
        self.assertEqual([item["id"] for item in selected], ["old", "middle"])

    def test_rejects_invalid_cutoff(self):
        with self.assertRaises(ConfigurationError):
            parse_timestamp("last Tuesday")


class DeletingKopiaRunner:
    def __init__(self, fail_maintenance=False):
        self.snapshots = [
            {
                "id": "manifest-one",
                "endTime": "2026-01-01T00:00:00Z",
                "tags": {
                    "tag:podvault.schema": "1",
                    "tag:podvault.project": "newlm",
                    "tag:podvault.snapshot": "stable-one",
                },
            },
            {
                "id": "manifest-two",
                "endTime": "2026-02-01T00:00:00Z",
                "tags": {
                    "tag:podvault.schema": "1",
                    "tag:podvault.project": "newlm",
                    "tag:podvault.snapshot": "stable-two",
                },
            },
        ]
        self.deleted = []
        self.list_calls = []
        self.maintenance_calls = 0
        self.fail_maintenance = fail_maintenance

    def run_streaming(self, args, **kwargs):
        return self.run(args, **kwargs)

    def run(self, args, **kwargs):
        values = list(args)
        if "list" in values:
            self.list_calls.append(values)
            return CommandResult(values, 0, json.dumps(self.snapshots), "")
        if "delete" in values:
            manifest_id = values[values.index("delete") + 1]
            self.deleted.append(manifest_id)
            self.snapshots = [
                item for item in self.snapshots if item["id"] != manifest_id
            ]
            return CommandResult(values, 0, "", "")
        if "maintenance" in values:
            self.maintenance_calls += 1
            if self.fail_maintenance:
                raise KopiaCommandError("injected maintenance failure")
            return CommandResult(values, 0, "", "")
        raise AssertionError(values)


class KopiaDeletionTests(unittest.TestCase):
    def test_deletes_only_selected_snapshot_and_preserves_project(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DeletingKopiaRunner()
            service = SnapshotService(
                runner,
                Console(False),
                ReceiptStore(Path(directory) / "receipts"),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.delete_project(
                    "newlm", snapshots=[runner.snapshots[0]], show_progress=False
                )
            self.assertEqual(runner.deleted, ["manifest-one"])
            self.assertEqual(result["deleted_podvault_snapshot_ids"], ["stable-one"])
            self.assertEqual(result["remaining_snapshot_count"], 1)
            self.assertFalse(result["project_deleted"])

    def test_deletes_all_project_snapshots_and_runs_safe_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DeletingKopiaRunner()
            service = SnapshotService(
                runner,
                Console(False),
                ReceiptStore(Path(directory) / "receipts"),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.delete_project("newlm", show_progress=False)
            self.assertEqual(runner.deleted, ["manifest-one", "manifest-two"])
            self.assertTrue(
                all("--incomplete" in values for values in runner.list_calls)
            )
            self.assertEqual(runner.maintenance_calls, 1)
            self.assertEqual(result["deleted_snapshot_count"], 2)
            self.assertEqual(result["maintenance"]["status"], "completed")

    def test_maintenance_failure_does_not_misreport_snapshot_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DeletingKopiaRunner(fail_maintenance=True)
            service = SnapshotService(
                runner,
                Console(False),
                ReceiptStore(Path(directory) / "receipts"),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.delete_project("newlm", show_progress=False)
            self.assertEqual(runner.snapshots, [])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["maintenance"]["status"], "failed")


class RestoreSummaryTests(unittest.TestCase):
    def test_counts_files_directories_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sub").mkdir()
            (root / "sub" / "file.txt").write_text("hello", encoding="utf-8")
            (root / "link").symlink_to("sub/file.txt")
            result = local_tree_summary(root)
            self.assertEqual(result, {"size": 5, "files": 1, "symlinks": 1, "dirs": 2})
            compare_summary(result, result)
            with self.assertRaises(VerificationError):
                compare_summary({"files": 2}, result)


class FailingVerifyRunner:
    def version(self):
        return (0, 23, 1)

    def run_streaming(self, args, **kwargs):
        return self.run(args, **kwargs)

    def run(self, args, **kwargs):
        if "create" in args:
            manifest = {
                "id": "manifest-id",
                "rootEntry": {"obj": "kroot", "summ": {"numFailed": 0, "files": 1}},
            }
            return CommandResult(list(args), 0, json.dumps(manifest), "")
        if "verify" in args:
            raise VerificationError("injected verification failure")
        raise AssertionError(args)


class FailureReceiptTests(unittest.TestCase):
    def test_failed_verification_never_claims_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("data", encoding="utf-8")
            output = io.StringIO()
            service = SnapshotService(
                FailingVerifyRunner(), Console(False), ReceiptStore(root / "receipts")
            )
            with contextlib.redirect_stdout(output):
                with self.assertRaises(VerificationError):
                    service.save(source, "project")
            self.assertNotIn("SAFE TO TERMINATE: YES", output.getvalue())
            receipts = list((root / "receipts" / "project").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertFalse(payload["safe_to_terminate"])


class FailingRestoreRunner:
    def run_streaming(self, args, **kwargs):
        return self.run(args, **kwargs)

    def run(self, args, **kwargs):
        if "list" in args:
            snapshot = {
                "id": "manifest-id",
                "endTime": "2026-01-01T00:00:00Z",
                "tags": {
                    "tag:podvault.schema": "1",
                    "tag:podvault.project": "newlm",
                    "tag:podvault.snapshot": "stable-id",
                },
                "rootEntry": {
                    "obj": "root-object",
                    "summ": {"size": 4, "files": 1, "symlinks": 0, "dirs": 1},
                },
            }
            return CommandResult(list(args), 0, json.dumps([snapshot]), "")
        if "verify" in args:
            return CommandResult(list(args), 0, json.dumps({"errorCount": 0}), "")
        if "restore" in args:
            destination = Path(args[args.index("restore") + 2])
            destination.mkdir()
            (destination / "partial.txt").write_text("data", encoding="utf-8")
            raise KopiaCommandError("injected restore failure")
        raise AssertionError(args)


class InterruptedRestoreTests(unittest.TestCase):
    def test_failure_preserves_staging_and_recovery_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConfigStore(root / "config.json")
            service = RestoreService(
                FailingRestoreRunner(), Console(False), ReceiptStore(root / "receipts"), config
            )
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KopiaCommandError):
                    service.restore("newlm", None, str(root / "destination"))
            staging = [path for path in root.glob(".destination.podvault-restore-*") if path.is_dir()]
            markers = list(root.glob(".destination.podvault-restore-state-*.json"))
            self.assertEqual(len(staging), 1)
            self.assertEqual(len(markers), 1)
            state = json.loads(markers[0].read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "interrupted-or-failed")


class RecordingRestoreRunner:
    def __init__(self):
        self.restore_args = []

    def run_streaming(self, args, **kwargs):
        return self.run(args, **kwargs)

    def run(self, args, **kwargs):
        if "list" in args:
            snapshot = {
                "id": "manifest-id",
                "endTime": "2026-01-01T00:00:00Z",
                "tags": {
                    "tag:podvault.schema": "1",
                    "tag:podvault.project": "newlm",
                    "tag:podvault.snapshot": "stable-id",
                },
                "rootEntry": {
                    "obj": "root-object",
                    "summ": {"size": 4, "files": 1, "symlinks": 0, "dirs": 1},
                },
            }
            return CommandResult(list(args), 0, json.dumps([snapshot]), "")
        if "verify" in args:
            return CommandResult(list(args), 0, json.dumps({"errorCount": 0}), "")
        if "restore" in args:
            self.restore_args = list(args)
            destination = Path(args[args.index("restore") + 2])
            destination.mkdir()
            (destination / "file.txt").write_text("data", encoding="utf-8")
            return CommandResult(list(args), 0, "", "")
        raise AssertionError(args)


class OptimizedKopiaRestoreTests(unittest.TestCase):
    def _restore(self, durable, preserve_owners=False):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        runner = RecordingRestoreRunner()
        service = RestoreService(
            runner,
            Console(False),
            ReceiptStore(root / "receipts"),
            ConfigStore(root / "config.json"),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = service.restore(
                "newlm",
                None,
                str(root / "destination"),
                show_progress=False,
                parallel=24,
                durable=durable,
                preserve_owners=preserve_owners,
            )
        return temporary, runner.restore_args, result

    def test_default_uses_parallelism_without_redundant_flushes(self):
        temporary, args, result = self._restore(durable=False)
        try:
            self.assertIn("--parallel=24", args)
            self.assertNotIn("--flush-files", args)
            self.assertNotIn("--write-files-atomically", args)
            self.assertIn("--skip-owners", args)
            self.assertIn("--no-ignore-permission-errors", args)
            self.assertEqual(result["engine"], "kopia")
            self.assertEqual(result["ownership_restore"], "current-user")
            self.assertIn("transfer", result["timings_seconds"])
        finally:
            temporary.cleanup()

    def test_durable_mode_restores_old_flush_behavior(self):
        temporary, args, result = self._restore(durable=True)
        try:
            self.assertIn("--flush-files", args)
            self.assertIn("--write-files-atomically", args)
            self.assertTrue(result["durable_restore"])
        finally:
            temporary.cleanup()

    def test_original_owners_are_an_explicit_opt_in(self):
        temporary, args, result = self._restore(
            durable=False, preserve_owners=True
        )
        try:
            self.assertNotIn("--skip-owners", args)
            self.assertEqual(result["ownership_restore"], "original")
        finally:
            temporary.cleanup()


class SelectiveKopiaRunner:
    def __init__(self):
        self.restore_args = []
        self.directories = {
            "root-object": {
                "stream": "kopia:directory",
                "summary": {"size": 8, "files": 2, "symlinks": 0, "dirs": 3},
                "entries": [
                    {"name": "checkpoints", "type": "d", "obj": "checkpoints-object", "size": 8}
                ],
            },
            "checkpoints-object": {
                "stream": "kopia:directory",
                "summary": {"size": 8, "files": 2, "symlinks": 0, "dirs": 2},
                "entries": [
                    {"name": "run-42", "type": "d", "obj": "run-object", "size": 8}
                ],
            },
            "run-object": {
                "stream": "kopia:directory",
                "summary": {"size": 8, "files": 2, "symlinks": 0, "dirs": 1},
                "entries": [
                    {"name": "model.bin", "type": "f", "obj": "file-object", "size": 4},
                    {"name": " leading.bin", "type": "f", "obj": "file-object-2", "size": 4},
                ],
            },
        }

    def run_streaming(self, args, **kwargs):
        return self.run(args, **kwargs)

    def run(self, args, **kwargs):
        if "snapshot" in args and "list" in args:
            snapshot = {
                "id": "manifest-id",
                "endTime": "2026-01-01T00:00:00Z",
                "tags": {
                    "tag:podvault.schema": "1",
                    "tag:podvault.project": "newlm",
                    "tag:podvault.snapshot": "stable-id",
                },
                "rootEntry": {"obj": "root-object", "summ": self.directories["root-object"]["summary"]},
            }
            return CommandResult(list(args), 0, json.dumps([snapshot]), "")
        if "verify" in args:
            return CommandResult(list(args), 0, json.dumps({"errorCount": 0}), "")
        if "show" in args:
            object_id = args[args.index("show") + 1]
            return CommandResult(list(args), 0, json.dumps(self.directories[object_id]), "")
        if "list" in args and "--recursive" in args:
            output = (
                "drwxr-xr-x 4 2026-01-01 00:00:00 UTC {:<34} {}\n".format(
                    "run-object-2", "run-42/"
                )
                + "-rw-r--r-- 4 2026-01-01 00:00:00 UTC {:<34} {}\n".format(
                    "file-object", "run-42/model.bin"
                )
                + "-rw-r--r-- 4 2026-01-01 00:00:00 UTC {:<34} {}\n".format(
                    "file-object-2", "run-42/ leading.bin"
                )
            )
            return CommandResult(list(args), 0, output, "")
        if "restore" in args:
            self.restore_args = list(args)
            destination = Path(args[args.index("restore") + 2])
            destination.mkdir()
            (destination / "model.bin").write_text("data", encoding="utf-8")
            (destination / " leading.bin").write_text("data", encoding="utf-8")
            return CommandResult(list(args), 0, "", "")
        raise AssertionError(args)


class SelectiveKopiaTests(unittest.TestCase):
    def test_tree_and_selective_restore_use_directory_object(self):
        runner = SelectiveKopiaRunner()
        listing = kopia_tree(runner, "root-object", "checkpoints", recursive=True)
        self.assertEqual(
            [item["path"] for item in listing["entries"]],
            [
                "checkpoints/run-42",
                "checkpoints/run-42/model.bin",
                "checkpoints/run-42/ leading.bin",
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ConfigStore(root / "config.json")
            remembered = root / "complete-project"
            config.set_project("newlm", remembered, engine="kopia")
            service = RestoreService(
                runner,
                Console(False),
                ReceiptStore(root / "receipts"),
                config,
            )
            destination = root / "run-42"
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.restore(
                    "newlm",
                    None,
                    str(destination),
                    show_progress=False,
                    relative_path="checkpoints/run-42",
                )
            self.assertEqual(
                runner.restore_args[runner.restore_args.index("restore") + 1],
                "root-object/checkpoints/run-42",
            )
            self.assertEqual(result["path"], "checkpoints/run-42")
            self.assertTrue(result["selective"])
            self.assertEqual(config.get_project("newlm")["path"], str(remembered))
            self.assertEqual((destination / "model.bin").read_text(), "data")

    def test_selective_restore_requires_destination_and_directory(self):
        runner = SelectiveKopiaRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = RestoreService(
                runner,
                Console(False),
                ReceiptStore(root / "receipts"),
                ConfigStore(root / "config.json"),
            )
            with self.assertRaises(ConfigurationError):
                service.restore("newlm", None, None, relative_path="checkpoints")
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ConfigurationError):
                service.restore(
                    "newlm",
                    None,
                    str(root / "bad"),
                    show_progress=False,
                    relative_path="checkpoints/run-42/model.bin",
                )

class MemoryBlobClient:
    def __init__(self):
        self.values = {}

    def put_json(self, path, value):
        self.values[path] = json.loads(json.dumps(value))

    def get_json(self, path, missing_ok=False):
        value = self.values.get(path)
        if value is None and not missing_ok:
            return None
        return json.loads(json.dumps(value)) if value is not None else None

    def delete_blob(self, path, missing_ok=False):
        if path not in self.values:
            return False
        del self.values[path]
        return True

    def list_blobs(self, prefix, include_metadata=False, delimiter=None):
        result = {}
        for name, value in self.values.items():
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            if delimiter and delimiter in remainder:
                first = remainder.split(delimiter, 1)[0]
                item_name = prefix + first + delimiter
                result[(item_name, "directory")] = {
                    "name": item_name,
                    "size": None,
                    "metadata": {},
                    "type": "directory",
                }
                continue
            metadata = value.get("_metadata", {}) if isinstance(value, dict) else {}
            size = (
                value.get("_blob_size", len(json.dumps(value)))
                if isinstance(value, dict)
                else len(json.dumps(value))
            )
            result[(name, "blob")] = {
                "name": name,
                "size": size,
                "metadata": metadata,
                "last_modified": None,
            }
        return list(result.values())


class FakeAzCopyRunner:
    def __init__(self, restore_source, blobs=None):
        self.restore_source = restore_source
        self.blobs = blobs
        self.uploads = []
        self.downloads = []
        self.deletions = []
        self.fail_upload = False

    def require_supported_version(self):
        return (10, 32, 6)

    def upload_tree(self, source, destination_url, show_progress):
        if self.fail_upload:
            raise RuntimeError("injected AzCopy failure")
        self.uploads.append((source, destination_url, show_progress))
        if self.blobs is not None:
            path = unquote(urlsplit(destination_url).path).lstrip("/")
            _, prefix = path.split("/", 1)
            for item in source.rglob("*"):
                relative = item.relative_to(source).as_posix()
                metadata = {}
                if item.is_symlink():
                    metadata["is_symlink"] = "true"
                    size = len(os.readlink(str(item)).encode("utf-8"))
                elif item.is_dir():
                    metadata["hdi_isfolder"] = "true"
                    size = 0
                else:
                    size = item.stat().st_size
                self.blobs.values[prefix + "/" + relative] = {
                    "_blob_size": size,
                    "_metadata": metadata,
                }

    def download_tree(self, source_url, destination, show_progress):
        self.downloads.append((source_url, destination, show_progress))
        decoded = unquote(urlsplit(source_url).path)
        selected = self.restore_source
        if "/data/" in decoded:
            selected = selected / decoded.split("/data/", 1)[1]
        shutil.copytree(selected, destination, symlinks=True)

    def delete_tree(self, source_url, show_progress):
        self.deletions.append((source_url, show_progress))
        if self.blobs is None:
            return
        path = unquote(urlsplit(source_url).path).lstrip("/")
        _, prefix = path.split("/", 1)
        for key in list(self.blobs.values):
            if key.startswith(prefix + "/"):
                del self.blobs.values[key]


class AzCopyServiceTests(unittest.TestCase):
    def test_generation_commit_restore_and_failed_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("data", encoding="utf-8")
            (source / "link").symlink_to("file.txt")
            blobs = MemoryBlobClient()
            catalog = ProjectCatalog(blobs)
            runner = FakeAzCopyRunner(source, blobs)
            config = ConfigStore(root / "config.json")
            service = AzCopyService(
                runner,
                parse_sas_url(VALID_SAS),
                blobs,
                catalog,
                Console(False),
                ReceiptStore(root / "receipts"),
                config,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                saved = service.save(source, "newlm", "direct", show_progress=False)
            self.assertTrue(saved["safe_to_terminate"])
            self.assertEqual(config.get_project("newlm")["engine"], "azcopy")
            current = catalog.get("newlm")["current_snapshot"]
            self.assertEqual(current, saved["podvault_snapshot_id"])
            self.assertEqual(len(service.list("newlm")), 1)

            destination = root / "restored"
            with contextlib.redirect_stdout(io.StringIO()):
                restored = service.restore(
                    "newlm", None, str(destination), show_progress=False
                )
            self.assertEqual(restored["restored_summary"]["files"], 1)
            self.assertTrue((destination / "link").is_symlink())

            runner.fail_upload = True
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as failure:
                    service.save(source, "newlm", "failed", show_progress=False)
            self.assertEqual(catalog.get("newlm")["current_snapshot"], current)
            self.assertTrue(hasattr(failure.exception, "receipt_path"))
            receipt = json.loads(
                Path(failure.exception.receipt_path).read_text(encoding="utf-8")
            )
            self.assertFalse(receipt["safe_to_terminate"])

    def test_delete_removes_all_generations_and_catalog_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("data", encoding="utf-8")
            blobs = MemoryBlobClient()
            catalog = ProjectCatalog(blobs)
            runner = FakeAzCopyRunner(source, blobs)
            service = AzCopyService(
                runner,
                parse_sas_url(DELETE_SAS),
                blobs,
                catalog,
                Console(False),
                ReceiptStore(root / "receipts"),
                ConfigStore(root / "config.json"),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                service.save(source, "newlm", "one", show_progress=False)
                service.save(source, "newlm", "two", show_progress=False)
                result = service.delete_project("newlm", show_progress=False)
            self.assertEqual(result["deleted_generations"], "all")
            self.assertIsNone(catalog.get("newlm"))
            self.assertFalse(
                blobs.list_blobs(".podvault/azcopy/v1/projects/newlm/")
            )
            self.assertEqual(len(runner.deletions), 1)

    def test_delete_current_generation_repoints_to_newest_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("one", encoding="utf-8")
            blobs = MemoryBlobClient()
            catalog = ProjectCatalog(blobs)
            runner = FakeAzCopyRunner(source, blobs)
            service = AzCopyService(
                runner,
                parse_sas_url(DELETE_SAS),
                blobs,
                catalog,
                Console(False),
                ReceiptStore(root / "receipts"),
                ConfigStore(root / "config.json"),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                first = service.save(source, "newlm", "one", show_progress=False)
                (source / "file.txt").write_text("two", encoding="utf-8")
                second = service.save(source, "newlm", "two", show_progress=False)
                latest = select_direct_snapshot(service.list("newlm"), second["podvault_snapshot_id"])
                result = service.delete_snapshots(
                    "newlm", [latest], show_progress=False
                )
            self.assertEqual(result["deleted_generation_count"], 1)
            self.assertEqual(result["remaining_snapshot_count"], 1)
            self.assertFalse(result["project_deleted"])
            self.assertEqual(
                catalog.get("newlm")["current_snapshot"],
                first["podvault_snapshot_id"],
            )
            self.assertEqual(len(service.list("newlm")), 1)

    def test_tree_and_selective_restore_use_only_selected_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            selected = source / "checkpoints" / "run-42"
            selected.mkdir(parents=True)
            (selected / "model.bin").write_bytes(b"weights")
            (source / "root.txt").write_text("root", encoding="utf-8")
            (source / "root-link").symlink_to("root.txt")
            blobs = MemoryBlobClient()
            catalog = ProjectCatalog(blobs)
            runner = FakeAzCopyRunner(source, blobs)
            config = ConfigStore(root / "config.json")
            service = AzCopyService(
                runner,
                parse_sas_url(VALID_SAS),
                blobs,
                catalog,
                Console(False),
                ReceiptStore(root / "receipts"),
                config,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                service.save(source, "newlm", "direct", show_progress=False)
            top = service.tree("newlm", None, None, recursive=False)
            self.assertEqual(
                [(item["path"], item["type"]) for item in top["entries"]],
                [
                    ("checkpoints", "directory"),
                    ("root-link", "symlink"),
                    ("root.txt", "file"),
                ],
            )
            subtree = service.tree(
                "newlm", None, "checkpoints", recursive=True
            )
            self.assertEqual(
                [item["path"] for item in subtree["entries"]],
                ["checkpoints/run-42", "checkpoints/run-42/model.bin"],
            )

            destination = root / "run-42"
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.restore(
                    "newlm",
                    None,
                    str(destination),
                    show_progress=False,
                    relative_path="checkpoints/run-42",
                )
            self.assertEqual((destination / "model.bin").read_bytes(), b"weights")
            self.assertFalse((destination / "root.txt").exists())
            self.assertEqual(result["restored_summary"]["files"], 1)
            self.assertTrue(result["selective"])
            self.assertTrue(runner.downloads[-1][0].split("?", 1)[0].endswith("/data/checkpoints/run-42"))
            self.assertEqual(config.get_project("newlm")["path"], str(source))

            with self.assertRaises(ConfigurationError):
                service.tree("newlm", None, "root.txt", recursive=False)

    def test_delete_can_remove_orphaned_generation_without_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            blobs = MemoryBlobClient()
            orphan = ".podvault/azcopy/v1/projects/newlm/snapshots/orphan/data/file"
            blobs.values[orphan] = {"content": "placeholder"}
            runner = FakeAzCopyRunner(source, blobs)
            service = AzCopyService(
                runner,
                parse_sas_url(DELETE_SAS),
                blobs,
                ProjectCatalog(blobs),
                Console(False),
                ReceiptStore(root / "receipts"),
                ConfigStore(root / "config.json"),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                result = service.delete_project("newlm", show_progress=False)
            self.assertFalse(result["catalog_record_deleted"])
            self.assertNotIn(orphan, blobs.values)


class StreamingProgressTests(unittest.TestCase):
    def test_streams_progress_and_redacts_complete_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "fake-kopia"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "secret = os.environ.get('KOPIA_PASSWORD', '')\n"
                "sys.stderr.write('uploaded 25 GB, estimated 100 GB (25.0%) 3m left\\r')\n"
                "sys.stderr.flush()\n"
                "sys.stderr.write('diagnostic password=' + secret + '\\n')\n"
                "sys.stderr.flush()\n"
                "sys.stdout.write('{\"id\":\"manifest-id\"}\\n')\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            progress = io.StringIO()
            runner = KopiaRunner(
                root / "repository.config",
                password="streaming-secret",
                executable=str(helper),
                known_secrets=["streaming-secret"],
            )
            result = runner.run_streaming(["snapshot", "create"], progress_stream=progress)
            displayed = progress.getvalue()
            self.assertIn("estimated 100 GB (25.0%) 3m left", displayed)
            self.assertIn("[REDACTED]", displayed)
            self.assertNotIn("streaming-secret", displayed)
            self.assertEqual(json.loads(result.stdout)["id"], "manifest-id")

    def test_azcopy_streaming_redacts_sas_and_uses_fast_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "fake-azcopy"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "if '--version' in sys.argv:\n"
                "    print('azcopy version 10.32.6')\n"
                "else:\n"
                "    print('copying ' + sys.argv[3])\n"
                "    print('concurrency=' + os.environ.get('AZCOPY_CONCURRENCY_VALUE', ''))\n"
                "    print('temp=' + os.environ.get('AZCOPY_DOWNLOAD_TO_TEMP_PATH', ''))\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            progress = io.StringIO()
            runner = AzCopyRunner(
                root / "state",
                executable=str(helper),
                known_secrets=[VALID_SAS],
            )
            self.assertEqual(runner.require_supported_version(), (10, 32, 6))
            runner.run(
                ["copy", "source", VALID_SAS],
                show_progress=True,
                progress_stream=progress,
            )
            displayed = progress.getvalue()
            self.assertNotIn("sig=secret", displayed)
            self.assertIn("[REDACTED]", displayed)
            self.assertIn("concurrency=AUTO", displayed)
            self.assertIn("temp=false", displayed)


if __name__ == "__main__":
    unittest.main()
