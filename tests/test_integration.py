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

        (self.source / "file with spaces.txt").write_text("version two", encoding="utf-8")
        with mock.patch("podvault.project.socket.gethostname", return_value="simulated-pod-two"):
            code, output, error = self.call(
                self.config1, "save", "newlm", "--description", "incremental", json_mode=True
            )
        self.assertEqual(code, 0, error)
        second = json.loads(output)
        self.assertNotEqual(first["podvault_snapshot_id"], second["podvault_snapshot_id"])

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
        self.assertEqual(restored["restored_summary"]["files"], 4)
        self.assertEqual((destination / "file with spaces.txt").read_text(), "version two")
        self.assertFalse((destination / "excluded.tmp").exists())
        self.assertTrue((destination / "model-link").is_symlink())
        self.assertTrue((destination / "train.sh").stat().st_mode & stat.S_IXUSR)

        code, _, error = self.call(
            self.config2, "restore", "newlm", "--to", str(destination)
        )
        self.assertEqual(code, 6)
        self.assertIn("not empty", error)

    def test_dangerous_source_exit_status(self):
        code, _, _ = self.call(self.config1, "save", "/", "--name", "root")
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
