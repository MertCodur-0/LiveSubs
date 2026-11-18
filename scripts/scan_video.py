"""List every line the reading loop would send for translation from a video, with its time.

For long gameplay recordings without a ground truth (``bench_video_ocr.py`` needs one): play the
video through the same loop as the benchmark (region, preprocessing, change detection,
stabilizer, PP-OCR / Windows OCR, on a virtual clock) and print each confirmed or upgraded line.
Garbled or cut lines then stand out when read through, and ``--reads`` keeps every OCR read
(with each segment's confidence) as JSON lines, to see why. Region and colours default to the
benchmark's (videos.json: GTA VI's subtitle area).

Long videos: split them with ``--jobs`` (each job plays its own stretch; a line crossing a
boundary may show twice).

    .venv\\Scripts\\python.exe scripts\\scan_video.py game.mp4 --jobs 8 > lines.tsv
    .venv\\Scripts\\python.exe scripts\\scan_video.py game.mp4 --start 1780 --end 1820 \\
        --reads r.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2

from livesubs.core.geometry import NormRect
from livesubs.core.profiles import VisionSettings
from livesubs.engine.ocr.ppocr import open_line_reader
from livesubs.engine.ocr.winrt_ocr import WinRtOcr
from livesubs.engine.pipeline import EmptyReadBackoff, read_subtitle
from livesubs.engine.text.quality import ReadScorer
from livesubs.engine.text.stabilizer import LineConfirmed, LineUpgraded, Stabilizer
from livesubs.engine.vision.change import ChangeDetector
from livesubs.engine.vision.preprocess import preprocess
from livesubs.platform.spellcheck import open_spell_checker

SAMPLES = Path(__file__).resolve().parent.parent / "tests" / "samples"
TICK_S = 0.1  # as bench_video_ocr.py
OCR_COST_S = 0.02


def scan(path: str, start: float, end: float, reads_path: str | None) -> list[tuple]:
    """``[(time s, "new" | "upgrade" | "repeat", text)]`` for ``start``-``end`` of the video."""
    spec = json.loads((SAMPLES / "videos.json").read_text(encoding="utf-8"))
    x, y, width, height = spec["region"]
    region = NormRect(x=x, y=y, width=width, height=height)
    vision = VisionSettings(text_colors=spec["text_colors"])
    checker = open_spell_checker()
    scorer = ReadScorer(checker.misspelled if checker else None)
    ocr, lines = WinRtOcr(), open_line_reader()
    segments: list[tuple[str, float]] = []
    if lines is not None:
        read_line = lines.read_line

        def logged(image):
            text, confidence = read_line(image)
            segments.append((text, round(confidence, 3)))
            return text, confidence

        lines.read_line = logged
    reads: list[str] = []
    capture = cv2.VideoCapture(path)
    fps = capture.get(cv2.CAP_PROP_FPS)
    capture.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    index = round(capture.get(cv2.CAP_PROP_POS_FRAMES)) - 1
    detector, stabilizer = ChangeDetector(), Stabilizer(scorer=scorer.score)
    backoff = EmptyReadBackoff()
    found: list[tuple] = []
    now, shown, frame = start, -1, None
    while now < end:
        now += stabilizer.fast_reread_s if stabilizer.awaiting_confirmation else TICK_S
        while index < int(now * fps):
            if not capture.grab():
                break
            index += 1
        if index < int(now * fps):  # the video ended
            break
        if index == shown and not stabilizer.awaiting_confirmation:
            continue
        if index != shown:
            frame = capture.retrieve()[1]
            shown = index
        r = region.to_pixels(frame.shape[1], frame.shape[0])
        crop = frame[r.y : r.bottom, r.x : r.right]
        processed = preprocess(crop, vision)
        pixels = cv2.countNonZero(processed.mask)
        decision = stabilizer.update(detector.update(processed.mask), pixels, now)
        events = list(decision.events)
        idle = not stabilizer.current and not stabilizer.awaiting_confirmation
        if decision.read and not backoff.skip(pixels, now, idle=idle):
            segments.clear()
            _, _, rows = read_subtitle(ocr, processed, scorer, lines, crop)
            backoff.on_read(bool(rows), pixels, now, idle=idle)
            if reads_path:
                record = {"t": round(now, 2), "px": pixels, "segments": segments,
                          "rows": [row.text for row in rows]}  # fmt: skip
                reads.append(json.dumps(record, ensure_ascii=False) + "\n")
            now += OCR_COST_S
            events += stabilizer.on_read(rows, now)
        for event in events:
            if isinstance(event, LineUpgraded):
                found.append((round(now, 2), "upgrade", event.text))
            elif isinstance(event, LineConfirmed):
                found.append((round(now, 2), "repeat" if event.repeat else "new", event.text))
    if reads_path:
        Path(reads_path).write_text("".join(reads), encoding="utf-8")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("video")
    parser.add_argument("--start", type=float, default=0.0, help="seconds")
    parser.add_argument("--end", type=float, help="seconds (default: the end)")
    parser.add_argument("--jobs", type=int, default=1, help="processes, each a stretch")
    parser.add_argument("--reads", help="write every OCR read to this JSON lines file (1 job)")
    args = parser.parse_args()
    capture = cv2.VideoCapture(args.video)
    if not capture.isOpened():
        sys.exit(f"cannot open {args.video}")
    length = capture.get(cv2.CAP_PROP_FRAME_COUNT) / capture.get(cv2.CAP_PROP_FPS)
    capture.release()
    end = min(args.end or length, length)
    if args.jobs <= 1:
        found = scan(args.video, args.start, end, args.reads)
    else:
        step = (end - args.start) / args.jobs
        stretches = [(args.start + i * step, args.start + (i + 1) * step) for i in range(args.jobs)]
        with ProcessPoolExecutor(args.jobs) as pool:
            parts = pool.map(
                scan, *zip(*[(args.video, a, b, None) for a, b in stretches], strict=True)
            )
            found = [line for part in parts for line in part]
    sys.stdout.reconfigure(encoding="utf-8")
    for time_s, kind, text in found:
        print(f"{time_s:.2f}\t{kind}\t{text}")


if __name__ == "__main__":
    main()
