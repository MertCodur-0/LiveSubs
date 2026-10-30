"""Models tab: installed models with VRAM fit labels, download, delete, and a side-by-side test.

Fit labels are judged against the active profile's budget (card memory minus the desktop's share
and the game's reserve, see ``ollama.vram``). Ollama calls run on worker threads; results come
back through signals. The main window owns the config: choosing a model is announced through
``model_selected``.
"""

from __future__ import annotations

import statistics
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from livesubs.core.config import AppConfig
from livesubs.core.profiles import Profile
from livesubs.engine.translate.base import CancelToken
from livesubs.i18n import t
from livesubs.ollama import catalog, vram
from livesubs.ollama.client import (
    InstalledModel,
    LoadedModel,
    OllamaError,
    PullCancelledError,
    PullError,
    PullProgress,
    delete_model,
    is_installed,
    list_models,
    loaded_models,
    model_info,
    models_dir,
    pull_model,
)
from livesubs.ollama.compare import TEST_LINES, LineResult, ModelRun, run_comparison
from livesubs.platform import gpu
from livesubs.ui.rows import info
from livesubs.ui.setup_check import free_bytes

GB = vram.GB
REFRESH_MS = 5000
CANCELLED = "cancelled"
COLUMNS = ("name", "size", "parameters", "memory", "fit", "state")
FULLY_ON_GPU = 0.99


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What Ollama reported in one refresh; ``models`` is None when it does not answer."""

    models: list[InstalledModel] | None
    loaded: list[LoadedModel] = field(default_factory=list)
    archs: dict[str, vram.ModelArch | None] = field(default_factory=dict)  # by digest


def fetch_snapshot(url: str, known: set[str]) -> Snapshot:
    """Ollama's view of the models; ``/api/show`` only for digests not in ``known``."""
    models = list_models(url)
    if models is None:
        return Snapshot(None)
    archs: dict[str, vram.ModelArch | None] = {}
    for model in models:
        if model.digest in known:
            continue
        try:
            archs[model.digest] = vram.parse_arch(model_info(model.name, url))
        except OllamaError:
            archs[model.digest] = None
    return Snapshot(models, loaded_models(url) or [], archs)


def cpu_share(url: str, model: str) -> float | None:
    """Part of the loaded ``model`` that runs on the CPU when that slows it down, else None.

    A MoE model always keeps its experts in RAM; that is expected and stays fast.
    """
    loaded = loaded_models(url) or []
    entry = next((m for m in loaded if catalog.same_model(m.name, model)), None)
    if entry is None or vram.gpu_share(entry.size_bytes, entry.vram_bytes) >= FULLY_ON_GPU:
        return None
    try:
        arch = vram.parse_arch(model_info(model, url))
    except OllamaError:
        return None
    if arch is not None and arch.is_moe:
        return None
    return 1.0 - vram.gpu_share(entry.size_bytes, entry.vram_bytes)


def _hint(text: str = "") -> QLabel:
    label = QLabel(text, wordWrap=True)
    label.setProperty("hint", True)  # muted, styled by the theme
    return label


class _Bridge(QObject):
    """Carries results from worker threads to the UI thread."""

    snapshot = Signal(object)  # Snapshot
    progress = Signal(object)  # PullProgress
    pull_finished = Signal(str, str)  # model, error ("" = done, CANCELLED)
    deleted = Signal(str, str)  # model, error
    model_run = Signal(object)  # ModelRun
    line_result = Signal(object)  # LineResult
    compare_finished = Signal()


class ModelsTab(QWidget):
    model_selected = Signal(str)  # make it the translation model
    models_changed = Signal()  # downloaded or deleted: other model lists refresh
    start_ollama_requested = Signal()
    setup_requested = Signal()

    def __init__(
        self,
        config: AppConfig,
        profile: Profile,
        *,
        fetch: Callable[[str, set[str]], Snapshot] = fetch_snapshot,
        adapter: Callable[[], gpu.GpuAdapter | None] = gpu.main_adapter,
        comparison: Callable[..., None] = run_comparison,
    ) -> None:
        super().__init__()
        self.config = config
        self.profile = profile
        self._fetch = fetch
        self._comparison = comparison
        self._adapter = adapter()
        self._snapshot: Snapshot | None = None
        self._archs: dict[str, vram.ModelArch | None] = {}
        self._busy = False  # live translation runs: it needs the GPU
        self._pull_cancel: threading.Event | None = None
        self._compare_cancel: CancelToken | None = None
        self._compare_models: list[str] = []
        self._compare_times: dict[str, list[float]] = {}
        self._compare_runs: dict[str, ModelRun] = {}
        self._bridge = _Bridge()
        self._bridge.snapshot.connect(self._on_snapshot)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.pull_finished.connect(self._on_pull_finished)
        self._bridge.deleted.connect(self._on_deleted)
        self._bridge.model_run.connect(self._on_model_run)
        self._bridge.line_result.connect(self._on_line_result)
        self._bridge.compare_finished.connect(self._on_compare_finished)
        self._timer = QTimer(self, interval=REFRESH_MS, timeout=self.refresh)

        content = QWidget(objectName="page")  # transparent in the scroll area (theme)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)
        layout.addWidget(self._status_box())
        layout.addWidget(self._installed_box())
        layout.addWidget(self._download_box())
        layout.addWidget(self._compare_box())
        layout.addStretch(1)
        scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        self._update_budget()
        self._update_buttons()

    # --- groups ----------------------------------------------------------------------------------

    def _status_box(self) -> QGroupBox:
        box = QGroupBox(t("models.status"))
        layout = QVBoxLayout(box)
        row = QHBoxLayout()
        self.ollama_status = QLabel(t("ollama.checking"), wordWrap=True)
        # Signal to signal through connect(): `clicked=signal` in the constructor raises on click.
        self.start_button = QPushButton(t("ollama.start"))
        self.start_button.clicked.connect(self.start_ollama_requested)
        self.setup_button = QPushButton(t("setup.button"))
        self.setup_button.clicked.connect(self.setup_requested)
        self.start_button.hide()
        self.setup_button.hide()
        row.addWidget(self.ollama_status, 1)
        row.addWidget(self.start_button)
        row.addWidget(self.setup_button)
        layout.addLayout(row)
        self.budget_label = QLabel(wordWrap=True)
        layout.addWidget(self.budget_label)
        return box

    def _installed_box(self) -> QGroupBox:
        box = QGroupBox(t("models.installed"))
        layout = QVBoxLayout(box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([t(f"models.column.{c}") for c in COLUMNS])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.setMinimumHeight(150)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.itemDoubleClicked.connect(lambda _item: self.use_selected())
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.use_button = QPushButton(t("models.use"), clicked=self.use_selected)
        self.delete_button = QPushButton(t("models.delete"), clicked=self.delete_selected)
        self.installed_status = _hint(t("models.fit_hint"))
        buttons.addWidget(self.installed_status, 1)
        buttons.addWidget(self.use_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(QPushButton(t("source.refresh"), clicked=self.refresh))
        layout.addLayout(buttons)
        return box

    def _download_box(self) -> QGroupBox:
        box = QGroupBox(t("models.download"))
        grid = QGridLayout(box)
        grid.setColumnStretch(0, 1)
        self.pull_combo = QComboBox(editable=True)
        self.pull_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for model in catalog.RECOMMENDED:
            self.pull_combo.addItem(model.name)
        self.pull_combo.lineEdit().setPlaceholderText(t("models.pull_placeholder"))
        self.pull_combo.currentTextChanged.connect(self._update_pull_hint)
        self.pull_button = QPushButton(t("setup.model.download"), clicked=self.start_pull)
        self.pull_cancel_button = QPushButton(t("setup.model.cancel"), clicked=self.cancel_pull)
        self.pull_cancel_button.hide()
        grid.addWidget(self.pull_combo, 0, 0)
        grid.addWidget(self.pull_button, 0, 1)
        grid.addWidget(self.pull_cancel_button, 0, 2)
        self.pull_hint = _hint()
        grid.addWidget(self.pull_hint, 1, 0, 1, 3)
        self.pull_progress = QProgressBar(maximum=1000, textVisible=False)
        self.pull_progress.hide()
        grid.addWidget(self.pull_progress, 2, 0, 1, 3)
        self.pull_status = QLabel(wordWrap=True)
        self.pull_status.hide()  # shown with a message: no empty row in the card
        grid.addWidget(self.pull_status, 3, 0, 1, 3)
        self._update_pull_hint(self.pull_combo.currentText())
        return box

    def _compare_box(self) -> QGroupBox:
        box = QGroupBox(t("models.compare"))
        layout = QVBoxLayout(box)
        layout.setSpacing(8)
        pick = QHBoxLayout()
        pick.addWidget(info(t("models.compare_hint")))
        self.compare_a = QComboBox()
        self.compare_b = QComboBox()
        self.compare_button = QPushButton(t("models.compare_start"), clicked=self.toggle_compare)
        pick.addWidget(QLabel(t("models.compare_a")))
        pick.addWidget(self.compare_a, 1)
        pick.addWidget(QLabel(t("models.compare_b")))
        pick.addWidget(self.compare_b, 1)
        pick.addWidget(self.compare_button)
        layout.addLayout(pick)
        self.compare_lines = QPlainTextEdit("\n".join(TEST_LINES))
        self.compare_lines.setFixedHeight(110)
        layout.addWidget(self.compare_lines)
        self.compare_status = QLabel(wordWrap=True)
        layout.addWidget(self.compare_status)
        self.compare_table = QTableWidget(0, 3)
        self.compare_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.compare_table.setWordWrap(True)
        self.compare_table.verticalHeader().hide()
        header = self.compare_table.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.compare_table.setMinimumHeight(220)
        self.compare_table.hide()
        layout.addWidget(self.compare_table)
        return box

    # --- refresh ---------------------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh()
        self._timer.start()  # loaded models change while translation runs

    def hideEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)  # type: ignore[arg-type]
        self._timer.stop()

    def refresh(self) -> None:
        url, known = self.config.ollama.url, set(self._archs)
        threading.Thread(
            target=lambda: self._bridge.snapshot.emit(self._fetch(url, known)), daemon=True
        ).start()

    def _on_snapshot(self, snapshot: Snapshot) -> None:
        self._archs.update(snapshot.archs)
        self._snapshot = snapshot
        if snapshot.models is None:
            installed = is_installed()
            self.ollama_status.setText(
                t("models.ollama_offline" if installed else "models.ollama_missing")
            )
            self.start_button.setVisible(installed)
            self.setup_button.show()
        else:
            self.ollama_status.setText(t("models.ollama_ok", count=len(snapshot.models)))
            self.start_button.hide()
            self.setup_button.hide()
        self._fill_table()
        self._fill_compare_combos()
        self._update_buttons()

    def set_profile(self, profile: Profile) -> None:
        """Another profile became active, or its VRAM reserve changed: re-judge every model."""
        self.profile = profile
        self._update_budget()
        self._fill_table()

    def set_busy(self, busy: bool) -> None:
        """Live translation is running (or not): it needs the GPU the test would use."""
        self._busy = busy
        self._update_buttons()

    def mark_in_use(self) -> None:
        """The translation model changed elsewhere (Live tab, setup dialog)."""
        self._fill_table()
        self._update_buttons()

    # --- VRAM budget and the table ---------------------------------------------------------------

    def total_vram_bytes(self) -> int:
        if self.config.total_vram_gb is not None:
            return round(self.config.total_vram_gb * GB)
        return self._adapter.dedicated_bytes if self._adapter is not None else 0

    def budget_bytes(self) -> int:
        return vram.budget_bytes(self.total_vram_bytes(), self.profile.game_vram_reserve_gb())

    def _update_budget(self) -> None:
        total = self.total_vram_bytes()
        if total == 0:
            self.budget_label.setText(t("models.no_gpu"))
            return
        name = self._adapter.name if self._adapter is not None else t("models.gpu_configured")
        self.budget_label.setText(
            t(
                "models.budget",
                gpu=name,
                total=total / GB,
                desktop=vram.DESKTOP_VRAM_BYTES / GB,
                reserve=self.profile.game_vram_reserve_gb(),
                profile=self.profile.name,
                budget=self.budget_bytes() / GB,
            )
        )

    def fit_of(self, model: InstalledModel) -> tuple[vram.Fit, int]:
        arch = self._archs.get(model.digest)
        estimate = vram.estimate_bytes(model.size_bytes, arch, self.config.ollama.num_ctx)
        if arch is None:
            return vram.Fit.UNKNOWN, estimate
        return vram.judge(estimate, self.budget_bytes(), moe=arch.is_moe), estimate

    def _state_text(self, model: InstalledModel) -> str:
        parts = []
        if catalog.same_model(model.name, self.config.ollama.gpu_model):
            parts.append(t("models.in_use"))
        loaded = self._snapshot.loaded if self._snapshot is not None else []
        entry = next((m for m in loaded if catalog.same_model(m.name, model.name)), None)
        if entry is not None:
            share = vram.gpu_share(entry.size_bytes, entry.vram_bytes)
            parts.append(t("models.loaded", percent=round(share * 100)))
        return ", ".join(parts)

    def _fill_table(self) -> None:
        models = self.installed()
        selected = self.selected_model()
        self.table.setRowCount(len(models))
        for row, model in enumerate(models):
            fit, estimate = self.fit_of(model)
            values = (
                model.name,
                f"{model.size_bytes / GB:.1f} GB",
                " ".join(filter(None, (model.parameter_size, model.quantization))),
                f"~{estimate / GB:.1f} GB",
                t(f"models.fit.{fit}"),
                self._state_text(model),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == COLUMNS.index("fit"):
                    item.setToolTip(t(f"models.fit_tip.{fit}"))
                self.table.setItem(row, column, item)
            if model.name == selected:
                self.table.selectRow(row)

    def installed(self) -> list[InstalledModel]:
        if self._snapshot is None or self._snapshot.models is None:
            return []
        return self._snapshot.models

    def selected_model(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        return item.text() if item is not None else None

    def _update_buttons(self) -> None:
        name = self.selected_model()
        in_use = name is not None and catalog.same_model(name, self.config.ollama.gpu_model)
        self.use_button.setEnabled(name is not None and not in_use)
        self.delete_button.setEnabled(name is not None and not (in_use and self._busy))
        online = self._snapshot is not None and self._snapshot.models is not None
        self.pull_button.setEnabled(online and self._pull_cancel is None)
        self.pull_combo.setEnabled(self._pull_cancel is None)
        comparing = self._compare_cancel is not None
        self.compare_button.setText(
            t("models.compare_stop" if comparing else "models.compare_start")
        )
        self.compare_button.setEnabled(comparing or (online and not self._busy))
        if self._busy and not comparing:
            self.compare_status.setText(t("models.compare_busy"))
        elif self.compare_status.text() == t("models.compare_busy"):
            self.compare_status.clear()

    # --- use and delete --------------------------------------------------------------------------

    def use_selected(self) -> None:
        name = self.selected_model()
        if name is None or catalog.same_model(name, self.config.ollama.gpu_model):
            return
        self.model_selected.emit(name)
        self.installed_status.setText(t("models.now_in_use", model=name))
        self.mark_in_use()

    def delete_selected(self) -> None:
        name = self.selected_model()
        if name is None:
            return
        in_use = catalog.same_model(name, self.config.ollama.gpu_model)
        if in_use and self._busy:
            self.installed_status.setText(t("models.delete_busy"))
            return
        question = t("models.delete_confirm", model=name)
        if in_use:
            question += "\n\n" + t("models.delete_in_use")
        if QMessageBox.question(self, t("models.delete"), question) != (
            QMessageBox.StandardButton.Yes
        ):
            return
        url = self.config.ollama.url

        def work() -> None:
            try:
                delete_model(name, url)
            except OllamaError as exc:
                self._bridge.deleted.emit(name, str(exc))
            else:
                self._bridge.deleted.emit(name, "")

        self.delete_button.setEnabled(False)
        threading.Thread(target=work, daemon=True).start()

    def _on_deleted(self, name: str, error: str) -> None:
        if error:
            self.installed_status.setText(t("models.delete_failed", detail=error))
        else:
            self.installed_status.setText(t("models.deleted", model=name))
            self.models_changed.emit()
        self.refresh()

    # --- download --------------------------------------------------------------------------------

    def _set_pull_status(self, text: str) -> None:
        self.pull_status.setText(text)
        self.pull_status.setVisible(bool(text))

    def _update_pull_hint(self, name: str) -> None:
        entry = next((m for m in catalog.RECOMMENDED if m.name == name.strip()), None)
        if entry is None:
            self.pull_hint.setText(t("models.pull_hint"))
        else:
            self.pull_hint.setText(t(entry.description_key, size=entry.download_bytes / GB))

    def start_pull(self) -> None:
        name = self.pull_combo.currentText().strip()
        if not name:
            return
        if any(catalog.same_model(name, m.name) for m in self.installed()):
            self._set_pull_status(t("models.already_installed", model=name))
            return
        entry = next((m for m in catalog.RECOMMENDED if m.name == name), None)
        if entry is not None:
            folder = models_dir()
            free = free_bytes(folder)
            if free < entry.download_bytes * 1.05:
                self._set_pull_status(
                    t(
                        "setup.model.no_space",
                        need=entry.download_bytes / GB,
                        free=free / GB,
                        folder=folder,
                    )
                )
                return
        cancel = threading.Event()
        self._pull_cancel = cancel
        self.pull_progress.setValue(0)
        self.pull_progress.show()
        self.pull_cancel_button.show()
        self._set_pull_status(t("setup.model.progress_unknown", status=name))
        self._update_buttons()
        url = self.config.ollama.url

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

    def cancel_pull(self) -> None:
        if self._pull_cancel is not None:
            self._pull_cancel.set()

    def _on_progress(self, progress: PullProgress) -> None:
        if not progress.total:
            self._set_pull_status(t("setup.model.progress_unknown", status=progress.status))
            return
        self.pull_progress.setValue(round(1000 * progress.completed / progress.total))
        self._set_pull_status(
            t(
                "models.pull_progress",
                status=progress.status,
                done=progress.completed / GB,
                total=progress.total / GB,
                left=(progress.total - progress.completed) / GB,
            )
        )

    def _on_pull_finished(self, name: str, error: str) -> None:
        self._pull_cancel = None
        self.pull_progress.hide()
        self.pull_cancel_button.hide()
        if error == CANCELLED:
            self._set_pull_status(t("setup.model.cancelled"))
        elif error:
            self._set_pull_status(t("setup.model.failed", detail=error))
        else:
            self._set_pull_status(t("models.pulled", model=name))
            self.models_changed.emit()
        self._update_buttons()
        self.refresh()

    # --- side-by-side test -----------------------------------------------------------------------

    def _fill_compare_combos(self) -> None:
        names = [m.name for m in self.installed()]
        for combo, fallback in ((self.compare_a, 0), (self.compare_b, 1)):
            current = combo.currentData()
            items = names if combo is self.compare_a else ["", *names]
            if [combo.itemData(i) for i in range(combo.count())] == items:
                continue
            combo.clear()
            for name in items:
                combo.addItem(name or t("models.compare_none"), name)
            if current in items:
                combo.setCurrentIndex(items.index(current))
            elif combo is self.compare_a and self.config.ollama.gpu_model in items:
                combo.setCurrentIndex(items.index(self.config.ollama.gpu_model))
            else:
                combo.setCurrentIndex(min(fallback, combo.count() - 1))

    def compare_lines_text(self) -> list[str]:
        return [
            line.strip() for line in self.compare_lines.toPlainText().splitlines() if line.strip()
        ]

    def toggle_compare(self) -> None:
        if self._compare_cancel is not None:
            self._compare_cancel.cancel()
            self.compare_status.setText(t("models.compare_stopping"))
            return
        models = [m for m in (self.compare_a.currentData(), self.compare_b.currentData()) if m]
        models = list(dict.fromkeys(models))  # the same model twice is one run
        lines = self.compare_lines_text()
        if not models or not lines or self._busy:
            return
        self._compare_models = models
        self._compare_times = {m: [] for m in models}
        self._compare_runs = {}
        self.compare_table.setColumnCount(1 + len(models))
        self.compare_table.setHorizontalHeaderLabels([t("feed.english"), *models])
        self.compare_table.setRowCount(len(lines))
        for row, line in enumerate(lines):
            self.compare_table.setItem(row, 0, QTableWidgetItem(line))
            for column in range(1, 1 + len(models)):
                self.compare_table.setItem(row, column, QTableWidgetItem(""))
        header = self.compare_table.horizontalHeader()
        for column in range(self.compare_table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.compare_table.show()
        cancel = CancelToken()
        self._compare_cancel = cancel
        self.compare_status.setText(t("models.compare_loading", model=models[0]))
        self._update_buttons()
        settings = self.config.ollama.model_copy(deep=True)
        context = self.profile.translation.context
        glossary = dict(self.profile.translation.glossary)
        examples = dict(self.profile.translation.examples)

        def work() -> None:
            try:
                self._comparison(
                    models,
                    lines,
                    settings,
                    on_model=self._bridge.model_run.emit,
                    on_line=self._bridge.line_result.emit,
                    cancel=cancel,
                    game_context=context,
                    glossary=glossary,
                    examples=examples,
                    keep_loaded=settings.gpu_model,
                )
            finally:
                self._bridge.compare_finished.emit()

        threading.Thread(target=work, daemon=True).start()

    def _on_model_run(self, run: ModelRun) -> None:
        self._compare_runs[run.model] = run
        if run.error:
            self.compare_status.setText(
                t("models.compare_failed", model=run.model, detail=run.error)
            )
        else:
            self.compare_status.setText(t("models.compare_running", model=run.model))
        self._update_compare_header()

    def _on_line_result(self, result: LineResult) -> None:
        column = 1 + self._compare_models.index(result.model)
        if result.error:
            text = t("models.compare_line_failed", detail=result.error)
        else:
            text = f"{result.text}\n({result.elapsed_ms:.0f} ms)"
            self._compare_times[result.model].append(result.elapsed_ms)
        self.compare_table.setItem(result.index, column, QTableWidgetItem(text))
        self.compare_table.resizeRowToContents(result.index)
        self._update_compare_header()
        lines = self.compare_table.rowCount()
        if result.index == lines - 1:  # this model is done; the next one loads now
            following = self._compare_models[column:]  # column is 1-based: the next model
            if following:
                self.compare_status.setText(t("models.compare_loading", model=following[0]))

    def _update_compare_header(self) -> None:
        labels = [t("feed.english")]
        for model in self._compare_models:
            run, times = self._compare_runs.get(model), self._compare_times[model]
            parts = [model]
            if run is not None and not run.error:
                parts.append(t("models.compare_warm", seconds=run.warm_up_ms / 1000))
                if run.gpu_share is not None:
                    parts.append(t("models.compare_gpu", percent=round(run.gpu_share * 100)))
            if times:
                parts.append(t("models.compare_median", ms=statistics.median(times)))
            labels.append("\n".join(parts))
        self.compare_table.setHorizontalHeaderLabels(labels)

    def _on_compare_finished(self) -> None:
        cancelled = self._compare_cancel is not None and self._compare_cancel.cancelled
        self._compare_cancel = None
        if cancelled:
            self.compare_status.setText(t("models.compare_cancelled"))
        elif not any(run.error for run in self._compare_runs.values()):
            self.compare_status.setText(t("models.compare_done"))
        self._update_buttons()
        self.refresh()  # comparing loads and unloads models

    def shutdown(self) -> None:
        self._timer.stop()
        self.cancel_pull()
        if self._compare_cancel is not None:
            self._compare_cancel.cancel()
