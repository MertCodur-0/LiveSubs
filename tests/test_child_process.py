"""Children (engine, device host) must not outlive a UI process that crashed or was killed."""

import ctypes
import multiprocessing
import time

from livesubs.device.host import DeviceHost
from livesubs.engine.process import EngineProcess

_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0


def _exits_within(pid: int, seconds: float) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return True  # already gone
    try:
        return kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def _ui_process(pids) -> None:
    engine, host = EngineProcess(), DeviceHost()
    engine.start()
    host.start()
    pids.put((engine.pid, host.pid))
    time.sleep(120)  # killed by the test long before


def test_children_exit_when_the_ui_process_is_killed() -> None:
    context = multiprocessing.get_context("spawn")
    pids = context.Queue()
    ui = context.Process(target=_ui_process, args=(pids,))
    ui.start()
    try:
        engine_pid, host_pid = pids.get(timeout=60)
        time.sleep(3)  # both children are up and watching
        ui.kill()  # TerminateProcess: no cleanup in the UI process at all
        ui.join(10)
        assert _exits_within(engine_pid, 10)
        assert _exits_within(host_pid, 10)
    finally:
        if ui.is_alive():
            ui.kill()
