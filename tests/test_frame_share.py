"""Tests for sharing the camera's latest frame between processes."""

from __future__ import annotations

import os
import time

import numpy as np

from yalp.camera import Camera
from yalp.frame_share import FramePublisher, frame_file_is_fresh


def _frame(value):
    return np.full((48, 64, 3), value, dtype=np.uint8)


def test_publisher_writes_atomically_and_reader_sees_updates(tmp_path):
    path = str(tmp_path / "state" / "latest.jpg")
    current = {"frame": None}
    publisher = FramePublisher(lambda: current["frame"], path, hz=50)

    assert publisher.publish_once() is False  # camera not warm yet: nothing written
    assert not os.path.exists(path)
    assert frame_file_is_fresh(path) is False

    current["frame"] = _frame(200)
    assert publisher.publish_once() is True
    assert frame_file_is_fresh(path) is True
    assert not os.path.exists(path + ".tmp")

    camera = Camera(source="shared", path=path, max_fps=100).start()
    try:
        first = camera.wait_for_frame(timeout=2.0)
        assert first is not None and abs(int(first.mean()) - 200) < 5

        current["frame"] = _frame(20)
        os.utime(path, (time.time() - 10, time.time() - 10))  # force a new mtime next
        assert publisher.publish_once() is True
        deadline = time.time() + 2.0
        while time.time() < deadline and int(camera.latest().mean()) > 100:
            time.sleep(0.01)
        assert abs(int(camera.latest().mean()) - 20) < 5

        os.remove(path)  # server gone: the last frame stays, nothing crashes
        time.sleep(0.05)
        assert camera.latest() is not None
    finally:
        camera.stop()


def test_stale_file_is_not_fresh(tmp_path):
    path = str(tmp_path / "latest.jpg")
    FramePublisher(lambda: _frame(90), path).publish_once()
    old = time.time() - 60
    os.utime(path, (old, old))
    assert frame_file_is_fresh(path, max_age_s=5) is False


def test_publisher_thread_runs_and_cleans_up(tmp_path):
    path = str(tmp_path / "latest.jpg")
    publisher = FramePublisher(lambda: _frame(90), path, hz=100).start()
    deadline = time.time() + 2.0
    while time.time() < deadline and publisher.frames_written < 2:
        time.sleep(0.01)
    publisher.stop()
    assert publisher.frames_written >= 2
    assert not os.path.exists(path)  # removed on stop so readers see it is gone


def test_publisher_survives_a_broken_frame_source(tmp_path):
    def broken():
        raise RuntimeError("camera exploded")

    assert FramePublisher(broken, str(tmp_path / "x.jpg")).publish_once() is False
