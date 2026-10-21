# CLAUDE.md — LiveSubs project memory

Living document: project context, decisions, plan and progress. **Update it at every step.**

**At the start of every session, read [PROMPT.md](PROMPT.md) and [PLAN.md](PLAN.md)** (both in
Turkish), then this file's "Plan and progress" and "Decision log", and continue from the first
unchecked step.

- `PROMPT.md` — the user's original project brief (requirements).
- `PLAN.md` — the architecture and plan approved by the user.
- Precedence when they conflict: PLAN.md's final section "Onaylanan düzeltme ve eklemeler" >
  rest of PLAN.md > PROMPT.md. Later decisions recorded in the decision log below refine all of
  them (e.g. `opencv-python` instead of `-headless`).

Everything relevant from both files is reflected below; keep it in sync.

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
- Two displays, **mixed DPI** — test region/overlay mapping here:
  `\\.\DISPLAY1` external monitor 1920x1080 @ 100 %, 179 Hz, primary, at (0, 0);
  `\\.\DISPLAY2` laptop panel 2560x1440 @ 150 %, at (1920, -164).
  Only the RTX 4070 is active (no Intel iGPU listed; MUX in dGPU mode), so both displays are on
  the same adapter — no cross-adapter capture copies.
- **User's primary scenario (updated 2026-10-21):** the game runs on a **console (PS5), not on
  this PC**; its picture reaches the PC through a **capture card** (+ HDMI splitter, or the card's
  passthrough). The PC only runs Windows + LiveSubs, so all of it goes to translation (see
  decision log). Where the user watches the game and the translation is **undecided**; all three
  must work: (1) game watched on the PC through a low-latency fullscreen preview of the capture
  card with the overlay on top — needs a preview window of our own (open item; OBS / the card's
  app as a *window* source meanwhile); (2) game on TV/monitor via splitter, translation on a PC
  screen next to it (overlay display target); (3) translation on a phone (web output).
  Earlier assumption, still valid for window/monitor sources: main window on the laptop panel,
  Inspector by default on a monitor other than the game's, window positions remembered, global
  hotkeys. Benchmarks run on DISPLAY1.
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
  `@pytest.mark.ollama`; tests capturing the real desktop are marked `@pytest.mark.capture`.
  CI runs `pytest -m "not ocr and not ollama and not capture"`. Capture tests target
  `tests/helpers/flicker_window.py` (a window in a *separate* process; see WGC pitfalls).
- Scratch scripts never get stdlib module names (a `bisect.py` once shadowed the stdlib).
- `cv2.imread`/`imwrite` fail on non-ASCII paths on Windows (e.g. "Ekran görüntüsü"): use
  `np.fromfile` + `cv2.imdecode` / `cv2.imencode(...)[1].tofile(...)` everywhere.
- Sample screenshots (`tests/samples/*.png`) are game footage: git-ignored, never published;
  `samples.json` (region, vision overrides, expected text, `reference_tr`) is committed and sample
  tests skip when an image is missing (CI). Loader: `tests/sample_data.py`.
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
  Commands: `StartPipeline(profile, ollama, profile_id)`, `StopPipeline`, `SetRegion`, `Shutdown`.
  Events: `StateChanged(IDLE/STARTING/WARMING_UP/RUNNING/STOPPED)`, `EngineError(kind, message,
  fatal)` (UI maps `kind` to Turkish text), `FrameInfo`, `SubtitleRead(line_id, text, ocr_ms,
  repeat, upgrade)` (English first), `SubtitleTranslated(...)`, `SubtitleCleared`.
- Engine (`engine/process.py`): `EngineProcess` (UI side, spawn context, `send`/`poll`/
  `wait_event`/`shutdown`) and `engine_main` (logs to `log_dir()/engine.log`). `engine/pipeline.py`:
  one thread runs capture -> region -> preprocess -> change -> stabilizer -> **OCR inline** (10-20 ms,
  no separate OCR thread needed) and hands lines to the orchestrator. Loop at profile fps, 50 ms
  while a candidate awaits confirmation; unchanged frames skipped. Reading starts at once; the
  model warms up on a side thread (WARMING_UP), the newest line read meanwhile is translated when
  it is ready (RUNNING) — a real run lost the first subtitles during a 7.5 s warm-up before.
  Without Ollama/model: non-fatal error, English-only mode. Source/OCR missing or source lost:
  fatal. Region changes reset detection. Source opener and backend factory are injectable (tests
  play sample screenshots as a scripted video).
- Region stored **normalized (0–1) to the source's frame space** (window client area / monitor /
  video frame), converted to physical pixels with DPI awareness.
- Capture FPS configurable, default 10. WGC yellow border disabled where Windows 11 allows it.
- Preprocessing (`engine/vision/preprocess.py`), all at native resolution until the upscale:
  per subtitle color a *closeness* map (255 - max per-channel distance) -> text = closeness within
  `color_tolerance` AND **local contrast** (white top-hat of the closeness map >=
  `min_local_contrast`, kernel `local_contrast_kernel_px`; Otsu on gray when color mask is off)
  -> connected-component filter (area between `min_component_px` and `max_component_fraction`)
  -> 2x upscale of the mask (linear + threshold) -> 2x2 opening -> OCR image = inverted mask.
  Local contrast replaced the first "outline support" (a dark pixel nearby): it failed on faint
  caption boxes over bright ground, while top-hat needs no dark edge at all — only that strokes
  are thinner than the kernel and stand out from their surroundings. Every step can be toggled,
  every threshold is tunable. Cost: 1.1–2.2 ms for a 1066x106 sample region; live 1536x216 at
  1080p p50 5.0 ms / p95 5.8 ms.
- Change detection (`engine/vision/change.py`) compares only the **text mask**: shrunk to a
  96-px-wide thumbnail, IoU >= 0.75 = same line; coverage < 0.1 % = empty. Background motion never
  triggers. Empty mask = subtitle gone (`CLEARED`); the overlay clears after a configurable delay.
- OCR (`engine/ocr/winrt_ocr.py`): `WinRtOcr.recognize(image) -> OcrResult` (lines with boxes,
  `text` = lines joined by spaces, `elapsed_ms`). Gray8 `SoftwareBitmap` filled through a WinRT
  `Buffer` memoryview; `recognize_async(...).get()` blocks the OCR thread (no asyncio); works
  from worker threads; images over `max_image_dimension` (10000) are downscaled, boxes mapped
  back. 5-13 ms per sample region, live p50 17 / p95 24 ms. `engine/ocr/language.py`: installed
  languages, `best_english` (en-US, else any en-*), elevated install via `ShellExecuteW runas`.
- OCR accuracy on the 18 samples (fuzz ratio vs expected, typographic quotes folded):
  preprocessed mean 99.5, 15/18 exact, min 96.3; raw gray 96.2 / 12 / 64.3; raw 2x 97.0 / 13 /
  72.5. Remaining misses: 200236 "What-should", "youir" (road junk); 200302 "You" missing.
- Text (`engine/text/`): `normalize.clean` = what is shown and translated (NFC, typographic
  quotes/dashes/ellipsis to ASCII, lone "|"/"l" before a word -> "I", edge junk stripped, single
  spaces); `normalize.key` = comparisons and cache key (casefold, accents and punctuation dropped).
  `filters.check` -> `Verdict(reason)`: empty, < 2 letters, repeated junk runs, letter ratio
  < 0.5, a vowel-less letter run of 5+ ("Hmm"/"Shh" pass); reasons are shown in the Inspector.
- Stabilizer (`engine/text/stabilizer.py`, pure logic, caller passes `time.monotonic()`):
  `update(change, text_pixels, now) -> Decision(read, event)` and `on_read(text, now) -> event`.
  A new line needs 2 agreeing reads (rapidfuzz ratio of keys >= 90; latest read wins) ->
  `LineConfirmed(text, repeat)`; `repeat` = same as the previous line (reappeared after a flash),
  so its translation is reused. `CHANGED` keeps the old line on screen until the new one is
  confirmed. `CLEARED` -> `LineCleared` (only if a line existed). Fast re-read 50 ms only while a
  candidate awaits its confirming read; otherwise reads every 500 ms, or at once when text pixels
  grow >= 15 % (fade-ins, glyphs uncovered by the moving background).
- **Read upgrades** (user's point: the background keeps moving, so words lost on a bright frame
  show up in later frames): while a line is confirmed, a read that keeps >= 85 % of its words in
  order and adds words (`is_upgrade`), confirmed by a second read, replaces it ->
  `LineUpgraded(text, previous)` (re-translate). Mere variations never replace it (no flicker).
  Static bright scenes stay a limit. Verified end to end on the samples played as a stream: each
  line confirmed exactly once, cleared once.
- Cache: in-memory LRU + SQLite, key = (model, profile, normalized text); context not in key.
- Latency budget (8B on GPU): capture+preprocess ~10 ms, OCR 30–80 ms, confirm ~50 ms,
  translate 250–600 ms => ~0.4–0.8 s on-screen to overlay. `scripts/bench_pipeline.py` measures it.

### Capture source abstraction (`engine/capture/base.py`)

Designed from the start for three source kinds; video device lands in phase 2.

- `SourceKind`: `WINDOW` (WGC by HWND), `MONITOR` (WGC by monitor; mss fallback),
  `VIDEO_DEVICE` (capture card via OpenCV `CAP_MSMF`, `CAP_DSHOW` fallback).
- `FrameSource` protocol: `open()`, `close()`, `latest_frame() -> Frame | None` (newest-wins, never
  queues stale frames; raises `SourceLostError` once gone), `wait_for_frame(newer_than, timeout)`
  (for the fast re-read), `info` (kind, name, backend, max fps), `screen_rect() -> Rect | None`
  (where the frame lives on the desktop; `None` for video devices — no screen mapping).
  `BaseFrameSource` + `LatestFrameSlot` hold the shared plumbing; `factory.open_source(spec)`
  resolves a profile's source (window: exe first, then title; own process excluded) and falls
  back WGC -> mss.
- `Frame`: BGR(A) ndarray (zero-copy view, read-only by convention) + `time.monotonic()` timestamp.
  WGC frames: BGRA, cropped to the window **client area** (WGC itself captures title bar and
  borders: frame = DWM extended frame bounds), stamped with WGC's composition time.
- Capture runs up to `max_fps` (default 30, via `minimum_update_interval`); WGC delivers only on
  content change. The pipeline tick (profile fps, default 10) is separate.

#### WGC pitfalls (windows-capture 2.0.1, reproduced 2026-10-21)

- Starting a WGC session crashes the process (access violation in `start_free_threaded`) when
  (a) a window **of the capturing process** was captured earlier -> `WgcWindowSource` refuses own
  windows; or (b) a **WMI query** ran after an earlier session -> `keep_platform_off_wmi()` sets
  `platform._wmi = None` (stdlib `platform.system()` queries WMI since 3.12 and `mss` calls it).
  Never run WMI (e.g. for GPU/VRAM info) in the engine process; do it in the UI process.
- Measured on DISPLAY1: present -> consumer p95 ~2.4 ms, region crop + BGR copy ~1 ms, first
  session open ~200 ms (WinRT init), later ones ~55 ms. mss (GDI): ~22 ms per 1080p grab.
- mss copies screen pixels: it sees the lock screen while the session is locked
  (`win32.is_session_locked()`, WTS session flags — the OpenInputDesktop/SwitchDesktop trick
  reported "unlocked" under LockApp on Windows 11), and obscured windows.
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

Implementation (step 7, `engine/translate/`):
- `base.py`: `TranslationRequest(text, history: ContextLine(source, translation)..., game_context,
  glossary)`, `TranslationResult(text, model, lane, elapsed_ms, cached)`, errors
  (`BackendUnavailableError`, `ModelNotFoundError`, `TranslationCancelledError`), `CancelToken`
  with `on_cancel` callbacks (the backend closes the HTTP stream from the cancelling thread, so a
  cancel works even while the model is still loading).
- `prompt.py`: English system prompt (rules above); history as real user/assistant turns
  (EN -> TR), which keeps small models from translating the context lines; `clean_output` strips
  `<think>` blocks, "Türkçe:" labels, wrapping quotes, line breaks.
- `ollama.py`: `/api/chat` stream, `think: false`, `num_ctx`, optional `num_gpu`, `num_predict` 200,
  temperature 0, `keep_alive` 30m; `load()`, `unload()` (`keep_alive: 0`) and **`warm_up()`**:
  the first chat after a load took ~4 s (measured twice) even with the model already loaded,
  later ones 100-600 ms — so translation start runs one tiny real translation.
- `cache.py`: LRU (2000) + SQLite WAL (`cache_dir()/translations.sqlite3`), key = (model, profile,
  `normalize.key(text)`), `clear(model)`.
- `orchestrator.py`: GPU lane worker thread; `submit(line_id, text)` cancels the request in flight,
  keeps one pending job (newest wins), answers cache hits at once; a result is published only if
  its sequence number is still the newest (a late answer never overwrites a newer line). History
  (default 3 lines) updated on publish; an upgrade (same `line_id`) replaces its own entry and
  never sees itself as context. Events: `LineTranslated` / `LineFailed`.
- `scripts/compare_models.py`: sample lines (expected EN) through several models next to the
  user's `reference_tr`, with warm-up time, VRAM fit (`/api/ps` size_vram vs size) and latency.
  `bench_pipeline.py` now runs stabilizer + orchestrator and reports **appear -> translated**.

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
    capture/   base.py, wgc.py, mss_fallback.py, factory.py, video_device.py (phase 2)
    vision/    preprocess.py, change.py
    ocr/       winrt_ocr.py, language.py
    text/      normalize.py, filters.py, stabilizer.py
    translate/ base.py, ollama.py, orchestrator.py, prompt.py, cache.py
  ollama/      client.py, catalog.py, vram.py, recommended_models.json
  platform/    win32.py
  ui/          main_window.py, overlay.py, region_frame.py, inspector.py, tray.py,
               setup_check.py, tabs/, theme/
  web/         server.py, static/
tests/         samples/ (+ samples.json), helpers/, test_*.py
scripts/       build_exe.ps1, livesubs.spec, bench_pipeline.py
```

## Plan and progress

Phase 1 — working core (source -> region -> OCR -> GPU translation -> overlay)
- [x] 1. Skeleton: pyproject, ruff/pytest, LICENSE, CLAUDE.md, README stubs, CI
- [x] 2. Platform + config: Win32 helpers (window list, DPI, affinity, click-through), config/profile models
- [x] 3. Capture: `FrameSource` abstraction (all three kinds designed), WGC window + monitor, bench script
- [x] 4. Vision: preprocessing + text-mask change detection + tests
- [x] 5. OCR: WinRT wrapper + language check + tests
- [x] 6. Text: normalize, filters, 2-frame stabilizer + tests
- [x] 7. Translation: Ollama backend, prompt, cache, orchestrator (GPU lane) + mocked-HTTP tests
- [x] 8. Engine process + IPC
- [x] 9. Minimal UI: source pick, drag-select region, Start/Stop, live feed, overlay  => core done
- [ ] 9b. **= step 15, moved up** (capture card is the user's main source): video device source,
  then a low-latency fullscreen preview window for it

Phase 2 — features
- [ ] 10. Calibration mode (F8): movable region frame, Inspector, overlay move/resize; hotkeys
- [ ] 11. Profiles (incl. per-profile VRAM reserve, overlay display target)
- [ ] 12. Models tab (list, VRAM labels vs active profile, pull/cancel, delete, recommended, test/compare)
- [ ] 13. CPU quality mode
- [ ] 14. Settings tab (incl. recording mode + overlap warning), startup checks, tray, fullscreen warning
- [ ] 15. Video device source (capture card): enumerate, resolution/fps, busy/HDCP errors, inspector region pick (done as 9b)
- [ ] 16. Vice theme, fonts, SVG icon/header, overlay styles
- [ ] 17. Web output + QR (+ Screen Wake Lock)

Phase 3 — release
- [ ] 18. PyInstaller onedir build script (+ earlier smoke build) — smoke build done 2026-10-21 (see log)
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
- 2026-10-21 (step 3): WGC via windows-capture **2.x** (pinned `>=2.0,<3`; 1.x API differs).
  Frames stored zero-copy (held buffers verified stable). Cursor never captured (it would pollute
  OCR), WGC border off. `monitor_index` = 1-based position in `win32.list_monitors()` order
  (verified). Two windows-capture crash triggers found and guarded (see "WGC pitfalls"); worth
  reporting upstream. mss timestamps use the grab start so its latency is not under-reported.
- 2026-10-21 (step 4): first samples (11 subtitles + 1 empty frame, user-provided with reference
  Turkish translations) are from a *video* with YouTube-style captions: white text on a translucent
  dark box. Region for all: [0.12, 0.86, 0.76, 0.135]. `color_tolerance` default 60 -> **75**: one
  sample's glyphs were dimmed to exactly 190 by a translucent band (fades do the same); 85 starts
  admitting scenery. Mask components ~= glyph count (ratio 1.0-1.13) on all samples, 0 on the empty
  frame (bright skin), so tests assert 0.9-1.25x. A few small junk marks remain near some captions
  (an edge line, a bracket next to "man."); left for OCR + text filters, revisit in step 5.
  **Still needed:** screenshots of real in-game subtitles (no box, outline only).
- 2026-10-21 (step 4, second batch): 6 more samples (same video source) with a *faint* caption box,
  white and **yellow** (#FFFF00) text, over bright road/grass/rooftops. Absolute-dark outline
  support dropped whole words there -> replaced by local contrast on color closeness (kernel 15,
  contrast 60; 40/50 let road texture in). Sample defaults now `text_colors` white + yellow.
  Known limits, documented in samples.json: 200236 keeps some road-texture junk next to
  "What should" (via the white color; ratio bound widened to 1.6), and in 200302 "You" on
  near-white asphalt is lost. Component filter moved before the upscale: same results, ~40 % faster.
- 2026-10-21 (step 5): settings models now `extra="forbid"` — the bench silently passed a removed
  field (`outline_support`) because unknown fields were ignored. Files still load: `read_model`
  drops unknown keys (also nested, also inside discriminated unions) and validates again.
- 2026-10-21: GitHub repo created **private** (user's choice): github.com/MertCodur-0/LiveSubs;
  make it public when it is presentable. CI on GitHub passes.
- 2026-10-21 (step 7): Ollama 0.40.5 on the dev machine; only `translategemma:latest` (4.3B) was
  installed — on the samples it softens swearing ("fucking" dropped) and misreads slang ("pick up"
  -> "geliyoruz"), median 302 ms. User chose to pull `qwen3:8b` and `gemma4:12b-it-qat` for the
  comparison. Ollama's GPU discovery took ~40 s at server start (requests 500 meanwhile): the
  startup check must wait/retry. `OLLAMA_MAX_LOADED_MODELS` is unset on this machine.
  Ruff `allowed-confusables` = ı ‘ ’ – (Turkish text and subtitle punctuation are normal content).
- 2026-10-21: **User: the whole machine is for translation** — only Windows runs besides LiveSubs
  (32 GB RAM, i7-14700HX 8P+12E, RTX 4070 8 GB). Plan: GPU lane `gemma4:12b-it-qat` (7.2 GB) fully
  on the GPU; CPU quality lane in RAM with a MoE model — candidates `gemma4:26b-a4b-it-qat`
  (16 GB) vs `qwen3:30b-a3b` (~18.5 GB) — likely **on by default** in this setup (step 13 decides);
  try `num_thread` = P-cores only. Needs `OLLAMA_MAX_LOADED_MODELS >= 2` (unset here). User said not
  to queue more downloads (aya-expanse:8b, gemma4:e4b-it-qat were suggested and declined for now).
  Open question to the user: does the game then run elsewhere (PS5 Remote Play / capture card)?
  qwen3:8b on the samples: fast (median 460 ms, fully on GPU) but weak Turkish ("dog" -> "köpek",
  "hugger" -> "öpücüklüyüm", swearing dropped); few-shot examples fix slang words but not the
  grammar. Prompt decision (few-shot, temperature) waits for gemma4:12b results.
- 2026-10-21: User: the game will not run on this PC — PS5 via capture card (+ splitter considered;
  most cards have passthrough, so a splitter may be unnecessary; PS5 HDCP must be off). Viewing
  setup undecided (see "User's primary scenario"). Video device source moved up to 9b, plus an
  own low-latency preview window. Default VRAM reserve for this profile kind is already 0.5 GB.
- 2026-10-21: gemma4:12b-it-qat measured: in memory **8.52 GB** (weights + vision and audio encoders
  + KV + graph; num_ctx 2048), so it does not fit 8 GB even on a dedicated machine: 39/49 layers on
  GPU with ~2 GB used by other apps -> median 1.05-1.2 s per line (target was 0.25-0.6 s).
  `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` only saved 0.2 GB and made it *slower*
  (1.6 s) -> keep Ollama defaults. Quality is clearly better than qwen3:8b: fluent, keeps swearing
  ("boktan", "lan"), "hugger" right; remaining misses: "pick up" (drugs), "the law chased in".
  Few-shot slang examples + temperature 0 are slightly better and deterministic (kept as the
  candidate prompt; not committed yet). The user decides on quality before picking a viewing setup.
- 2026-10-21 (steps 7-8 closed): default GPU model **`gemma4:12b-it-qat`** (clear quality winner on
  the samples; ~1.1 s/line because it overflows 8 GB; a MoE alternative, `gemma4:26b-a4b-it-qat`,
  is expected to be both better and faster by bandwidth math — to be measured later, user deferred
  it). Prompt: 5 fixed slang few-shot turns (`prompt.EXAMPLES`, never from tests/samples — a test
  enforces it) + temperature 0 (deterministic). User wants a runnable exe next: step 9 (minimal UI)
  + an early PyInstaller smoke build; viewing setup (3 options) chosen after judging quality live.
- 2026-10-21 (step 9 + smoke build): minimal UI — `ui/main_window.py` (source combo of windows and
  monitors, region selector, installed-model combo from `/api/tags`, Ollama status every 5 s on a
  worker thread + "start Ollama" (`ollama app.exe`), Start/Stop, live feed table, errors in the
  status bar via `i18n` kinds), `ui/overlay.py` (click-through, `WDA_EXCLUDEFROMCAPTURE`,
  QPainterPath outline + shadow + translucent box, word wrap, font shrinks to `max_lines`; placed
  right above the region, below it when there is no room), `ui/region_select.py` (dimmed window
  over the source area; selection painted with alpha 1, not 0 — fully transparent pixels of a
  layered window pass clicks through), `ui/screens.py` (physical <-> logical: Qt keeps each
  screen's top-left at its physical position and divides only the size; screens matched by that
  corner because `QScreen.name()` is "MSI G244F E2" for one monitor and "\.\DISPLAY2" for the
  other). One profile "varsayilan" (source, region, white+yellow) saved on Start/region change.
  Overlay: a new line hides the old translation until its own arrives; a repeat restores it; a
  clear waits `clear_delay_ms`. PyInstaller one-folder build (`scripts/livesubs.spec`,
  `scripts/build_exe.ps1`): 254 MB (OpenCV + Qt; trim later), exe starts UI + engine (winrt and
  windows-capture load frozen), closes cleanly. PS 5.1: native stderr + `$ErrorActionPreference
  = "Stop"` = false failures -> the build script uses "Continue" + `$LASTEXITCODE`.
  E2E on a sample slideshow with gemma4 (cold): warm-up 7.5 s, OCR 6-12 ms, translation 1.1-1.6 s,
  cache hits 0 ms across engine restarts.
- 2026-10-21 (first user test of the exe): **no translation appeared** because the source combo
  preselected the front-most window — the Explorer window the exe was started from — and the
  region selector only covered that window (the "small rectangle" the user complained about).
  Fix, as the user asked: "select region" dims **every monitor**; the user draws the subtitle
  wherever it is; `ui/pick.resolve` makes the **window under the selection** the source (front-
  most, not minimized, not "Program Manager"), else the monitor, and normalizes the region to it.
  The source combo never preselects anything any more. While running with no line read yet the
  status says "waiting for subtitles". Choosing a different window while running restarts the
  pipeline.
- 2026-10-21 (user feedback after a successful test: "translations sometimes vanish early"):
  the overlay now **keeps the previous translation until the new line's own arrives** (it used to
  blank out while the model worked, ~1-1.5 s), and a translation stays **3.5 s** after its
  subtitle disappears (`clear_delay_ms` default 800 -> 3500; the user's saved profile updated too);
  a new line within that time cancels the clear. Supersedes the step-9 overlay rule above.
