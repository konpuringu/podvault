"""Kopia subprocess adapter."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import DependencyError, KopiaCommandError
from .redaction import redact


MINIMUM_KOPIA_VERSION = (0, 23, 1)


@dataclass
class CommandResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str


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
