"""Line-by-line output of what a command did."""

from __future__ import annotations

import sys


class Reporter:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.lines: list[tuple[str, str, str, str]] = []

    def line(self, status: str, kind: str, name: str, detail: str = "") -> None:
        self.lines.append((status, kind, name, detail))
        tail = f"  {detail}" if detail else ""
        print(f"{status:<10} {kind:<18} {name}{tail}", file=self.stream)

    def note(self, text: str) -> None:
        print(text, file=self.stream)
