"""Kopia subprocess adapter."""

import codecs
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TextIO, Tuple

from .errors import DependencyError, KopiaCommandError
from .redaction import redact


MINIMUM_KOPIA_VERSION = (0, 23, 1)


@dataclass
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str


class _RedactingProgressWriter:
    """Forward complete CR/LF-delimited records without leaking secrets."""

    def __init__(self, stream: TextIO, known_secrets: Iterable[str]):
        self.stream = stream
        self.known_secrets = list(known_secrets)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""
        self.last_delimiter = ""

    def feed(self, value: bytes) -> None:
        self.pending += self.decoder.decode(value)
        self._write_complete_records()

    def _write_complete_records(self) -> None:
        start = 0
        for index, character in enumerate(self.pending):
            if character not in "\r\n":
                continue
            self.stream.write(
                redact(self.pending[start:index], self.known_secrets) + character
            )
            self.last_delimiter = character
            start = index + 1
        self.pending = self.pending[start:]
        self.stream.flush()

    def finish(self) -> None:
        self.pending += self.decoder.decode(b"", final=True)
        if self.pending:
            self.stream.write(redact(self.pending, self.known_secrets))
            self.pending = ""
        elif self.last_delimiter == "\r":
            self.stream.write("\n")
        self.stream.flush()


class KopiaRunner:
    def __init__(
        self,
        config_file: Path,
        password: Optional[str] = None,
        executable: Optional[str] = None,
        known_secrets: Iterable[str] = (),
    ):
        candidate = executable or os.environ.get("PODVAULT_KOPIA") or shutil.which("kopia")
        if not candidate:
            raise DependencyError(
                "kopia executable not found; install Kopia v0.23.1 or newer and run podvault doctor"
            )
        self.executable = candidate
        self.config_file = config_file
        self.password = password
        self.known_secrets = list(known_secrets)

    def with_password(self, password: str, known_secrets: Iterable[str] = ()) -> "KopiaRunner":
        return KopiaRunner(
            self.config_file,
            password=password,
            executable=self.executable,
            known_secrets=list(self.known_secrets) + list(known_secrets) + [password],
        )

    def _base_args(self, repository_command: bool = True) -> List[str]:
        args = [self.executable, "--disable-file-logging"]
        if repository_command:
            args.append("--config-file={}".format(self.config_file))
            args.append("--no-auto-maintenance")
            args.append("--no-persist-credentials")
        return args

    def run(
        self,
        args: Iterable[str],
        input_text: Optional[str] = None,
        check: bool = True,
        repository_command: bool = True,
    ) -> CommandResult:
        command = self._base_args(repository_command=repository_command) + list(args)
        environment = os.environ.copy()
        if self.password:
            environment["KOPIA_PASSWORD"] = self.password
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                check=False,
            )
        except OSError as exc:
            raise DependencyError("unable to execute Kopia: {}".format(exc)) from exc
        result = CommandResult(
            args=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            safe_out = redact(result.stdout, self.known_secrets)
            safe_err = redact(result.stderr, self.known_secrets)
            detail = safe_err.strip() or safe_out.strip() or "unknown Kopia error"
            raise KopiaCommandError(
                "Kopia command failed: {}".format(detail),
                result.returncode,
                safe_out,
                safe_err,
            )
        return result

    def run_streaming(
        self,
        args: Iterable[str],
        check: bool = True,
        repository_command: bool = True,
        progress_stream: Optional[TextIO] = None,
    ) -> CommandResult:
        """Run Kopia while forwarding its redacted stderr progress records.

        Kopia writes the final JSON document to stdout and live progress to
        stderr. Keeping the streams separate preserves machine-readable output
        while still giving interactive and redirected callers timely feedback.
        """
        command = self._base_args(repository_command=repository_command) + list(args)
        environment = os.environ.copy()
        if self.password:
            environment["KOPIA_PASSWORD"] = self.password
        stream = progress_stream if progress_stream is not None else sys.stderr
        forwarder = _RedactingProgressWriter(stream, self.known_secrets)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
            )
        except OSError as exc:
            raise DependencyError("unable to execute Kopia: {}".format(exc)) from exc

        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise DependencyError("unable to capture Kopia progress streams")

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
                    else:
                        errors.extend(chunk)
                        forwarder.feed(chunk)
            returncode = process.wait()
            forwarder.finish()
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            forwarder.finish()
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
        if check and result.returncode != 0:
            safe_out = redact(result.stdout, self.known_secrets)
            safe_err = redact(result.stderr, self.known_secrets)
            detail = safe_err.strip() or safe_out.strip() or "unknown Kopia error"
            raise KopiaCommandError(
                "Kopia command failed: {}".format(detail),
                result.returncode,
                safe_out,
                safe_err,
            )
        return result

    def version(self) -> Tuple[int, int, int]:
        result = self.run(["--version"], repository_command=False)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
        if not match:
            raise DependencyError("unable to parse Kopia version")
        return tuple(int(part) for part in match.groups())  # type: ignore[return-value]

    def require_supported_version(self) -> Tuple[int, int, int]:
        version = self.version()
        if version < MINIMUM_KOPIA_VERSION:
            raise DependencyError(
                "Kopia {}.{}.{} is too old; v0.23.1 or newer is required".format(*version)
            )
        return version


def parse_json_document(output: str) -> Any:
    stripped = output.strip()
    if not stripped:
        raise KopiaCommandError("Kopia returned no JSON output")
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    values = []
    for line in stripped.splitlines():
        try:
            values.append(json.loads(line))
        except ValueError:
            continue
    if not values:
        raise KopiaCommandError("unable to parse Kopia JSON output")
    return values[-1]


def parse_json_lines(output: str) -> List[Dict[str, Any]]:
    values = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            values.append(value)
    if not values:
        value = parse_json_document(output)
        if isinstance(value, dict):
            values.append(value)
    return values
