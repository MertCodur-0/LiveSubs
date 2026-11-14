"""How often does the first read of a new line equal what is confirmed?"""

import json, os, sys
from pathlib import Path

import cv2

sys.path.insert(0, "scripts")
import bench_video_ocr as b
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

TICK = 1 / 30


def run(path, region, vision, ocr, scorer, lines):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS)
    det, st, back = ChangeDetector(), Stabilizer(scorer=scorer.score), EmptyReadBackoff()
    c = dict(new=0, hit=0, upgrade=0, upgrade_hit=0, started=0, wasted=0)
    misses = []
    now, index, shown, frame, prev = 0.0, -1, -1, None, None
    while True:
        now += st.fast_reread_s if st.awaiting_confirmation else TICK
        while index < int(now * fps):
            if not cap.grab():
                return c, misses
            index += 1
        if index == shown and not st.awaiting_confirmation:
            continue
        if index != shown:
            frame = cap.retrieve()[1]
            shown = index
        r = region.to_pixels(frame.shape[1], frame.shape[0])
        crop = frame[r.y : r.bottom, r.x : r.right]
        p = preprocess(crop, vision)
        px = cv2.countNonZero(p.mask)
        d = st.update(det.update(p.mask), px, now)
        events = list(d.events)
        idle = not st.current and not st.awaiting_confirmation
        if d.read and not back.skip(px, now, idle=idle):
            _, _, rows = read_subtitle(ocr, p, scorer, lines, crop)
            back.on_read(bool(rows), px, now, idle=idle)
            now += b.OCR_COST_S
            events += st.on_read(rows, now)
        claimed = False
        for e in events:
            if isinstance(e, LineConfirmed) and not e.repeat:
                c["new"] += 1
                if e.text == prev and not claimed:
                    c["hit"] += 1
                    claimed = True
                else:
                    misses.append((prev, e.text))
            elif isinstance(e, LineUpgraded):
                c["upgrade"] += 1
                if e.text == prev and not claimed:
                    c["upgrade_hit"] += 1
                    claimed = True
        exp = st.expected
        if exp != prev:
            if prev is not None and not claimed:
                c["wasted"] += 1
            if exp is not None:
                c["started"] += 1
            prev = exp


spec = json.loads((b.SAMPLES / "videos.json").read_text(encoding="utf-8"))
x, y, w, h = spec["region"]
region = NormRect(x=x, y=y, width=w, height=h)
vision = VisionSettings(text_colors=spec["text_colors"])
checker = open_spell_checker()
scorer = ReadScorer(checker.misspelled if checker else None)
ocr, lines = WinRtOcr(), open_line_reader()
total = {}
for v in spec["videos"]:
    path = b.SAMPLES / "videos" / v["file"]
    if not path.is_file():
        continue
    c, misses = run(path, region, vision, ocr, scorer, lines)
    print(v["name"], c, flush=True)
    for prev, text in misses[:4]:
        print("     ", repr(prev), "->", repr(text))
    for k, n in c.items():
        total[k] = total.get(k, 0) + n
print("TOTAL", total)
