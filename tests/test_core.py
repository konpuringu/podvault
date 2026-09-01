import base64
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from podvault.azure import parse_sas_url
from podvault.config import ConfigStore
from podvault.errors import CredentialError, KopiaCommandError, SafetyError, VerificationError
from podvault.kopia import CommandResult
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
            tags = snapshot_tags("newlm", Path(directory), "0.1.0", "stable-id")
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


if __name__ == "__main__":
    unittest.main()
