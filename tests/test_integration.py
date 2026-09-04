import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from podvault.cli import main
from podvault.config import ConfigStore


KOPIA = os.environ.get("PODVAULT_KOPIA") or shutil.which("kopia")


@unittest.skipUnless(KOPIA, "Kopia executable not available")
class LocalRepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config1 = self.root / "pod1" / "config.json"
        self.config2 = self.root / "pod2" / "config.json"
        self.repository = self.root / "repository"
        self.source = self.root / "source project"
        self.source.mkdir()
        (self.source / ".podvaultignore").write_text("excluded.tmp\n", encoding="utf-8")
        (self.source / "file with spaces.txt").write_text("version one", encoding="utf-8")
        executable = self.source / "train.sh"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        (self.source / "unicodé.txt").write_text("snowman ☃", encoding="utf-8")
        (self.source / "excluded.tmp").write_text("not backed up", encoding="utf-8")
        (self.source / "model-link").symlink_to("file with spaces.txt")
        selected = self.source / "checkpoints" / "run-42"
        selected.mkdir(parents=True)
        (selected / "model.bin").write_bytes(b"checkpoint-data")
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PODVAULT_KOPIA": str(KOPIA),
                "PODVAULT_REPOSITORY_PASSWORD": "integration-password",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def call(self, config, *args, json_mode=False):
        argv = ["--config", str(config)]
        if json_mode:
            argv.append("--json")
        argv.extend(args)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_full_incremental_reconnect_and_restore_workflow(self):
        code, _, error = self.call(
            self.config1,
            "repository",
            "init",
            "filesystem",
            str(self.repository),
        )
        self.assertEqual(code, 0, error)

        with mock.patch("podvault.project.socket.gethostname", return_value="simulated-pod-one"):
            code, output, error = self.call(
                self.config1,
                "save",
                str(self.source),
                "--name",
                "newlm",
                "--description",
                "initial",
                json_mode=True,
            )
        self.assertEqual(code, 0, error)
        first = json.loads(output)
        self.assertTrue(first["safe_to_terminate"])
        self.assertNotIn("SAFE TO TERMINATE: YES", error)
        self.assertIn("Snapshotting", error)
        self.assertIn("uploaded", error)

        (self.source / "file with spaces.txt").write_text("version two", encoding="utf-8")
        with mock.patch("podvault.project.socket.gethostname", return_value="simulated-pod-two"):
            code, output, error = self.call(
                self.config1, "save", "newlm", "--description", "incremental", json_mode=True
            )
        self.assertEqual(code, 0, error)
        second = json.loads(output)
        self.assertNotEqual(first["podvault_snapshot_id"], second["podvault_snapshot_id"])

        code, output, error = self.call(
            self.config1,
            "save",
            "newlm",
            "--dry-run",
            "--no-progress",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["operation"], "dry-run")
        self.assertNotIn("Analyzing", error)
        self.assertNotIn("hashing", error)

        code, _, error = self.call(
            self.config2,
            "repository",
            "connect",
            "filesystem",
            str(self.repository),
        )
        self.assertEqual(code, 0, error)
        code, output, error = self.call(self.config2, "list", "newlm", json_mode=True)
        self.assertEqual(code, 0, error)
        snapshots = json.loads(output)["snapshots"]
        self.assertEqual(len(snapshots), 2)
        self.assertEqual({item["project"] for item in snapshots}, {"newlm"})

        code, output, error = self.call(
            self.config2,
            "tree",
            "newlm",
            "--path",
            "checkpoints",
            "--recursive",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        tree = json.loads(output)
        self.assertEqual(tree["path"], "checkpoints")
        self.assertEqual(
            [item["path"] for item in tree["entries"]],
            ["checkpoints/run-42", "checkpoints/run-42/model.bin"],
        )

        selective_destination = self.root / "selected-run"
        code, output, error = self.call(
            self.config2,
            "restore",
            "newlm",
            "--path",
            "checkpoints/run-42",
            "--to",
            str(selective_destination),
            "--no-progress",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        selective = json.loads(output)
        self.assertTrue(selective["selective"])
        self.assertEqual(
            (selective_destination / "model.bin").read_bytes(), b"checkpoint-data"
        )
        self.assertIsNone(ConfigStore(self.config2).get_project("newlm"))

        destination = self.root / "restored"
        destination.mkdir()
        code, output, error = self.call(
            self.config2,
            "restore",
            "newlm",
            "--latest",
            "--to",
            str(destination),
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        restored = json.loads(output)
        self.assertEqual(restored["restored_summary"]["files"], 5)
        self.assertEqual(restored["ownership_restore"], "current-user")
        self.assertEqual((destination / "file with spaces.txt").read_text(), "version two")
        self.assertFalse((destination / "excluded.tmp").exists())
        self.assertTrue((destination / "model-link").is_symlink())
        self.assertTrue((destination / "train.sh").stat().st_mode & stat.S_IXUSR)

        code, _, error = self.call(
            self.config2, "restore", "newlm", "--to", str(destination)
        )
        self.assertEqual(code, 6)
        self.assertIn("not empty", error)

        code, output, error = self.call(
            self.config2,
            "delete",
            "newlm",
            "--snapshot",
            first["podvault_snapshot_id"],
            "--yes",
            "--no-maintenance",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        deleted_old = json.loads(output)
        self.assertEqual(deleted_old["deleted_snapshot_count"], 1)
        self.assertEqual(deleted_old["remaining_snapshot_count"], 1)
        self.assertFalse(deleted_old["project_deleted"])

        (destination / "file with spaces.txt").write_text(
            "version three", encoding="utf-8"
        )
        code, output, error = self.call(
            self.config2,
            "save",
            "newlm",
            "--description",
            "third",
            "--no-progress",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        third = json.loads(output)

        code, output, error = self.call(
            self.config2,
            "delete",
            "newlm",
            "--through",
            second["podvault_snapshot_id"],
            "--yes",
            "--no-maintenance",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        deleted_through = json.loads(output)
        self.assertEqual(deleted_through["deleted_snapshot_count"], 1)
        self.assertEqual(deleted_through["remaining_snapshot_count"], 1)
        self.assertFalse(deleted_through["project_deleted"])
        code, output, error = self.call(
            self.config2, "list", "newlm", json_mode=True
        )
        self.assertEqual(code, 0, error)
        remaining = json.loads(output)["snapshots"]
        self.assertEqual(
            [item["podvault_snapshot_id"] for item in remaining],
            [third["podvault_snapshot_id"]],
        )

        code, output, error = self.call(
            self.config2,
            "delete",
            "newlm",
            "--yes",
            json_mode=True,
        )
        self.assertEqual(code, 0, error)
        deleted = json.loads(output)
        self.assertEqual(deleted["deleted_snapshot_count"], 1)
        self.assertEqual(deleted["maintenance"]["status"], "completed")
        self.assertFalse(deleted["local_directory_deleted"])
        self.assertTrue((destination / "file with spaces.txt").exists())
        self.assertIsNone(ConfigStore(self.config2).get_project("newlm"))

        code, output, error = self.call(self.config2, "list", "newlm", json_mode=True)
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["count"], 0)

    def test_dangerous_source_exit_status(self):
        code, _, _ = self.call(self.config1, "save", "/", "--name", "root")
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
