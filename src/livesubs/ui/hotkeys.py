"""Connects registered global hotkeys to Qt: WM_HOTKEY arrives through a native event filter."""

from __future__ import annotations

import logging

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Signal

from livesubs.platform import hotkeys

log = logging.getLogger(__name__)


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, manager: HotkeyManager) -> None:
        super().__init__()
        self._manager = manager

    def nativeEventFilter(self, event_type: bytes, message: int) -> tuple[bool, int]:  # noqa: N802
        if bytes(event_type) == b"windows_generic_MSG":
            hotkey_id = hotkeys.hotkey_id_of(int(message))
            if hotkey_id is not None and self._manager.dispatch(hotkey_id):
                return True, 0
        return False, 0


class HotkeyManager(QObject):
    triggered = Signal(str)  # action name, e.g. "calibration"

    def __init__(self, hwnd: int) -> None:
        super().__init__()
        self._hwnd = hwnd
        self._actions: dict[int, str] = {}
        self._filter = _Filter(self)
        QCoreApplication.instance().installNativeEventFilter(self._filter)

    def register_all(self, keys: dict[str, str]) -> list[str]:
        """Register ``{action: "F8", ...}``; returns the key texts that could not be registered."""
        self.unregister_all()
        failed = []
        for hotkey_id, (action, text) in enumerate(keys.items(), start=1):
            try:
                hotkey = hotkeys.parse(text)
            except hotkeys.HotkeyError:
                log.warning("Invalid hotkey %r for %s", text, action)
                failed.append(text)
                continue
            if hotkeys.register(self._hwnd, hotkey_id, hotkey):
                self._actions[hotkey_id] = action
            else:
                failed.append(text)
        return failed

    def unregister_all(self) -> None:
        for hotkey_id in self._actions:
            hotkeys.unregister(self._hwnd, hotkey_id)
        self._actions.clear()

    def dispatch(self, hotkey_id: int) -> bool:
        action = self._actions.get(hotkey_id)
        if action is None:
            return False
        self.triggered.emit(action)
        return True

    def close(self) -> None:
        self.unregister_all()
        QCoreApplication.instance().removeNativeEventFilter(self._filter)
