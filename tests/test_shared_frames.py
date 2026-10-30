import multiprocessing
import threading
import time

import numpy as np
import pytest

from livesubs.engine.capture.base import SourceLostError, SourceUnavailableError
from livesubs.engine.capture.shared_frames import (
    SharedFrameReader,
    SharedFrameSource,
    SharedFrameWriter,
)

W, H = 64, 36


def _image(value: int) -> np.ndarray:
    return np.full((H, W, 3), value % 256, np.uint8)


@pytest.fixture
def writer():
    writer = SharedFrameWriter(W, H)
    yield writer
    writer.close()


def test_reader_sees_the_newest_frame(writer: SharedFrameWriter) -> None:
    reader = SharedFrameReader(writer.name)
    try:
        assert reader.shape == (H, W, 3)
        assert reader.read() is None
        for value in range(1, 6):
            writer.write(_image(value), float(value))
        result = reader.read()
        assert result is not None
        seq, frame = result
        assert seq == 5
        assert frame.timestamp == 5.0
        assert int(frame.image[0, 0, 0]) == 5
    finally:
        reader.close()


def test_frames_read_are_private_copies(writer: SharedFrameWriter) -> None:
    reader = SharedFrameReader(writer.name)
    try:
        writer.write(_image(1), 1.0)
        _seq, frame = reader.read()
        for value in range(2, 10):  # every slot overwritten
            writer.write(_image(value), float(value))
        assert int(frame.image.max()) == 1
    finally:
        reader.close()


def test_wrong_shape_is_refused(writer: SharedFrameWriter) -> None:
    with pytest.raises(ValueError):
        writer.write(np.zeros((H, W, 4), np.uint8), 1.0)


def test_unknown_name_is_unavailable() -> None:
    with pytest.raises(SourceUnavailableError):
        SharedFrameReader("livesubs-frames-does-not-exist")


def test_concurrent_reads_are_never_torn(writer: SharedFrameWriter) -> None:
    stop = threading.Event()

    def produce() -> None:
        value = 0
        while not stop.is_set():
            value += 1
            writer.write(_image(value), time.monotonic())

    thread = threading.Thread(target=produce)
    thread.start()
    reader = SharedFrameReader(writer.name)
    try:
        reads = 0
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            result = reader.read()
            if result is None:
                continue
            image = result[1].image
            assert image.min() == image.max()  # one frame, not a mix of two
            reads += 1
        assert reads > 50
    finally:
        stop.set()
        thread.join()
        reader.close()


def test_source_waits_for_newer_frames(writer: SharedFrameWriter) -> None:
    with SharedFrameSource(writer.name, "Test Card", 60.0) as source:
        assert source.info.name == "Test Card"
        assert source.screen_rect() is None
        assert source.latest_frame() is None
        writer.write(_image(1), time.monotonic())
        first = source.latest_frame()
        assert first is not None
        assert source.latest_frame() is first  # no copy for a frame already read
        assert source.wait_for_frame(first.timestamp, timeout=0.02) is None
        threading.Timer(0.02, lambda: writer.write(_image(2), time.monotonic())).start()
        newer = source.wait_for_frame(first.timestamp, timeout=2.0)
        assert newer is not None and int(newer.image[0, 0, 0]) == 2


def test_source_is_lost_when_the_writer_closes() -> None:
    writer = SharedFrameWriter(W, H)
    source = SharedFrameSource(writer.name)
    source.open()
    try:
        writer.write(_image(1), time.monotonic())
        assert source.latest_frame() is not None
        writer.close()
        with pytest.raises(SourceLostError):
            source.latest_frame()
    finally:
        source.close()


def test_source_is_lost_when_frames_stop(writer: SharedFrameWriter) -> None:
    with SharedFrameSource(writer.name) as source:
        source.stale_after_s = 0.05
        writer.write(_image(1), time.monotonic())
        assert source.latest_frame() is not None
        time.sleep(0.1)
        with pytest.raises(SourceLostError):
            source.latest_frame()


def _write_in_child(name: str, ready, done) -> None:
    writer = SharedFrameWriter(W, H, name=name)
    writer.write(_image(7), time.monotonic())
    ready.set()
    done.wait(10)
    writer.close()


def test_frames_cross_processes() -> None:
    context = multiprocessing.get_context("spawn")
    ready, done = context.Event(), context.Event()
    name = f"livesubs-frames-test-{time.monotonic_ns()}"
    child = context.Process(target=_write_in_child, args=(name, ready, done))
    child.start()
    try:
        assert ready.wait(30)
        with SharedFrameSource(name) as source:
            frame = source.latest_frame()
            assert frame is not None
            assert int(frame.image[0, 0, 0]) == 7
            # monotonic() is system-wide on Windows: the child's timestamp is recent here too
            assert 0 <= time.monotonic() - frame.timestamp < 30
    finally:
        done.set()
        child.join(10)
    assert child.exitcode == 0
