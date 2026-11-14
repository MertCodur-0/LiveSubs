"""Read game videos the way the app does and score the subtitles it reads.

Plays each video of ``tests/samples/videos.json`` through the engine's reading loop (region ->
preprocess -> change detection -> stabilizer -> OCR, two OCR inputs per read, the cleanest kept)
on a virtual clock: ticks at 10 fps, the stabilizer's 50 ms re-reads, 20 ms per OCR read. Every
line the stabilizer confirms or upgrades is an output (= a translation request; stacked lines
are outputs of their own); a subtitle line counts as read when an
output shown within 1.5 s of it contains all its words in order (case, accents and punctuation
ignored; "I" and "l" are the same glyph in many subtitle fonts).

Reported: lines read exactly (and exactly with punctuation), lines read in part, lines never read,
outputs that match no line ("phantom", e.g. garbled reads), median delay from the line's first
scan time to its first exact output.

The videos are game footage and stay out of the repo (see samples.json for the same rule): put
them in tests/samples/videos/ under the names in videos.json.

    .venv\\Scripts\\python.exe scripts\\bench_video_ocr.py
    .venv\\Scripts\\python.exe scripts\\bench_video_ocr.py v02 v19 --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import cv2

from livesubs.core.geometry import NormRect
from livesubs.core.profiles import VisionSettings
from livesubs.engine.ocr.winrt_ocr import WinRtOcr
from livesubs.engine.pipeline import read_subtitle
from livesubs.engine.text.normalize import clean, key
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.text.stabilizer import LineConfirmed, LineUpgraded, Stabilizer
from livesubs.engine.vision.change import ChangeDetector
from livesubs.engine.vision.preprocess import preprocess
from livesubs.platform.spellcheck import open_spell_checker

SAMPLES = Path(__file__).resolve().parent.parent / "tests" / "samples"
TICK_S = 0.1
OCR_COST_S = 0.02
WINDOW_S = 1.5  # outputs this long before a line's first / after its last scan time count
# Watermarks burned into the clips (not part of any game): ignored in outputs.
WATERMARK = re.compile(r"cyb|leek|support|trade|turbo|gatew|solana|villa|vilen|^buy$|^show$")


@dataclass
class Score:
    lines: int = 0
    exact: int = 0
    exact_punct: int = 0
    partial: int = 0
    missed: int = 0
    phantom: int = 0
    outputs: int = 0
    delays: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def add(self, other: Score) -> None:
        for name in ("lines", "exact", "exact_punct", "partial", "missed", "phantom", "outputs"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.delays += other.delays
        self.misses += other.misses

    def summary(self) -> str:
        delay = f"{statistics.median(self.delays):.2f} s" if self.delays else "-"
        share = 100 * self.exact / self.lines if self.lines else 0.0
        return (
            f"exact {self.exact}/{self.lines} ({share:.1f} %), with punctuation "
            f"{self.exact_punct} | partial {self.partial} | never read {self.missed} | "
            f"phantom outputs {self.phantom} of {self.outputs} | delay p50 {delay}"
        )


def words(text: str, *, strip_watermark: bool = False) -> list[str]:
    tokens = key(text).split()
    if strip_watermark:
        tokens = [t for t in tokens if not WATERMARK.search(t)]
    return [t.replace("l", "i") for t in tokens]


def contains(line: list[str], output: list[str]) -> bool:
    n = len(line)
    return any(output[i : i + n] == line for i in range(len(output) - n + 1))


def recall(line: list[str], output: list[str]) -> float:
    matcher = SequenceMatcher(a=line, b=output, autojunk=False)
    return sum(b.size for b in matcher.get_matching_blocks()) / max(1, len(line))


def punct(text: str) -> str:
    return " ".join(clean(text).replace("l", "I").split())


def replay(
    path: Path, region: NormRect, vision: VisionSettings, ocr: WinRtOcr, scorer: ReadScorer
) -> list[tuple[float, str]]:
    """``(virtual time, text)`` of every confirmed or upgraded line."""
    capture = cv2.VideoCapture(str(path))
    fps = capture.get(cv2.CAP_PROP_FPS)
    detector, stabilizer = ChangeDetector(), Stabilizer(scorer=scorer.score)
    outputs: list[tuple[float, str]] = []
    now, index, shown, frame = 0.0, -1, -1, None
    while True:
        now += stabilizer.fast_reread_s if stabilizer.awaiting_confirmation else TICK_S
        while index < int(now * fps):
            if not capture.grab():
                return outputs
            index += 1
        if index == shown and not stabilizer.awaiting_confirmation:
            continue  # nothing new on screen
        if index != shown:
            frame = capture.retrieve()[1]
            shown = index
        r = region.to_pixels(frame.shape[1], frame.shape[0])
        processed = preprocess(frame[r.y : r.bottom, r.x : r.right], vision)
        decision = stabilizer.update(
            detector.update(processed.mask), cv2.countNonZero(processed.mask), now
        )
        events = list(decision.events)
        if decision.read:
            _, _, rows = read_subtitle(ocr, processed, scorer)
            now += OCR_COST_S
            events += stabilizer.on_read(rows, now)
        for event in events:
            if isinstance(event, LineConfirmed | LineUpgraded):
                outputs.append((now, event.text))


def score(lines: list[list], outputs: list[tuple[float, str]]) -> Score:
    result = Score(lines=len(lines), outputs=len(outputs))
    tokens = [(t, text, words(text, strip_watermark=True)) for t, text in outputs]
    matched = [False] * len(tokens)
    for start, end, text in lines:
        line = words(text)
        best, first, with_punct = 0.0, None, False
        for i, (t, output, out_words) in enumerate(tokens):
            if not start - WINDOW_S <= t <= end + WINDOW_S:
                continue
            share = recall(line, out_words)
            matched[i] |= share > 0.5
            if contains(line, out_words):
                best, first = 1.0, first if first is not None else t
                with_punct |= punct(text) in punct(output)
            else:
                best = max(best, min(share, 0.99))
        if best == 1.0:
            result.exact += 1
            result.exact_punct += with_punct
            result.delays.append(max(0.0, first - start))
        elif best >= 0.5:
            result.partial += 1
            result.misses.append(f"{start:7.1f}  partial  {text}")
        else:
            result.missed += 1
            result.misses.append(f"{start:7.1f}  MISSED   {text}")
    result.phantom = sum(1 for (_, _, w), hit in zip(tokens, matched, strict=True) if w and not hit)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("videos", nargs="*", help="names from videos.json (default: all)")
    parser.add_argument("--verbose", action="store_true", help="list lines not read exactly")
    args = parser.parse_args()

    spec = json.loads((SAMPLES / "videos.json").read_text(encoding="utf-8"))
    x, y, width, height = spec["region"]
    region = NormRect(x=x, y=y, width=width, height=height)
    vision = VisionSettings(text_colors=spec["text_colors"])
    checker = open_spell_checker()
    scorer = ReadScorer(checker.misspelled if checker else None)
    ocr = WinRtOcr()
    total = Score()
    for video in spec["videos"]:
        if args.videos and video["name"] not in args.videos:
            continue
        path = SAMPLES / "videos" / video["file"]
        if not path.is_file():
            print(f"{video['name']}: {video['file']} not found, skipped")
            continue
        started = time.perf_counter()
        result = score(video["lines"], replay(path, region, vision, ocr, scorer))
        total.add(result)
        print(f"{video['name']}: {result.summary()}  [{time.perf_counter() - started:.0f} s]")
        if args.verbose:
            print("\n".join(f"    {m}" for m in result.misses))
    if not total.lines:
        sys.exit("no videos found")
    print(f"\nall: {total.summary()}")


if __name__ == "__main__":
    main()
