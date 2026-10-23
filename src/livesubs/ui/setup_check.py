"""Setup: what LiveSubs needs on this PC, with one-click fixes (first run, fresh Windows install).

Checks English OCR, Ollama (installed, running) and the translation model; installs Ollama with
winget, downloads the model with progress (then makes its text-only copy where the catalog names
one), creates shortcuts for the packaged exe, and exports or
imports the settings. Slow work runs on worker threads; results come back through signals.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QStandardPaths, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from livesubs import APP_NAME
from livesubs.core.backup import BackupError, export_settings, import_settings
from livesubs.core.config import AppConfig
from livesubs.i18n import t
from livesubs.ollama import catalog, install
from livesubs.ollama.client import (
    OllamaError,
    PullCancelledError,
    PullError,
    PullProgress,
    create_text_only,
    is_installed,
    list_models,
    models_dir,
    pull_model,
    start_ollama,
    wait_until_running,
)
from livesubs.platform import shortcuts, win32
from livesubs.ui import theme

log = logging.getLogger(__name__)

GB = 1_000_000_000  # as Ollama and ollama.com count model sizes
OCR_POLL_MS = 5000
OCR_POLL_LIMIT = 120  # 10 minutes: Windows downloads the language pack meanwhile
CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SetupStatus:
    ocr_language: str | None
    ollama_installed: bool
    models: tuple[str, ...] | None  # None: Ollama does not answer

    @property
    def ollama_running(self) -> bool:
        return self.models is not None

    def has_model(self, name: str) -> bool:
        return self.models is not None and any(catalog.same_model(name, m) for m in self.models)

    def complete(self, model: str) -> bool:
        return self.ocr_language is not None and self.has_model(model)


def check_status(url: str) -> SetupStatus:
    from livesubs.engine.ocr.language import english_ocr_language

    try:
        language = english_ocr_language()
    except OSError:  # WinRT unavailable
        language = None
    models = list_models(url)
    return SetupStatus(
        ocr_language=language,
        ollama_installed=models is not None or is_installed(),
        models=None if models is None else tuple(m.name for m in models),
    )


def free_bytes(folder: Path) -> int:
    """Free space on the drive of ``folder`` (or of its nearest existing parent)."""
    existing = next(p for p in (folder, *folder.parents) if p.exists())
    return shutil.disk_usage(existing).free


def desktop_shortcut() -> Path:
    desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    return Path(desktop) / f"{APP_NAME}.lnk"


class _Bridge(QObject):
    """Carries results from worker threads to the UI thread."""

    status = Signal(object)  # SetupStatus
    progress = Signal(object)  # PullProgress
    pull_finished = Signal(str, str)  # model, error ("" = done, CANCELLED)
    ollama_finished = Signal(str)  # error, "" = running
    prepared = Signal(str, str)  # model to use, error making the text-only copy ("" = none)


class SetupDialog(QDialog):
    model_selected = Signal(str)  # installed and chosen: the main window makes it the GPU model
    settings_imported = Signal()

    def __init__(
        self,
        config: AppConfig,
        parent: QWidget | None = None,
        *,
        checker: Callable[[str], SetupStatus] = check_status,
        ram_gb: float | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("setup.title"))
        self.setMinimumWidth(720)
        self._config = config
        self._checker = checker
        self._status: SetupStatus | None = None
        self._cancel: threading.Event | None = None
        self._preparing = False
        self._ocr_polls = 0
        self._bridge = _Bridge()
        self._bridge.status.connect(self._on_status)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.pull_finished.connect(self._on_pull_finished)
        self._bridge.ollama_finished.connect(self._on_ollama_finished)
        self._bridge.prepared.connect(self._on_prepared)
        self._ocr_timer = QTimer(self, interval=OCR_POLL_MS, timeout=self._poll_ocr)
        ram = ram_gb if ram_gb is not None else win32.total_ram_bytes() / GB
        self._recommended = catalog.recommend(ram)
        self._build()

    # --- layout ----------------------------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(t("setup.intro"), wordWrap=True)
        layout.addWidget(intro)
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        row = 0

        def add_row(title: str, status: QLabel, *actions: QWidget) -> None:
            nonlocal row
            grid.addWidget(QLabel(f"<b>{title}</b>"), row, 0)
            grid.addWidget(status, row, 1)
            buttons = QHBoxLayout()
            for action in actions:
                buttons.addWidget(action)
            grid.addLayout(buttons, row, 2)
            row += 1

        self.ocr_status = QLabel(t("ollama.checking"), wordWrap=True)
        self.ocr_button = QPushButton(t("setup.ocr.install"), clicked=self.install_ocr)
        add_row(t("setup.ocr"), self.ocr_status, self.ocr_button)

        self.ollama_status = QLabel(t("ollama.checking"), wordWrap=True)
        self.ollama_button = QPushButton(t("setup.ollama.install"), clicked=self.fix_ollama)
        add_row(t("setup.ollama"), self.ollama_status, self.ollama_button)

        self.model_status = QLabel(t("ollama.checking"), wordWrap=True)
        add_row(t("setup.model"), self.model_status)
        self.model_combo = QComboBox()
        for model in catalog.RECOMMENDED:
            label = model.name + "  —  " + t(model.description_key, size=model.download_bytes / GB)
            if model == self._recommended:
                label += t("setup.model.recommended")
            self.model_combo.addItem(label, model.name)
        self.model_combo.currentIndexChanged.connect(self._update_model_button)
        self.model_button = QPushButton(t("setup.model.download"), clicked=self.get_model)
        self.cancel_button = QPushButton(t("setup.model.cancel"), clicked=self.cancel_download)
        self.cancel_button.hide()
        grid.addWidget(self.model_combo, row, 1)
        model_buttons = QHBoxLayout()
        model_buttons.addWidget(self.model_button)
        model_buttons.addWidget(self.cancel_button)
        grid.addLayout(model_buttons, row, 2)
        row += 1
        self.progress = QProgressBar(maximum=1000, textVisible=False)
        self.progress.hide()
        self.progress_label = QLabel(wordWrap=True)
        grid.addWidget(self.progress, row, 1)
        row += 1
        grid.addWidget(self.progress_label, row, 1, 1, 2)
        row += 1

        self._exe = shortcuts.packaged_exe()
        self.shortcut_status = QLabel()
        self.shortcut_button = QPushButton(
            t("setup.shortcut.create"), clicked=self.create_shortcuts
        )
        if self._exe is not None:  # the development venv has its own script for this
            add_row(t("setup.shortcut"), self.shortcut_status, self.shortcut_button)
            self._update_shortcut()

        self.backup_status = QLabel(t("setup.backup.hint"), wordWrap=True)
        export_button = QPushButton(t("setup.export"), clicked=self.export_settings)
        import_button = QPushButton(t("setup.import"), clicked=self.import_settings)
        add_row(t("setup.backup"), self.backup_status, export_button, import_button)
        layout.addLayout(grid)

        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(QPushButton(t("setup.close"), clicked=self.close))
        layout.addLayout(footer)

        self.model_combo.setCurrentIndex(catalog.RECOMMENDED.index(self._recommended))

    # --- status ----------------------------------------------------------------------------------

    def refresh(self) -> None:
        url = self._config.ollama.url
        threading.Thread(
            target=lambda: self._bridge.status.emit(self._checker(url)), daemon=True
        ).start()

    def check_on_startup(self) -> None:
        """Check in the background and open only when something is missing."""

        def on_first(status: SetupStatus) -> None:
            self._bridge.status.disconnect(on_first)
            if not status.complete(self._config.ollama.gpu_model):
                self.show()

        self._bridge.status.connect(on_first)
        self.refresh()

    def _on_status(self, status: SetupStatus) -> None:
        first, self._status = self._status is None, status
        if status.ocr_language is not None:
            self._ocr_timer.stop()
            self.ocr_status.setText(t("setup.ocr.ok", language=status.ocr_language))
            self.ocr_button.hide()
        elif not self._ocr_timer.isActive():
            self.ocr_status.setText(t("setup.ocr.missing"))
            self.ocr_button.show()

        busy = self._ollama_busy
        if status.ollama_running:
            self.ollama_status.setText(t("setup.ollama.ok"))
            self.ollama_button.hide()
        elif not busy:
            installed = status.ollama_installed
            self.ollama_status.setText(
                t("setup.ollama.stopped" if installed else "setup.ollama.missing")
            )
            self.ollama_button.setText(
                t("setup.ollama.start" if installed else "setup.ollama.install")
            )
            self.ollama_button.show()

        model = self._config.ollama.gpu_model
        entry = catalog.entry_for(model)
        if first and status.has_model(model) and entry is not None:
            # Later checks keep the user's choice.
            self.model_combo.setCurrentIndex(self.model_combo.findData(entry.name))
        if not status.ollama_running:
            self.model_status.setText(t("setup.model.needs_ollama"))
        elif status.has_model(model):
            self.model_status.setText(t("setup.model.ok", model=model))
        else:
            self.model_status.setText(t("setup.model.missing", model=model))
        self._update_model_button()

    @property
    def _ollama_busy(self) -> bool:
        return not self.ollama_button.isEnabled()

    @property
    def downloading(self) -> bool:
        return self._cancel is not None

    @property
    def busy(self) -> bool:
        return self.downloading or self._preparing

    def _entry(self) -> catalog.CatalogModel:
        name = self.model_combo.currentData()
        return next(m for m in catalog.RECOMMENDED if m.name == name)

    def _update_model_button(self) -> None:
        status, entry = self._status, self._entry()
        installed = status is not None and status.has_model(entry.name)
        in_use = (
            installed
            and status.has_model(entry.use_name)
            and catalog.same_model(entry.use_name, self._config.ollama.gpu_model)
        )
        if in_use:
            self.model_button.setText(t("setup.model.in_use"))
        else:
            self.model_button.setText(t("setup.model.use" if installed else "setup.model.download"))
        ready = status is not None and status.ollama_running
        self.model_button.setEnabled(ready and not in_use and not self.busy)
        self.model_combo.setEnabled(not self.busy)

    # --- English OCR -----------------------------------------------------------------------------

    def install_ocr(self) -> None:
        from livesubs.engine.ocr.language import request_english_install

        if not request_english_install():
            self.ocr_status.setText(t("setup.ocr.declined"))
            return
        self.ocr_status.setText(t("setup.ocr.installing"))
        self.ocr_button.hide()
        self._ocr_polls = 0
        self._ocr_timer.start()

    def _poll_ocr(self) -> None:
        self._ocr_polls += 1
        if self._ocr_polls > OCR_POLL_LIMIT:
            self._ocr_timer.stop()
            self.ocr_status.setText(t("setup.ocr.restart"))
            self.ocr_button.show()
            return
        self.refresh()

    # --- Ollama ----------------------------------------------------------------------------------

    def fix_ollama(self) -> None:
        installed = self._status is not None and self._status.ollama_installed
        if not installed and install.winget_path() is None:
            QDesktopServices.openUrl(QUrl(install.DOWNLOAD_PAGE))
            self.ollama_status.setText(t("setup.ollama.no_winget"))
            return
        self.ollama_button.setEnabled(False)
        self.ollama_status.setText(
            t("setup.ollama.starting" if installed else "setup.ollama.installing")
        )
        url = self._config.ollama.url

        def work() -> None:
            error = ""
            if not installed:
                ok, detail = install.install_ollama()
                if not ok:
                    log.warning("Ollama installation failed: %s", detail)
                    error = detail
            if not error and list_models(url) is None:
                start_ollama()
                if not wait_until_running(url):
                    error = t("ollama.offline")
            self._bridge.ollama_finished.emit(error)

        threading.Thread(target=work, daemon=True).start()

    def _on_ollama_finished(self, error: str) -> None:
        self.ollama_button.setEnabled(True)
        if error:
            self.ollama_status.setText(t("setup.ollama.failed", detail=error))
            QDesktopServices.openUrl(QUrl(install.DOWNLOAD_PAGE))
        self.refresh()

    # --- model -----------------------------------------------------------------------------------

    def get_model(self) -> None:
        entry = self._entry()
        if self._status is not None and self._status.has_model(entry.name):
            self.use_model(entry)
            return
        folder = models_dir()
        free = free_bytes(folder)
        if free < entry.download_bytes * 1.05:
            self.progress_label.setText(
                t(
                    "setup.model.no_space",
                    need=entry.download_bytes / GB,
                    free=free / GB,
                    folder=folder,
                )
            )
            return
        self.start_download(entry.name)

    def start_download(self, name: str) -> None:
        cancel = threading.Event()
        self._cancel = cancel
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.show()
        self.progress_label.setText(t("setup.model.progress_unknown", status=name))
        self._update_model_button()
        url = self._config.ollama.url

        def work() -> None:
            try:
                pull_model(name, self._bridge.progress.emit, cancel, url)
            except PullCancelledError:
                self._bridge.pull_finished.emit(name, CANCELLED)
            except PullError as exc:
                self._bridge.pull_finished.emit(name, str(exc))
            else:
                self._bridge.pull_finished.emit(name, "")

        threading.Thread(target=work, daemon=True).start()

    def cancel_download(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _on_progress(self, progress: PullProgress) -> None:
        if progress.total:
            self.progress.setValue(round(1000 * progress.completed / progress.total))
            self.progress_label.setText(
                t(
                    "setup.model.progress",
                    status=progress.status,
                    done=progress.completed / GB,
                    total=progress.total / GB,
                )
            )
        else:
            self.progress_label.setText(t("setup.model.progress_unknown", status=progress.status))

    def _on_pull_finished(self, name: str, error: str) -> None:
        self._cancel = None
        self.progress.hide()
        self.cancel_button.hide()
        if error == CANCELLED:
            self.progress_label.setText(t("setup.model.cancelled"))
        elif error:
            self.progress_label.setText(t("setup.model.failed", detail=error))
        else:
            self.progress_label.clear()
            if (entry := catalog.entry_for(name)) is not None:
                self.use_model(entry)
                return
            self.model_selected.emit(name)
        self._update_model_button()
        self.refresh()

    def use_model(self, entry: catalog.CatalogModel) -> None:
        """Select an installed catalog model; first make its text-only copy if it has one."""
        if not entry.text_only or (
            self._status is not None and self._status.has_model(entry.text_only)
        ):
            self.model_selected.emit(entry.use_name)
            self._update_model_button()
            self.refresh()
            return
        self._preparing = True
        self.progress_label.setText(t("setup.model.preparing"))
        self._update_model_button()
        url = self._config.ollama.url

        def work() -> None:
            try:
                create_text_only(entry.name, entry.text_only, url)
            except OllamaError as exc:
                log.warning("Text-only copy of %s failed: %s", entry.name, exc)
                self._bridge.prepared.emit(entry.name, str(exc))
            else:
                self._bridge.prepared.emit(entry.text_only, "")

        threading.Thread(target=work, daemon=True).start()

    def _on_prepared(self, model: str, error: str) -> None:
        self._preparing = False
        if error:
            self.progress_label.setText(t("setup.model.text_only_failed", detail=error))
        else:
            self.progress_label.clear()
        self.model_selected.emit(model)
        self._update_model_button()
        self.refresh()

    # --- shortcuts and settings backup -----------------------------------------------------------

    def _update_shortcut(self) -> None:
        exists = desktop_shortcut().exists()
        self.shortcut_status.setText(t("setup.shortcut.ok" if exists else "setup.shortcut.missing"))
        # Recreating is allowed: an old shortcut may point to a moved or older copy.
        self.shortcut_button.setText(
            t("setup.shortcut.recreate" if exists else "setup.shortcut.create")
        )

    def create_shortcuts(self) -> None:
        if self._exe is None or not shortcuts.create_shortcuts(self._exe, theme.asset("icon.ico")):
            self.shortcut_status.setText(t("setup.shortcut.failed"))
            return
        self._update_shortcut()

    def export_settings(self) -> None:
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            t("setup.export.title"),
            str(Path(documents) / t("setup.backup.default_name")),
            t("setup.backup.filter"),
        )
        if not path:
            return
        try:
            count = export_settings(Path(path))
        except (BackupError, OSError) as exc:
            self.backup_status.setText(t("setup.backup.failed", detail=exc))
            return
        self.backup_status.setText(t("setup.export.done", count=count, path=path))

    def import_settings(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("setup.import.title"), "", t("setup.backup.filter")
        )
        if not path:
            return
        try:
            count = import_settings(Path(path))
        except BackupError as exc:
            self.backup_status.setText(t("setup.backup.failed", detail=exc))
            return
        self.backup_status.setText(t("setup.import.done", count=count))
        self.settings_imported.emit()

    def shutdown(self) -> None:
        self.cancel_download()
        self._ocr_timer.stop()
