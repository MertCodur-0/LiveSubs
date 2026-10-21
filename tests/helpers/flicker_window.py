"""A window in its own process whose color changes continuously.

Capture tests need a target that keeps producing new frames and is owned by *another* process:
WGC must never capture a window of the capturing process (see CLAUDE.md, decision log).

Usage: python flicker_window.py TITLE X Y WIDTH HEIGHT
Prints "ready" once the window is shown; exits when stdin closes.
"""

import sys
import threading
import tkinter as tk

COLORS = ("#FF2D95", "#00E5FF", "#FF8C00", "#7B2FF7")


def main() -> None:
    title, x, y, width, height = sys.argv[1], *map(int, sys.argv[2:6])
    root = tk.Tk()
    root.title(title)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.attributes("-topmost", True)
    state = {"i": 0}

    def flip() -> None:
        state["i"] += 1
        root.configure(background=COLORS[state["i"] % len(COLORS)])
        root.after(30, flip)

    def announce() -> None:
        print("ready", flush=True)

    def wait_for_parent() -> None:
        sys.stdin.read()
        root.after(0, root.destroy)

    threading.Thread(target=wait_for_parent, daemon=True).start()
    root.after(30, flip)
    root.after(300, announce)
    root.mainloop()


if __name__ == "__main__":
    main()
