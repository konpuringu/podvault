"""Remote snapshot browsing helpers for Kopia repositories."""

import json
import re
from typing import Any, Dict, List

from .errors import ConfigurationError, VerificationError
from .kopia import KopiaRunner


_LONG_ENTRY = re.compile(
    r"^(?P<mode>\S+)\s+(?P<size>\d+)\s+(?P<date>\S+)\s+"
    r"(?P<time>\S+)\s+(?P<zone>\S+)\s+(?P<object>\S+)(?P<tail>.*)$"
)


def _entry_type(value: Any) -> str:
    kind = str(value or "")
    return {"d": "directory", "f": "file", "s": "symlink"}.get(kind, "other")


def _show_directory(runner: KopiaRunner, object_id: str) -> Dict[str, Any]:
    result = runner.run(["--no-progress", "show", object_id])
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise VerificationError("Kopia returned invalid directory metadata") from exc
    if not isinstance(value, dict) or value.get("stream") != "kopia:directory":
        raise ConfigurationError("selected snapshot path is not a directory")
    entries = value.get("entries")
    if not isinstance(entries, list) or not isinstance(value.get("summary"), dict):
        raise VerificationError("Kopia directory metadata is incomplete")
    return value


def resolve_kopia_directory(
    runner: KopiaRunner, root_object_id: str, relative_path: str
) -> Dict[str, Any]:
    """Resolve a directory without asking Kopia to stream a possible file."""
    current_id = root_object_id
    current = _show_directory(runner, current_id)
    for component in relative_path.split("/") if relative_path else ():
        match = next(
            (
                entry
                for entry in current["entries"]
                if isinstance(entry, dict) and entry.get("name") == component
            ),
            None,
        )
        if not match:
            raise ConfigurationError(
                "snapshot directory not found: {}".format(relative_path)
            )
        if _entry_type(match.get("type")) != "directory":
            raise ConfigurationError(
                "snapshot path is not a directory: {}".format(relative_path)
            )
        current_id = str(match.get("obj") or "")
        if not current_id:
            raise VerificationError("Kopia directory entry has no object ID")
        current = _show_directory(runner, current_id)
    return {
        "object_id": current_id,
        "summary": current["summary"],
        "entries": current["entries"],
    }


def _join_selected(relative_path: str, name: str) -> str:
    return relative_path + "/" + name if relative_path else name


def kopia_tree(
    runner: KopiaRunner,
    root_object_id: str,
    relative_path: str,
    recursive: bool,
) -> Dict[str, Any]:
    directory = resolve_kopia_directory(runner, root_object_id, relative_path)
    if not recursive:
        entries = []
        for item in directory["entries"]:
            if not isinstance(item, dict):
                continue
            entries.append(
                {
                    "path": _join_selected(relative_path, str(item.get("name") or "")),
                    "type": _entry_type(item.get("type")),
                    "size": item.get("size"),
                    "mode": item.get("mode"),
                    "mtime": item.get("mtime"),
                    "object_id": item.get("obj"),
                }
            )
        return {"summary": directory["summary"], "entries": entries}

    object_id = str(directory["object_id"])
    result = runner.run(
        [
            "--no-progress",
            "list",
            object_id,
            "--long",
            "--recursive",
            "--show-object-id",
        ]
    )
    entries: List[Dict[str, Any]] = []
    prefix = object_id + "/"
    for line in result.stdout.splitlines():
        match = _LONG_ENTRY.match(line)
        if not match:
            if line.strip():
                raise VerificationError("unable to parse Kopia directory listing")
            continue
        values = match.groupdict()
        # Kopia formats object IDs in a left-aligned 34-character field,
        # followed by one separator space. Remove precisely that formatting so
        # leading whitespace in a real filename remains intact.
        separator = " " * (max(34 - len(values["object"]), 0) + 1)
        tail = values["tail"]
        if not tail.startswith(separator):
            raise VerificationError("unable to parse Kopia directory listing")
        name = tail[len(separator) :]
        mode = values["mode"]
        if name.startswith(prefix):
            name = name[len(prefix) :]
        kind = {"d": "directory", "-": "file", "L": "symlink"}.get(
            mode[:1], "other"
        )
        if kind == "directory" and name.endswith("/"):
            name = name[:-1]
        entries.append(
            {
                "path": _join_selected(relative_path, name),
                "type": kind,
                "size": int(values["size"]),
                "mode": mode,
                "mtime": "{} {} {}".format(
                    values["date"], values["time"], values["zone"]
                ),
                "object_id": values["object"],
            }
        )
    return {"summary": directory["summary"], "entries": entries}
