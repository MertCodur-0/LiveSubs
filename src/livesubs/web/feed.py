"""What the phone page shows: the current translation in the middle, a few older ones above it.

Pure state, owned by the UI thread (the server snapshots it under its lock). The page receives the
whole state each time and animates the difference itself, so the rules live only here.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

HISTORY = 4  # older lines kept above the current one, dimmer the older they are


@dataclass
class WebLine:
    key: int  # unique for the page (line ids restart with every Start)
    line_id: int | None  # the engine's line id; None once a new session started
    text: str


class WebFeed:
    def __init__(self, history: int = HISTORY) -> None:
        self.history: deque[WebLine] = deque(maxlen=history)
        self.current: WebLine | None = None
        self._next_key = 0

    def show(self, line_id: int, text: str) -> None:
        """A translation for engine line ``line_id``: a new line, an upgrade or a repeat."""
        if self.current is not None and self.current.line_id == line_id:
            self.current.text = text  # upgrade: replaced in place
            return
        if self.current is None and self.history and self.history[-1].line_id == line_id:
            self.current = self.history.pop()  # reappeared after a short gap: back to the middle
            self.current.text = text
            return
        for line in self.history:
            if line.line_id == line_id:  # a late upgrade of a line already moved up
                line.text = text
                return
        if self.current is not None:
            self.history.append(self.current)
        self.current = WebLine(self._next_key, line_id, text)
        self._next_key += 1

    def clear(self) -> None:
        """The subtitle is gone: its translation moves up with the older ones."""
        if self.current is not None:
            self.history.append(self.current)
            self.current = None

    def new_session(self) -> None:
        """A new Start numbers lines from scratch: never match the old ids again."""
        for line in (*self.history, self.current):
            if line is not None:
                line.line_id = None

    def snapshot(self) -> dict[str, object]:
        def item(line: WebLine) -> dict[str, object]:
            return {"key": line.key, "text": line.text}

        return {
            "history": [item(line) for line in self.history],
            "current": item(self.current) if self.current is not None else None,
        }
