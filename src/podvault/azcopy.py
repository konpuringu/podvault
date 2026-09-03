"""AzCopy process adapter with progress streaming and secret redaction."""

import os
import re
import selectors
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, TextIO, Tuple

from .errors import AzCopyCommandError, DependencyError
from .kopia import CommandResult, _RedactingProgressWriter
from .redaction import redact


MINIMUM_AZCOPY_VERSION = (10, 18, 0)


class AzCopyRunner:
    def __init__(
        self,
        state_dir: Path,
        executable: Optional[str] = None,
        known_secrets: Iterable[str] = (),
    ):
        candidate = executable or os.environ.get("PODVAULT_AZCOPY") or shutil.which("azcopy")
        if not candidate:
            raise DependencyError(
                "azcopy executable not found; install AzCopy v10.18.0 or newer and run podvault doctor"
            )
        self.executable = candidate
        self.state_dir = state_dir
        self.known_secrets = list(known_secrets)

    def _environment(self) -> dict:
        environment = os.environ.copy()
        jobs = self.state_dir / "azcopy" / "jobs"
        logs = self.state_dir / "azcopy" / "logs"
        jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment.setdefault("AZCOPY_JOB_PLAN_LOCATION", str(jobs))
        environment.setdefault("AZCOPY_LOG_LOCATION", str(logs))
        environment.setdefault("AZCOPY_CONCURRENCY_VALUE", "AUTO")
        # Podvault already isolates downloads in a staging tree. Avoiding a
        # second per-file temporary path removes redundant disk work.
        environment.setdefault("AZCOPY_DOWNLOAD_TO_TEMP_PATH", "false")
        environment.setdefault("AZCOPY_DISABLE_SYSLOG", "true")
        return environment

    def run(
        self,
        args: Iterable[str],
        check: bool = True,
        show_progress: bool = True,
        progress_stream: Optional[TextIO] = None,
    ) -> CommandResult:
        command = [self.executable] + list(args)
        stream = progress_stream if progress_stream is not None else sys.stderr
        stdout_writer = _RedactingProgressWriter(stream, self.known_secrets)
        stderr_writer = _RedactingProgressWriter(stream, self.known_secrets)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment(),
                shell=False,
            )
        except OSError as exc:
            raise DependencyError("unable to execute AzCopy: {}".format(exc)) from exc
        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise DependencyError("unable to capture AzCopy output streams")

        output = bytearray()
        errors = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map():
                for key, _ in selector.select(timeout=0.5):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        output.extend(chunk)
                        if show_progress:
                            stdout_writer.feed(chunk)
                    else:
                        errors.extend(chunk)
                        if show_progress:
                            stderr_writer.feed(chunk)
            returncode = process.wait()
            if show_progress:
                stdout_writer.finish()
                stderr_writer.finish()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if show_progress:
                stdout_writer.finish()
                stderr_writer.finish()
            raise
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

        result = CommandResult(
            args=command,
            returncode=returncode,
            stdout=output.decode("utf-8", errors="replace"),
            stderr=errors.decode("utf-8", errors="replace"),
        )
        if check and returncode != 0:
            safe_out = redact(result.stdout, self.known_secrets)
            safe_err = redact(result.stderr, self.known_secrets)
            detail = safe_err.strip() or safe_out.strip() or "unknown AzCopy error"
            raise AzCopyCommandError(
                "AzCopy command failed: {}".format(detail),
                returncode,
                safe_out,
                safe_err,
            )
        return result

    def version(self) -> Tuple[int, int, int]:
        result = self.run(["--version"], show_progress=False)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
        if not match:
            raise DependencyError("unable to parse AzCopy version")
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]

    def require_supported_version(self) -> Tuple[int, int, int]:
        version = self.version()
        if version < MINIMUM_AZCOPY_VERSION:
            raise DependencyError(
                "AzCopy {}.{}.{} is too old; v10.18.0 or newer is required".format(*version)
            )
        return version

    def upload_tree(self, source: Path, destination_url: str, show_progress: bool) -> None:
        self.run(
            [
                "copy",
                str(source),
                destination_url,
                "--recursive=true",
                "--as-subdir=false",
                "--overwrite=false",
                "--include-directory-stub=true",
                "--preserve-symlinks=true",
                "--preserve-posix-properties=true",
                "--put-md5",
                "--log-level=ERROR",
            ],
            show_progress=show_progress,
        )

    def download_tree(self, source_url: str, destination: Path, show_progress: bool) -> None:
        self.run(
            [
                "copy",
                source_url,
                str(destination),
                "--recursive=true",
                "--as-subdir=false",
                "--overwrite=false",
                "--include-directory-stub=true",
                "--preserve-symlinks=true",
                "--preserve-posix-properties=true",
                "--check-md5=FailIfDifferent",
                "--log-level=ERROR",
            ],
            show_progress=show_progress,
        )

    def delete_tree(self, source_url: str, show_progress: bool) -> None:
        self.run(
            [
                "remove",
                source_url,
                "--recursive=true",
                "--delete-snapshots=include",
                "--log-level=ERROR",
            ],
            show_progress=show_progress,
        )
