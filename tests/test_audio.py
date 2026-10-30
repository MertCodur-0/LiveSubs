"""The capture card's sound: the buffer between the two clocks, and when it plays."""

import numpy as np
import pytest

from livesubs.device.audio import AudioControl, AudioError, AudioErrorKind, Ring

RATE = 48000


def block(start: int, frames: int) -> np.ndarray:
    """Stereo samples numbered from ``start`` (left = right)."""
    values = np.arange(start, start + frames, dtype=np.float32)
    return np.stack([values, values], axis=1)


def test_ring_waits_for_its_target_then_plays_in_order() -> None:
    ring = Ring(RATE, 2)
    out = np.empty((480, 2), np.float32)
    ring.write(block(1, 480))
    ring.read(out)
    assert not out.any()  # 10 ms buffered, 30 ms wanted: silence while priming
    ring.write(block(481, 1440))
    ring.read(out)
    assert out[0, 0] == 1 and out[-1, 0] == 480
    ring.read(out)
    assert out[0, 0] == 481


def test_ring_underrun_plays_silence_and_primes_again() -> None:
    ring = Ring(RATE, 2)
    ring.write(block(1, ring.target))
    out = np.empty((ring.target + 100, 2), np.float32)
    ring.read(out)
    assert out[ring.target - 1, 0] == ring.target and not out[ring.target :].any()
    assert ring.underruns == 1
    ring.write(block(1, 100))
    small = np.empty((50, 2), np.float32)
    ring.read(small)
    assert not small.any()  # refilling up to the target first


def test_ring_drift_skips_back_to_the_newest_target() -> None:
    """The card's clock runs ahead of the output's: never let the delay grow."""
    ring = Ring(RATE, 2)
    written = 0
    for _ in range(20):
        ring.write(block(written + 1, 480))
        written += 480
    assert ring.fill <= ring.limit and ring.skips >= 1
    out = np.empty((ring.fill, 2), np.float32)
    ring.read(out)
    assert out[-1, 0] == written  # the newest sample is kept, the oldest were dropped


class Players:
    def __init__(self, error: AudioError | None = None) -> None:
        self.error = error
        self.opened: list[tuple[str, str]] = []
        self.closed = 0

    def __call__(self, card: str, output: str) -> "Players":
        if self.error is not None:
            raise self.error
        self.opened.append((card, output))
        return self

    def close(self) -> None:
        self.closed += 1


def test_sound_plays_only_with_card_setting_and_preview() -> None:
    events: list = []
    players = Players()
    audio = AudioControl(lambda *e: events.append(e), players)
    audio.set_output("")
    audio.set_card("V-Z632")
    assert players.opened == []  # the preview is not on screen
    audio.set_preview(True)
    assert players.opened == [("V-Z632", "")]
    assert events[-1] == (True, None, "")
    audio.set_preview(True)  # nothing changed
    assert len(players.opened) == 1

    audio.set_output("Hoparlör")
    assert players.closed == 1 and players.opened[-1] == ("V-Z632", "Hoparlör")
    audio.set_card(None)  # card lost
    assert players.closed == 2 and events[-1] == (False, None, "")
    audio.set_card("V-Z632")
    audio.set_output(None)  # setting off
    assert players.closed == 3


@pytest.mark.parametrize("kind", list(AudioErrorKind))
def test_a_failed_start_is_reported_once(kind: AudioErrorKind) -> None:
    events: list = []
    audio = AudioControl(lambda *e: events.append(e), Players(AudioError(kind, "x")))
    audio.set_output("")
    audio.set_preview(True)
    audio.set_card("V-Z632")
    audio.set_preview(True)
    assert events == [(False, kind, "x")]
