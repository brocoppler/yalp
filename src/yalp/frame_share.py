"""Share the camera's latest frame between processes through one JPEG file.

Only one process can hold the camera device. On the robot that is the reactive
server, which needs every frame for heading and following. The deliberative side
(``yalp agent`` / ``yalp telegram``) only needs an occasional still, so instead of
fighting over the device it reads the file the server keeps up to date.

* :class:`FramePublisher` — server side. A daemon thread that writes the newest
  frame as a JPEG a few times a second. It runs off the tick thread and replaces
  the file atomically, so a reader never sees a half-written image.
* :func:`frame_file_is_fresh` — client side. True when a server is publishing.

The matching reader is ``Camera(source="shared", path=...)`` in :mod:`yalp.camera`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from . import config

logger = logging.getLogger("yalp.frame_share")


def frame_file_is_fresh(
    path: Optional[str] = None, *, max_age_s: Optional[float] = None
) -> bool:
    """True if the shared frame file exists and was written recently."""
    path = path or config.FRAME_FILE
    max_age_s = config.FRAME_FILE_MAX_AGE_S if max_age_s is None else max_age_s
    try:
        return (time.time() - os.path.getmtime(path)) <= max_age_s
    except OSError:
        return False


class FramePublisher:
    """Periodically write ``get_frame()`` to ``path`` as a JPEG. Never raises."""

    def __init__(
        self,
        get_frame: Callable[[], Any],
        path: Optional[str] = None,
        *,
        hz: Optional[float] = None,
    ) -> None:
        self._get_frame = get_frame
        self.path = path or config.FRAME_FILE
        hz = config.FRAME_PUBLISH_HZ if hz is None else hz
        self._period = 1.0 / hz if hz > 0 else 0.5
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.frames_written = 0

    def start(self) -> "FramePublisher":
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="yalp-frame-publisher", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop publishing and remove the file so readers see the server is gone."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            os.remove(self.path)
        except OSError:
            pass

    def publish_once(self) -> bool:
        """Write one frame now. Returns True if a frame was written."""
        try:
            frame = self._get_frame()
            if frame is None:
                return False
            from .camera import encode_jpeg

            data = encode_jpeg(frame)
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, self.path)  # atomic: readers never see a partial file
            self.frames_written += 1
            return True
        except Exception as exc:  # noqa: BLE001 - sharing a frame must never hurt the server
            logger.debug("frame publish failed: %s", exc)
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.publish_once()
            self._stop.wait(self._period)


__all__ = ["FramePublisher", "frame_file_is_fresh"]
