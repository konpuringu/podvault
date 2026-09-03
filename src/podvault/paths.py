"""Configuration locations and defensive filesystem validation."""

import os
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable, Optional

from .errors import DestinationConflictError, SafetyError


def _xdg(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else fallback


@dataclass(frozen=True)
class AppPaths:
    config_file: Path
    credentials_file: Path
    kopia_config_file: Path
    cache_dir: Path
    state_dir: Path

    @classmethod
    def discover(cls, explicit_config: Optional[str] = None) -> "AppPaths":
        if explicit_config:
            config_file = Path(explicit_config).expanduser().resolve()
            base = config_file.parent
            return cls(
                config_file=config_file,
                credentials_file=base / "credentials.json",
                kopia_config_file=base / "kopia.repository.config",
                cache_dir=base / "cache",
                state_dir=base / "state",
            )

        home = Path.home()
        config_home = _xdg("XDG_CONFIG_HOME", home / ".config")
        cache_home = _xdg("XDG_CACHE_HOME", home / ".cache")
        state_home = _xdg("XDG_STATE_HOME", home / ".local" / "state")
        config_dir = config_home / "podvault"
        return cls(
            config_file=config_dir / "config.json",
            credentials_file=config_dir / "credentials.json",
            kopia_config_file=config_dir / "kopia.repository.config",
            cache_dir=cache_home / "podvault" / "kopia",
            state_dir=state_home / "podvault",
        )

    @property
    def receipts_dir(self) -> Path:
        return self.state_dir / "receipts"

    def ensure_directories(self) -> None:
        for directory in (
            self.config_file.parent,
            self.cache_dir,
            self.state_dir,
            self.receipts_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except OSError:
                pass


_BROAD_PATHS = {
    Path("/"),
    Path("/workspace"),
    Path("/home"),
    Path("/root"),
    Path("/usr"),
    Path("/var"),
    Path("/etc"),
    Path("/opt"),
    Path("/mnt"),
    Path("/data"),
}


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_broad(path: Path, kind: str) -> None:
    if path in _BROAD_PATHS or path == Path.home().resolve():
        raise SafetyError("refusing dangerous {} path: {}".format(kind, path))


def validate_source(path_value: str, protected: Iterable[Path] = ()) -> Path:
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError("source path is not accessible: {}".format(path_value)) from exc
    if not path.is_dir():
        raise SafetyError("source must be a directory: {}".format(path))
    _reject_broad(path, "source")
    for item in protected:
        resolved = item.expanduser().resolve(strict=False)
        if _contains(path, resolved):
            raise SafetyError(
                "source contains Podvault configuration or credentials: {}".format(resolved)
            )
    return path


def validate_destination(path_value: str) -> Path:
    raw = Path(path_value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    if raw.is_symlink():
        raise DestinationConflictError("destination may not be a symbolic link: {}".format(raw))
    try:
        parent = raw.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError("destination parent does not exist: {}".format(raw.parent)) from exc
    path = parent / raw.name
    _reject_broad(path, "destination")
    if path.exists():
        if not path.is_dir():
            raise DestinationConflictError("destination exists and is not a directory: {}".format(path))
        try:
            next(path.iterdir())
        except StopIteration:
            pass
        else:
            raise DestinationConflictError("destination is not empty: {}".format(path))
    return path


def validate_relative_path(path_value: Optional[str]) -> str:
    """Return a normalized project-relative POSIX path.

    Snapshot paths are remote POSIX paths regardless of the host running the
    CLI. An empty value or ``.`` selects the snapshot root.
    """
    value = path_value or ""
    if not value or value == ".":
        return ""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SafetyError("snapshot path may not contain control characters")
    candidate = PurePosixPath(value)
    if candidate.is_absolute():
        raise SafetyError("snapshot path must be relative to the project root")
    if ".." in candidate.parts:
        raise SafetyError("snapshot path may not contain '..' traversal")
    normalized = str(candidate)
    return "" if normalized == "." else normalized
