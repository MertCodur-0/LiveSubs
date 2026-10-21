# LiveSubs

> Real-time, fully local English → Turkish subtitle translation for any game on Windows.

[Türkçe](README.tr.md)

LiveSubs reads the subtitle area of a game window, monitor or capture card with Windows' built-in
OCR, translates each line with a local LLM through [Ollama](https://ollama.com), and draws the
translation in a click-through overlay on top of the game. No cloud APIs — everything runs on your
own machine.

**Status:** early development. See [CLAUDE.md](CLAUDE.md) for the architecture and roadmap.

## Planned features

- Window, monitor or capture-card sources; per-game profiles
- Subtitle-color masking that ignores busy, bright game backgrounds
- GPU translation with an optional higher-quality CPU pass
- In-app model management for Ollama (download, VRAM fit check, side-by-side test)
- Overlay hidden from screen capture, so OCR never reads its own output
- Optional phone view over local Wi-Fi with a QR code

## Development

Requires Windows 10 2004+ / Windows 11 and **Python 3.13**.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m livesubs
.\.venv\Scripts\python.exe -m pytest
```

## License

[MIT](LICENSE)
