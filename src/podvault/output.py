"""Human and machine-readable command output."""

import json
import sys
from typing import Any, Dict, Iterable


class Console:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    def info(self, message: str) -> None:
        print(message, file=sys.stderr if self.json_mode else sys.stdout)

    def warning(self, message: str) -> None:
        print("WARNING: " + message, file=sys.stderr)

    def result(self, value: Dict[str, Any], human_lines: Iterable[str]) -> None:
        if self.json_mode:
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            for line in human_lines:
                print(line)
