"""Which translations the overlay shows: the game's subtitle lines, stacked the same way.

The engine reports the lines on screen (``SubtitleLines``, top to bottom) and each line's
translation as it arrives. The overlay mirrors the screen: every line on screen that has a
translation, in the game's order. Translations lag the game by about a second, so a line that
left the screen stays a little longer (``clear_delay_s``) - until then, or until a newer line's
translation arrives to take its place, the way the game scrolls an old line out for a new one.
While a line on screen still waits for its translation, the lines that left stay up whatever the
delay (one subtitle replaced by the next: the old translation stays until the new one comes).
A translation that arrives after its line already left is shown for ``clear_delay_s`` too.

Pure logic, owned by the UI thread; times are ``time.monotonic()`` seconds.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShownLine:
    translation: str
    original: str


class ShownLines:
    def __init__(self, clear_delay_s: float = 2.0) -> None:
        self.clear_delay_s = clear_delay_s
        self._on_screen: tuple[int, ...] = ()
        self._translations: dict[int, ShownLine] = {}
        self._leaving: dict[int, float] = {}  # left the screen, still shown until this time

    def reset(self) -> None:
        """A new Start numbers lines from scratch."""
        self._on_screen = ()
        self._translations.clear()
        self._leaving.clear()

    def translation(self, line_id: int) -> ShownLine | None:
        return self._translations.get(line_id)

    def screen(self, line_ids: tuple[int, ...], now: float) -> None:
        """The lines on screen now, top to bottom."""
        if self._waiting():  # lines held for a waiting line get their full delay from now on
            for line_id in self._leaving:
                self._leaving[line_id] = now + self.clear_delay_s
        for line_id in self._on_screen:
            if line_id not in line_ids and line_id in self._translations:
                self._leaving[line_id] = now + self.clear_delay_s
        for line_id in line_ids:
            self._leaving.pop(line_id, None)  # back on screen
        self._on_screen = line_ids

    def translated(self, line_id: int, translation: str, original: str, now: float) -> None:
        self._translations[line_id] = ShownLine(translation, original)
        if line_id not in self._on_screen:
            self._leaving[line_id] = now + self.clear_delay_s  # late: show it for a moment
        # Older lines that already left make room for this one.
        for old in [i for i in self._leaving if i < line_id]:
            del self._leaving[old]

    def hide_all(self) -> None:
        """Stop or pause: nothing is shown until new lines come."""
        self._on_screen = ()
        self._leaving.clear()

    def lines(self, now: float) -> list[ShownLine]:
        """What the overlay shows now: lines that are leaving (they were above), then the lines
        on screen in the game's order."""
        if not self._waiting():
            for line_id in [i for i, until in self._leaving.items() if until <= now]:
                del self._leaving[line_id]
        leaving = [self._translations[i] for i in sorted(self._leaving)]
        on_screen = [self._translations[i] for i in self._on_screen if i in self._translations]
        return leaving + on_screen

    def next_change(self) -> float | None:
        """When a leaving line goes (to schedule a refresh), or None."""
        if self._waiting():
            return None  # the waiting line's translation will replace them
        return min(self._leaving.values(), default=None)

    def _waiting(self) -> bool:
        """A line on screen still waits for its translation. Lines that left meanwhile stay up
        until it comes: a blank overlay while the model works looked like subtitles vanishing."""
        return any(i not in self._translations for i in self._on_screen)
