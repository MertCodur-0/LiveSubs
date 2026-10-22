"""Launcher of the packaged app (copied next to LiveSubs.exe by scripts/build_exe.ps1).

LiveSubs.exe is the official, Python-Software-Foundation-signed ``pythonw.exe`` of the embeddable
Python distribution, renamed. Windows Smart App Control trusts it, unlike an unsigned PyInstaller
bootloader that gets a new hash on every build. Python imports this module at startup; when the
exe was started without arguments (a double-click), it runs the app and exits. With arguments —
the engine's child process (``--multiprocessing-fork``), or ``LiveSubs.exe -m ...`` for
diagnostics — it does nothing and Python carries on as usual.
"""

import sys

if len(sys.orig_argv) == 1:
    import logging
    import os

    from livesubs.app import main

    try:
        code = main()
    finally:
        logging.shutdown()
    os._exit(code)  # skip the interactive prompt that would follow site initialization
