import base64
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from podvault.azure import parse_sas_url
from podvault.azcopy import AzCopyRunner
from podvault.catalog import ProjectCatalog
from podvault.cli import Application, _parser
from podvault.config import ConfigStore
from podvault.direct import AzCopyService
from podvault.errors import (
    ConfigurationError,
    CredentialError,
    KopiaCommandError,
    SafetyError,
    VerificationError,
)
from podvault.kopia import CommandResult, KopiaRunner
from podvault.output import Console
from podvault.paths import validate_destination, validate_source
from podvault.project import canonical_source, snapshot_tags
from podvault.receipts import ReceiptStore
from podvault.redaction import redact
from podvault.restore import RestoreService, compare_summary, local_tree_summary
from podvault.snapshots import SnapshotService, select_snapshot


VALID_SAS = (
    "https://account123.blob.core.windows.net/podvault"
    "?sv=2024-11-04&sr=c&sp=rcwl&spr=https&se=2099-01-01T00%3A00%3A00Z&sig=secret"
)


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
    def _restore(self, durable):
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
            )
        return temporary, runner.restore_args, result

    def test_default_uses_parallelism_without_redundant_flushes(self):
        temporary, args, result = self._restore(durable=False)
        try:
            self.assertIn("--parallel=24", args)
            self.assertNotIn("--flush-files", args)
            self.assertNotIn("--write-files-atomically", args)
            self.assertEqual(result["engine"], "kopia")
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

    def list_blobs(self, prefix):
        return [
            {"name": name, "size": len(json.dumps(value))}
            for name, value in self.values.items()
            if name.startswith(prefix)
        ]


class FakeAzCopyRunner:
    def __init__(self, restore_source):
        self.restore_source = restore_source
        self.uploads = []
        self.downloads = []
        self.fail_upload = False

    def require_supported_version(self):
        return (10, 32, 6)

    def upload_tree(self, source, destination_url, show_progress):
        if self.fail_upload:
            raise RuntimeError("injected AzCopy failure")
        self.uploads.append((source, destination_url, show_progress))

    def download_tree(self, source_url, destination, show_progress):
        self.downloads.append((source_url, destination, show_progress))
        shutil.copytree(self.restore_source, destination, symlinks=True)


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
            runner = FakeAzCopyRunner(source)
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
