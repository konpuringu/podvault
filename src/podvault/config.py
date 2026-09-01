"""Non-secret Podvault configuration."""

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ConfigurationError


DEFAULT_CONFIG = {
    "schema_version": 1,
    "repository": {},
    "projects": {},
    "preferences": {"large_file_bytes": 10 * 1024 * 1024 * 1024},
}


def atomic_json_write(path: Path, value: Dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".{}-".format(path.name), dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, str(path))
        os.chmod(str(path), mode)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return deepcopy(DEFAULT_CONFIG)
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, ValueError) as exc:
            raise ConfigurationError("invalid configuration file: {}".format(self.path)) from exc
        if loaded.get("schema_version") != 1:
            raise ConfigurationError("unsupported configuration schema")
        result = deepcopy(DEFAULT_CONFIG)
        result.update(loaded)
        result.setdefault("repository", {})
        result.setdefault("projects", {})
        result.setdefault("preferences", {})
        return result

    def save(self) -> None:
        atomic_json_write(self.path, self.data, 0o600)

    @property
    def repository(self) -> Dict[str, Any]:
        return self.data["repository"]

    def set_repository(self, value: Dict[str, Any]) -> None:
        self.data["repository"] = value
        self.save()

    def get_project(self, name: str) -> Optional[Dict[str, Any]]:
        value = self.data["projects"].get(name)
        return dict(value) if value else None

    def set_project(self, name: str, path: Path) -> None:
        self.data["projects"][name] = {"path": str(path)}
        self.save()
