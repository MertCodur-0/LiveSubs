# CLAUDE.md — LiveSubs project memory

Living document: project context, decisions, plan and progress. **Update it at every step.**

Sources (Turkish): `PROMPT.md` = the user's project brief (requirements); `plan.md` = the approved
plan (its final section "Onaylanan düzeltme ve eklemeler" overrides the rest). Everything relevant
from both is reflected here.

## What LiveSubs is

A Windows desktop app that reads English subtitles from any game on screen (PC games, PS5 Remote
Play, emulators, capture cards), translates them to Turkish **fully locally** (Ollama) in real time,
and shows the translation in a movable overlay. Portfolio project (GitHub + LinkedIn): code quality,
structure and README must be professional.

**Latency is the top priority.** Capture, OCR and translation never run on the UI thread/process.
It must be a real app: double-click the .exe, no terminal. The app itself stays light (the game may
share the PC).

## Environment

- Windows 11, i7-14700HX, RTX 4070 Laptop (8 GB VRAM), 32 GB RAM. The game may run on the same PC.
- **Python 3.13** (3.15 breaks winrt/numpy builds). `py` defaults to 3.15 — always use the venv:
  `.\.venv\Scripts\python.exe`.
- Ollama installed at `%LOCALAPPDATA%\Programs\Ollama`, API `http://127.0.0.1:11434`.
- en-US Windows OCR is currently installed on the dev machine (`OcrEngine` reports `en-US`, `tr`);
  detection + install guidance must still exist for other users.

## Conventions

- UI text: **Turkish**, centralized (`livesubs/i18n`) so it can be translated later.
- Code, comments, commit messages: **English**. README.md English + README.tr.md.
- License MIT. Python 3.13 venv + pip, `pyproject.toml` (src layout).
- Lint/format: `ruff check .` and `ruff format .` (line length 100). Tests: `pytest`.
- Tests needing Windows OCR are marked `@pytest.mark.ocr`; tests hitting a live Ollama are marked
  `@pytest.mark.ollama`. CI runs `pytest -m "not ocr and not ollama"`.
- One meaningful commit per step (or a few). Commit identity is repo-local, using the GitHub
  noreply address (keeps the personal e-mail out of public history).

## Dependencies

App: PySide6-Essentials (not the `PySide6` meta package — it drags in WebEngine), numpy,
opencv-python (required by `windows-capture`; do **not** also install `-headless`, they conflict),
windows-capture (WGC), mss (fallback), winrt-* (Media.Ocr, Graphics.Imaging, Storage.Streams,
Globalization, Foundation), httpx, pydantic, rapidfuzz, qrcode, platformdirs.
Dev: pytest, pytest-qt, respx, ruff, pyinstaller.

## Architecture

Two processes so the GIL can never let the UI stall the pipeline.

```
UI process (PySide6)                                  Engine process (multiprocessing)
  MainWindow / tabs / tray / hotkeys                    Capture thread  -> FrameSource
  Overlay, RegionFrame, Inspector (F8)    commands ->   Vision: crop, 2x, color mask, change detect
  WebServer (SSE) + QR                    <- events     OCR thread: WinRT OcrEngine
  ModelManager -> Ollama REST                           Text: normalize, filters, 2-frame stabilizer
                                                        TranslationOrchestrator: GPU lane, CPU lane
```

- IPC: `multiprocessing.Queue`, typed dataclass messages (`core/messages.py`).
  Debug frames (raw/preprocessed) are only sent while calibration mode is on, throttled to ~5 fps.
- Region stored **normalized (0–1) to the source's frame space** (window client area / monitor /
  video frame), converted to physical pixels with DPI awareness.
- Capture FPS configurable, default 10. WGC yellow border disabled where Windows 11 allows it.
- Preprocessing: crop -> 2x upscale (`INTER_CUBIC`) -> HSV/color-distance mask for the configured
  subtitle colors -> optional outline support (never *require* a dark text edge) -> morphological
  cleanup -> connected-component filter (size/aspect) -> black text on white. Every step can be
  toggled, every threshold is tunable.
- Change detection compares only the **text mask** (downscaled; pixel diff / IoU), never raw
  pixels, so background motion never triggers. Empty mask = subtitle gone; the overlay clears after
  a configurable delay.
- Filters: letter ratio, minimum length, vowel presence, repeated junk characters; a repeat of the
  last sent line is dropped.
- Stabilizer: same normalized text (rapidfuzz >= 0.9) in 2 consecutive reads before translating; on
  change a fast re-read is scheduled (~50 ms) instead of waiting for the next tick.
- Cache: in-memory LRU + SQLite, key = (model, profile, normalized text); context not in key.
- Latency budget (8B on GPU): capture+preprocess ~10 ms, OCR 30–80 ms, confirm ~50 ms,
  translate 250–600 ms => ~0.4–0.8 s on-screen to overlay. `scripts/bench_pipeline.py` measures it.

### Capture source abstraction (`engine/capture/base.py`)

Designed from the start for three source kinds; video device lands in phase 2.

- `SourceKind`: `WINDOW` (WGC by HWND), `MONITOR` (WGC by monitor; mss fallback),
  `VIDEO_DEVICE` (capture card via OpenCV `CAP_MSMF`, `CAP_DSHOW` fallback).
- `FrameSource` protocol: `open()`, `close()`, `latest_frame() -> Frame | None` (newest-wins, never
  queues stale frames), `info` (kind, frame size, nominal fps), `screen_rect() -> Rect | None`
  (where the frame lives on the desktop; `None` for video devices — no screen mapping).
- `Frame`: BGR(A) ndarray + monotonic timestamp + frame-space size.
- Typed errors: `SourceUnavailableError`, `SourceBusyError` (device held by another app such as OBS
  -> UI suggests using the OBS preview window as a *window* source), `SourceLostError`.
- Health signals: black-frame detector emits `BlackFrameWarning` (window/monitor: protected content;
  video device: HDCP hint — PS5: Settings -> System -> HDMI -> disable HDCP).
- Video device enumeration: device name + supported resolution/fps list (via Media Foundation;
  candidate libs: WinRT `Windows.Devices.Enumeration` or `cv2-enumerate-cameras` — decide in phase 2).
  Frames are read directly, no preview window. In calibration mode the current frame is shown in the
  Inspector for region selection (there is no on-screen frame to draw a region box on).

### Translation

- `TranslationBackend` protocol: `translate(request, cancel) -> TranslationResult`. First impl:
  `OllamaBackend` (httpx, `/api/chat` streaming, `think: false`, `options.num_ctx` default 2048,
  `options.num_gpu`, `keep_alive`). Cancel = close the stream. More backends (llama.cpp, own
  fine-tune) plug in later.
- Orchestrator: **GPU lane** (one in flight, newest-wins pending slot). **CPU quality lane**
  (default off): after the GPU result, if the subtitle is still on screen, run the quality model
  with `num_gpu: 0` (default `qwen3:30b-a3b`) and replace the text; cancel when the subtitle changes.
  When disabled the CPU model is never loaded (unload with `keep_alive: 0`). CPU model and its
  `num_gpu` are chosen in Settings; GPU lane `num_gpu` optional (None = Ollama decides).
- Prompt: slang/swearing -> natural Turkish, don't translate names/places, output only the
  translation; per-profile extra context + glossary; last N lines (default 3) as context.
- Every result carries model, lane (GPU/CPU), OCR ms and translation ms for the live feed.

### Model management

- `/api/tags`, `/api/show`, `/api/ps`, `/api/pull` (stream, cancel), `/api/delete` (confirm).
- VRAM estimate = weights + KV cache (layers, kv heads, head dim from `model_info`, × num_ctx) +
  overhead. Label **fits / borderline / doesn't fit** against the budget
  `total VRAM − active profile's game VRAM reserve` (see Profiles). Runtime check: `/api/ps`
  `size_vram < size` -> warning.
- Recommended models live in `ollama/recommended_models.json` (verify tags on ollama.com):
  GPU: `qwen3:8b`, `aya-expanse:8b`, `gemma4:12b-it-qat` (7.2 GB, verified 2026-10-21 — borderline
  on PC-game profiles), `translategemma:4b`; CPU: `qwen3:30b-a3b`.
- Test/compare: fixed set of sample game lines through two models side by side with ms.
- Two models loaded at once needs `OLLAMA_MAX_LOADED_MODELS >= 2` — check and explain.

### Config and profiles — `%APPDATA%\LiveSubs\config.json` + `profiles\*.json`

Source (kind + identifier), subtitle region, color/threshold settings, overlay position & style,
overlay display target, translation context/glossary, **game VRAM reserve**. Last used profile is
loaded at startup. Pydantic models with a schema version.

- **Game VRAM reserve is per profile**: default ~0.5 GB when the game does not run on this PC
  (Remote Play, capture card — only video decode), ~2 GB+ for PC games. Model VRAM labels are
  computed against the **active** profile.
- **Overlay display target** (mainly for video-device sources, where the game monitor may not be
  connected to the PC): a specific monitor on this PC, or web/phone output only.

### Overlay

Frameless, always-on-top, translucent `QWidget`; click-through via `WS_EX_LAYERED |
WS_EX_TRANSPARENT | WS_EX_NOACTIVATE` (off in calibration mode). Excluded from capture with
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` — also applied to the RegionFrame and Inspector.
Text drawn with `QPainterPath` (outline/shadow). Styles incl. "Vice" (pink/cyan outline);
readability first. Optional small English original underneath. Adjustable: position, size, font,
font size, text color, outline/shadow, background opacity, max lines shown.

### Main window

Source + profile pick, Ollama status (connected, model loaded), names of the models in use, big
Start/Stop. Live feed: English line, Turkish line, model, lane, OCR ms + translation ms. Tabs:
Live / Models / Profiles / Settings (fps, thresholds, language, theme, port, hotkeys -> config).
Models tab shows what to do when Ollama is missing/not running; pull shows progress, downloaded /
remaining size, cancel. Minimizes to tray.

- **Recording mode** (setting, default off): disables `WDA_EXCLUDEFROMCAPTURE` so OBS/screenshots
  see the overlay (README GIF, LinkedIn video). When on, warn if the overlay rectangle intersects the
  subtitle region on screen (OCR would read our own translation).
- Exclusive fullscreen detection via `SHQueryUserNotificationState` -> suggest borderless windowed.

### Other

- Global hotkeys via Win32 `RegisterHotKey` + Qt native event filter (no low-level hooks:
  anti-cheat friendly, no admin). Defaults: F8 calibration, overlay show/hide, pause/resume.
- Web output: stdlib `ThreadingHTTPServer` + Server-Sent Events, QR via `qrcode`; firewall note
  in the UI. Page uses the Screen Wake Lock API so the phone screen stays on; if unsupported, show a
  "disable screen timeout" hint. Vice-themed, large readable text, works in portrait and landscape.
- Startup checks: Ollama installed/running, models present, en-US OCR (button runs
  `Add-WindowsCapability -Online -Name Language.OCR~~~en-US~0.0.1.0` elevated).
- Theme: `ui/theme/base.qss` template + token files (`vice.json` default, `dark.json`).
  Fonts (OFL, Google Fonts): Monoton or Neonderthaw (logo), Audiowide (headings), Inter (body);
  font licenses listed in the README. Palette: dark purple/night-blue base; neon pink/magenta,
  cyan, sunset orange accents; pink -> orange -> purple gradients. Animations light.
  Own SVG icon/header (palms + sunset). No Rockstar/GTA logos, fonts, art or names.
  Glow (`QGraphicsDropShadowEffect`) only on logo and Start button — it is expensive.
- Packaging: PyInstaller **onedir** zipped for GitHub Releases (onefile unpacks on every start).
  Do an early smoke build — winrt / windows-capture native modules may need spec tweaks.

## Project layout (target)

```
assets/        icon.svg, header.svg, fonts/ (+ OFL licenses)
src/livesubs/
  app.py, __main__.py, i18n/
  core/        config.py, profiles.py, paths.py, jsonio.py, geometry.py, messages.py
  engine/      process.py, pipeline.py
    capture/   base.py, wgc.py, mss_fallback.py, video_device.py (phase 2)
    vision/    preprocess.py, change.py
    ocr/       winrt_ocr.py, language.py
    text/      normalize.py, filters.py, stabilizer.py
    translate/ base.py, ollama.py, orchestrator.py, prompt.py, cache.py
  ollama/      client.py, catalog.py, vram.py, recommended_models.json
  platform/    win32.py
  ui/          main_window.py, overlay.py, region_frame.py, inspector.py, tray.py,
               setup_check.py, tabs/, theme/
  web/         server.py, static/
tests/         samples/ (+ samples.json), test_*.py
scripts/       build_exe.ps1, livesubs.spec, bench_pipeline.py
```

## Plan and progress

Phase 1 — working core (source -> region -> OCR -> GPU translation -> overlay)
- [x] 1. Skeleton: pyproject, ruff/pytest, LICENSE, CLAUDE.md, README stubs, CI
- [x] 2. Platform + config: Win32 helpers (window list, DPI, affinity, click-through), config/profile models
- [ ] 3. Capture: `FrameSource` abstraction (all three kinds designed), WGC window + monitor, bench script
- [ ] 4. Vision: preprocessing + text-mask change detection + tests
- [ ] 5. OCR: WinRT wrapper + language check + tests
- [ ] 6. Text: normalize, filters, 2-frame stabilizer + tests
- [ ] 7. Translation: Ollama backend, prompt, cache, orchestrator (GPU lane) + mocked-HTTP tests
- [ ] 8. Engine process + IPC
- [ ] 9. Minimal UI: source pick, drag-select region, Start/Stop, live feed, overlay  => core done

Phase 2 — features
- [ ] 10. Calibration mode (F8): movable region frame, Inspector, overlay move/resize; hotkeys
- [ ] 11. Profiles (incl. per-profile VRAM reserve, overlay display target)
- [ ] 12. Models tab (list, VRAM labels vs active profile, pull/cancel, delete, recommended, test/compare)
- [ ] 13. CPU quality mode
- [ ] 14. Settings tab (incl. recording mode + overlap warning), startup checks, tray, fullscreen warning
- [ ] 15. Video device source (capture card): enumerate, resolution/fps, busy/HDCP errors, inspector region pick
- [ ] 16. Vice theme, fonts, SVG icon/header, overlay styles
- [ ] 17. Web output + QR (+ Screen Wake Lock)

Phase 3 — release
- [ ] 18. PyInstaller onedir build script (+ earlier smoke build)
- [ ] 19. README (architecture diagram, install, GIF slot), screenshots

## Known risks

- DPI scaling: Qt logical coordinates vs. physical capture pixels — handle from step 2, test it.
- Two models loaded at once needs `OLLAMA_MAX_LOADED_MODELS >= 2` (app checks and explains).
- CPU model RAM: `qwen3:30b-a3b` Q4 is ~18–19 GB; borderline on 32 GB next to a game — say so in
  Settings.
- PyInstaller + winrt / windows-capture native modules — early smoke build at the end of phase 1.

## Decision log

- 2026-10-21: Plan approved with changes: per-profile game VRAM reserve; recording mode;
  `gemma4:12b-it-qat` in recommended list; video-device (capture card) source kind (phase 2,
  abstraction designed now).
- 2026-10-21: `opencv-python` (not headless) because `windows-capture` depends on it.
- 2026-10-21: `PySide6-Essentials` instead of `PySide6` to keep the build small.
- 2026-10-21 (step 2): `SourceKind` + source specs live in `core/profiles.py` (capture imports
  them). Window sources are re-found by exe name first, title second (`best_window_match`).
  "Game runs on this PC" is inferred (video device / streaming-client exe such as
  `RemotePlay.exe` -> no) and overridable; the VRAM reserve defaults from it (2.0 / 0.5 GB).
  Settings models validate on assignment; files carry `schema_version`, newer versions are refused,
  unknown fields ignored, a corrupt `config.json` is kept as `config.json.bad`. `LIVESUBS_HOME`
  redirects all app files (tests / portable). Click-through only adds `WS_EX_LAYERED` (+ alpha)
  when missing — Qt's own layered windows use `UpdateLayeredWindow`, which
  `SetLayeredWindowAttributes` would break.
