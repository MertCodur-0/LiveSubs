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

**The figure to optimise is "on screen"**: the lines the stabilizer holds at each moment, joined
top to bottom, are what the viewer gets; a subtitle line is exact when some state shown within
1.5 s of it contains all its words in order. It does not penalise stacking (two sentences of one
subtitle line tracked as two lines) the way the per-output score does. Next to it: the number of
outputs (= translation requests; fewer is better at equal accuracy) and the real cost per read
(preprocess / OCR, median ms - only comparable on an otherwise idle machine).

``--set dev`` / ``--set holdout`` split the videos (``holdout`` in videos.json): tune on dev,
check on holdout. ``--json FILE`` writes the totals and every line not read exactly.
``LIVESUBS_VIDEOS`` names the video folder (a git worktree has none of its own).

The videos are game footage and stay out of the repo (see samples.json for the same rule): put
them in tests/samples/videos/ under the names in videos.json.

    .venv\\Scripts\\python.exe scripts\\bench_video_ocr.py
    .venv\\Scripts\\python.exe scripts\\bench_video_ocr.py v02 v19 --verbose
    .venv\\Scripts\\python.exe scripts\\bench_video_ocr.py --set dev --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
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
from livesubs.engine.ocr.ppocr import LineReader, open_line_reader
from livesubs.engine.ocr.winrt_ocr import WinRtOcr
from livesubs.engine.pipeline import EmptyReadBackoff, read_subtitle
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
    requests: int = 0
    ocr_calls: int = 0
    ocr_pixels: int = 0
    screen_exact: int = 0
    screen_partial: int = 0
    screen_missed: int = 0
    delays: list[float] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    screen_misses: list[str] = field(default_factory=list)
    preprocess_ms: list[float] = field(default_factory=list)
    ocr_ms: list[float] = field(default_factory=list)

    def add(self, other: Score) -> None:
        for name in COUNTS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in ("delays", "misses", "screen_misses", "preprocess_ms", "ocr_ms"):
            getattr(self, name).extend(getattr(other, name))

    def summary(self) -> str:
        delay = f"{statistics.median(self.delays):.2f} s" if self.delays else "-"
        share = 100 * self.exact / self.lines if self.lines else 0.0
        on_screen = 100 * self.screen_exact / self.lines if self.lines else 0.0
        return (
            f"ON SCREEN exact {self.screen_exact}/{self.lines} ({on_screen:.1f} %), partial "
            f"{self.screen_partial}, never read {self.screen_missed} | outputs {self.outputs} "
            f"(requests {self.requests}) | OCR calls {self.ocr_calls} "
            f"({self.ocr_pixels / 1e6:.0f} Mpx) | "
            f"per output: exact {self.exact} ({share:.1f} %), with punctuation "
            f"{self.exact_punct}, partial {self.partial}, never read {self.missed}, phantom "
            f"{self.phantom} | delay p50 {delay} | preprocess {_median(self.preprocess_ms)} ms, "
            f"OCR {_median(self.ocr_ms)} ms per read ({len(self.ocr_ms)} reads)"
        )

    def totals(self) -> dict:
        result = {name: getattr(self, name) for name in COUNTS}
        result["reads"] = len(self.ocr_ms)
        result["delay_p50_s"] = round(statistics.median(self.delays), 3) if self.delays else None
        result["preprocess_ms_p50"] = _median(self.preprocess_ms)
        result["ocr_ms_p50"] = _median(self.ocr_ms)
        return result


COUNTS = (
    "lines",
    "screen_exact",
    "screen_partial",
    "screen_missed",
    "outputs",
    "requests",
    "ocr_calls",
    "ocr_pixels",
    "exact",
    "exact_punct",
    "partial",
    "missed",
    "phantom",
)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


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


@dataclass
class Replay:
    outputs: list[tuple[float, str]] = field(default_factory=list)  # confirmed / upgraded lines
    screens: list[tuple[float, str]] = field(default_factory=list)  # the lines held, joined
    preprocess_ms: list[float] = field(default_factory=list)
    ocr_ms: list[float] = field(default_factory=list)
    requests: int = 0  # outputs that are not repeats (a repeat reuses its translation)
    ocr_calls: int = 0
    ocr_pixels: int = 0


class CountedOcr:
    """The OCR engine, counting calls and pixels: the deterministic cost of reading."""

    def __init__(self, ocr: WinRtOcr, result: Replay) -> None:
        self.ocr, self.result = ocr, result

    def recognize(self, image):
        self.result.ocr_calls += 1
        self.result.ocr_pixels += image.shape[0] * image.shape[1]
        return self.ocr.recognize(image)


def replay(
    path: Path,
    region: NormRect,
    vision: VisionSettings,
    ocr: WinRtOcr,
    scorer: ReadScorer,
    lines: LineReader | None = None,
) -> Replay:
    """Everything the reading loop put out for a video, on its virtual clock."""
    capture = cv2.VideoCapture(str(path))
    fps = capture.get(cv2.CAP_PROP_FPS)
    detector, stabilizer = ChangeDetector(), Stabilizer(scorer=scorer.score)
    backoff = EmptyReadBackoff()
    result = Replay()
    counted = CountedOcr(ocr, result)
    now, index, shown, frame, held = 0.0, -1, -1, None, ""
    while True:
        now += stabilizer.fast_reread_s if stabilizer.awaiting_confirmation else TICK_S
        while index < int(now * fps):
            if not capture.grab():
                return result
            index += 1
        if index == shown and not stabilizer.awaiting_confirmation:
            continue  # nothing new on screen
        if index != shown:
            frame = capture.retrieve()[1]
            shown = index
        r = region.to_pixels(frame.shape[1], frame.shape[0])
        started = time.perf_counter()
        crop = frame[r.y : r.bottom, r.x : r.right]
        processed = preprocess(crop, vision)
        result.preprocess_ms.append(1000 * (time.perf_counter() - started))
        pixels = cv2.countNonZero(processed.mask)
        decision = stabilizer.update(detector.update(processed.mask), pixels, now)
        events = list(decision.events)
        idle = not stabilizer.current and not stabilizer.awaiting_confirmation
        if decision.read and not backoff.skip(pixels, now, idle=idle):  # as the pipeline does
            started = time.perf_counter()
            _, _, rows = read_subtitle(counted, processed, scorer, lines, crop)
            result.ocr_ms.append(1000 * (time.perf_counter() - started))
            backoff.on_read(bool(rows), pixels, now, idle=idle)
            now += OCR_COST_S
            events += stabilizer.on_read(rows, now)
        for event in events:
            if isinstance(event, LineConfirmed | LineUpgraded):
                result.outputs.append((now, event.text))
                result.requests += not getattr(event, "repeat", False)
        if (screen := " ".join(stabilizer.current)) != held:
            held = screen
            result.screens.append((now, screen))


def score_screens(lines: list[list], screens: list[tuple[float, str]], result: Score) -> None:
    """Each line against what was held on screen around its time."""
    ends = [t for t, _ in screens[1:]] + [float("inf")]
    states = [
        (t, end, text, words(text, strip_watermark=True))
        for (t, text), end in zip(screens, ends, strict=True)
    ]
    for start, end, text in lines:
        line, best, closest = words(text), 0.0, ""
        for shown_from, shown_to, shown, held in states:
            if shown_from > end + WINDOW_S or shown_to < start - WINDOW_S:
                continue
            exact = contains(line, held) or contains(line, words(shown))
            share = 1.0 if exact else min(recall(line, held), 0.99)
            if share > best:
                best, closest = share, shown
        if best == 1.0:
            result.screen_exact += 1
        elif best >= 0.5:
            result.screen_partial += 1
            result.screen_misses.append(f"{start:7.1f}  partial  {text}  ==>  {closest}")
        else:
            result.screen_missed += 1
            result.screen_misses.append(f"{start:7.1f}  MISSED   {text}  ==>  {closest}")


def score(lines: list[list], played: Replay) -> Score:
    outputs = played.outputs
    result = Score(
        lines=len(lines),
        outputs=len(outputs),
        requests=played.requests,
        ocr_calls=played.ocr_calls,
        ocr_pixels=played.ocr_pixels,
        preprocess_ms=played.preprocess_ms,
        ocr_ms=played.ocr_ms,
    )
    score_screens(lines, played.screens, result)
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
            if contains(line, out_words) or contains(line, words(output)):
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
    parser.add_argument("--set", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--json", type=Path, help="write totals and misses to this file")
    args = parser.parse_args()

    spec = json.loads((SAMPLES / "videos.json").read_text(encoding="utf-8"))
    x, y, width, height = spec["region"]
    region = NormRect(x=x, y=y, width=width, height=height)
    vision = VisionSettings(text_colors=spec["text_colors"])
    checker = open_spell_checker()
    scorer = ReadScorer(checker.misspelled if checker else None)
    ocr = WinRtOcr()
    lines = open_line_reader()  # LIVESUBS_NO_PPOCR=1: Windows OCR only
    print(f"OCR: {'PP-OCR line reader, Windows as fallback' if lines else 'Windows OCR'}")
    folder = Path(os.environ.get("LIVESUBS_VIDEOS") or SAMPLES / "videos")
    holdout = set(spec.get("holdout", ()))
    total, report = Score(), {}
    for video in spec["videos"]:
        if args.videos and video["name"] not in args.videos:
            continue
        if args.set != "all" and (video["name"] in holdout) != (args.set == "holdout"):
            continue
        path = folder / video["file"]
        if not path.is_file():
            print(f"{video['name']}: {video['file']} not found, skipped")
            continue
        started = time.perf_counter()
        result = score(video["lines"], replay(path, region, vision, ocr, scorer, lines))
        total.add(result)
        print(f"{video['name']}: {result.summary()}  [{time.perf_counter() - started:.0f} s]")
        report[video["name"]] = {**result.totals(), "not_exact_on_screen": result.screen_misses}
        if args.verbose:
            print("\n".join(f"    {m}" for m in result.screen_misses))
    if not total.lines:
        sys.exit("no videos found")
    print(f"\n{args.set}: {total.summary()}")
    if args.json:
        report = {"set": args.set, "total": total.totals(), "videos": report}
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
