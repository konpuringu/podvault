"""Stable project identity and source preflight checks."""

import fnmatch
import os
import re
import socket
import time
from pathlib import Path
from typing import Dict, List

from .errors import ConfigurationError


PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def validate_project_name(value: str) -> str:
    if not PROJECT_PATTERN.match(value):
        raise ConfigurationError(
            "project name must be 1-64 lowercase letters, digits, dots, underscores, or hyphens"
        )
    return value


def canonical_source(project: str) -> str:
    return "podvault@podvault:/projects/{}".format(validate_project_name(project))


def snapshot_tags(project: str, source: Path, version: str, snapshot_uuid: str) -> Dict[str, str]:
    return {
        "podvault.project": project,
        "podvault.snapshot": snapshot_uuid,
        "podvault.schema": "1",
        "podvault.version": version,
        "podvault.actual-host": socket.gethostname(),
        "podvault.actual-source": str(source),
    }


def risky_files(source: Path, max_entries: int = 50000, recent_seconds: int = 120) -> List[str]:
    """Return a bounded sample of files that may indicate an active/incomplete write."""
    warnings = []
    checked = 0
    cutoff = time.time() - recent_seconds
    patterns = ("*.partial", "*.tmp", "*.part", "*.incomplete")
    try:
        for root, directories, files in os.walk(str(source), followlinks=False):
            directories[:] = [name for name in directories if not Path(root, name).is_symlink()]
            for name in files:
                checked += 1
                if checked > max_entries or len(warnings) >= 20:
                    return warnings
                path = Path(root, name)
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                relative = str(path.relative_to(source))
                if modified >= cutoff or any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                    warnings.append(relative)
    except OSError:
        pass
    return warnings
