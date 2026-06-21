"""Tests for the orientation-agnostic cv2.dnn PERSON detector (Gate H candidate).

NO model download, NO network, NO camera. The cv2.dnn net is MOCKED: a fake net
with ``setInput``/``forward`` returns canned raw SSD detections of shape
``(1, 1, N, 7)`` — ``[_, class_id, confidence, x1, y1, x2, y2]`` (corners in 0..1).
We assert the wrapper:

  * filters to the Pascal-VOC ``person`` class (index 15), dropping other classes;
  * applies the confidence threshold;
  * returns the LARGEST person box as the target;
  * yields a usable :class:`Detection` that drives the tracker + steering
    (center/far -> forward, off-center -> turn);
  * loads the model LAZILY (the loader is never called at import / construction,
    and never when a net is injected);
  * fails GRACEFULLY offline with instructions naming the files + cache path.

``--detector person`` selection (CLI + ``build_detector``) is covered too, without
ever loading a net (construction is lazy).
"""

from __future__ import annotations

import numpy as np
import pytest

from yalp import config
from yalp.reactive.follow import FollowController, REASON_REACHED
from yalp.reactive.person_tracker import (
    Detection,
    DnnPersonDetector,
    PersonTracker,
    build_detector,
    detect_interval_for,
    ensure_dnn_model_files,
)


# --------------------------------------------------------------------------- #
# A fake cv2.dnn net: forward() returns canned raw detections.
# --------------------------------------------------------------------------- #
class FakeNet:
    """Stand-in for a cv2.dnn Net: records setInput, returns canned forward()."""

    def __init__(self, raw: np.ndarray) -> None:
        self._raw = raw
        self.set_input_calls = 0
        self.forward_calls = 0

    def setInput(self, _blob) -> None:  # noqa: N802 - mirror cv2 API
        self.set_input_calls += 1

    def forward(self):
        self.forward_calls += 1
        return self._raw


PERSON = config.FOLLOW_DNN_PERSON_CLASS_ID  # 15


def _raw(rows) -> np.ndarray:
    """Build a (1, 1, N, 7) SSD output from [class, conf, x1, y1, x2, y2] rows."""
    arr = np.zeros((1, 1, len(rows), 7), dtype=np.float32)
    for i, (cls, conf, x1, y1, x2, y2) in enumerate(rows):
        arr[0, 0, i] = (0.0, float(cls), float(conf), x1, y1, x2, y2)
    return arr


def _frame(h: int = 240, w: int = 320, value: int = 180) -> np.ndarray:
    return np.full((h, w, 3), value, dtype=np.uint8)


def _exploding_loader(*_a, **_k):
    raise AssertionError("model loader must NOT be called when a net is injected")


# --------------------------------------------------------------------------- #
# Class filtering + confidence threshold + largest-box selection
# --------------------------------------------------------------------------- #
def test_filters_to_person_class():
    # A high-confidence NON-person (e.g. class 7 = car) must be dropped; only the
    # person row survives.
    net = FakeNet(_raw([
        (7, 0.99, 0.1, 0.1, 0.4, 0.9),       # car -> dropped
        (PERSON, 0.9, 0.4, 0.2, 0.6, 0.8),   # person -> kept
    ]))
    det = DnnPersonDetector(net=net, loader=_exploding_loader)
    out = det.detect(_frame())
    assert len(out) == 1
    assert isinstance(out[0], Detection)
    # The person box (frame 320x240): x1=0.4*320=128, w=(0.6-0.4)*320=64.
    assert out[0].bbox == (128, 48, 64, 144)
    assert abs(out[0].score - 0.9) < 1e-6


def test_applies_confidence_threshold():
    net = FakeNet(_raw([
        (PERSON, 0.10, 0.4, 0.2, 0.6, 0.8),  # below threshold -> dropped
    ]))
    det = DnnPersonDetector(net=net, confidence=0.5, loader=_exploding_loader)
    assert det.detect(_frame()) == []


def test_returns_largest_person():
    net = FakeNet(_raw([
        (PERSON, 0.8, 0.10, 0.10, 0.20, 0.30),  # small person
        (PERSON, 0.7, 0.30, 0.10, 0.90, 0.95),  # BIG person (the target)
    ]))
    det = DnnPersonDetector(net=net, loader=_exploding_loader)
    out = det.detect(_frame())
    assert len(out) == 1
    # The big box wins despite a lower score (nearest person = largest area).
    x, y, w, h = out[0].bbox
    assert w * h == max(
        int(round((0.20 - 0.10) * 320)) * int(round((0.30 - 0.10) * 240)),
        int(round((0.90 - 0.30) * 320)) * int(round((0.95 - 0.10) * 240)),
    )


def test_none_frame_returns_empty_without_touching_net():
    net = FakeNet(_raw([(PERSON, 0.9, 0.4, 0.2, 0.6, 0.8)]))
    det = DnnPersonDetector(net=net, loader=_exploding_loader)
    assert det.detect(None) == []
    assert net.forward_calls == 0


# --------------------------------------------------------------------------- #
# Lazy model loading
# --------------------------------------------------------------------------- #
def test_loader_is_lazy_not_called_at_construction():
    calls = {"n": 0}

    def spy(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("should not run")

    # Constructing the detector (no injected net) must NOT load the model.
    DnnPersonDetector(loader=spy)
    assert calls["n"] == 0


def test_injected_net_never_invokes_loader():
    net = FakeNet(_raw([(PERSON, 0.9, 0.4, 0.2, 0.6, 0.8)]))
    det = DnnPersonDetector(net=net, loader=_exploding_loader)
    det.detect(_frame())  # would raise via _exploding_loader if it loaded
    assert net.set_input_calls == 1 and net.forward_calls == 1


# --------------------------------------------------------------------------- #
# Graceful offline failure
# --------------------------------------------------------------------------- #
def test_missing_files_no_download_raises_with_instructions(tmp_path):
    with pytest.raises((FileNotFoundError, RuntimeError)) as exc:
        ensure_dnn_model_files(str(tmp_path), download=False)
    msg = str(exc.value)
    # The message must name the exact files and the cache path to drop them into.
    assert config.FOLLOW_DNN_PROTOTXT_NAME in msg
    assert config.FOLLOW_DNN_CAFFEMODEL_NAME in msg
    assert str(tmp_path) in msg


def test_download_failure_raises_clear_runtime_error(tmp_path):
    # Point at an unreachable URL scheme so the download attempt fails fast.
    with pytest.raises(RuntimeError) as exc:
        ensure_dnn_model_files(
            str(tmp_path),
            prototxt_url="http://127.0.0.1:0/nope.prototxt",
            caffemodel_url="http://127.0.0.1:0/nope.caffemodel",
            download=True,
        )
    msg = str(exc.value)
    assert config.FOLLOW_DNN_CAFFEMODEL_NAME in msg
    assert str(tmp_path) in msg


def test_existing_files_are_returned_without_download(tmp_path):
    proto = tmp_path / config.FOLLOW_DNN_PROTOTXT_NAME
    model = tmp_path / config.FOLLOW_DNN_CAFFEMODEL_NAME
    proto.write_text("layer {}")
    model.write_bytes(b"\x00\x01\x02")
    got_proto, got_model = ensure_dnn_model_files(str(tmp_path), download=True)
    assert got_proto == str(proto)
    assert got_model == str(model)


# --------------------------------------------------------------------------- #
# Selection: --detector person -> DnnPersonDetector (no net load)
# --------------------------------------------------------------------------- #
def test_build_detector_person_returns_dnn_detector():
    det = build_detector("person")
    assert isinstance(det, DnnPersonDetector)


def test_cli_accepts_person_choice():
    import argparse

    from yalp.reactive import follow_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    follow_cli.add_parser(sub)
    assert parser.parse_args(["follow", "--detector", "person"]).detector == "person"
    # The slow DNN detector uses the longer re-detect cadence (not the face one).
    assert detect_interval_for("person") == config.FOLLOW_DETECT_INTERVAL_TICKS


# --------------------------------------------------------------------------- #
# Full detect -> track -> steer with the mocked net (steering contract unchanged)
# --------------------------------------------------------------------------- #
def _steer_from_net(rows) -> "object":
    frame = _frame()  # 240x320
    net = FakeNet(_raw(rows))
    det = DnnPersonDetector(net=net, loader=_exploding_loader)
    pt = PersonTracker(detector=det, detect_interval=detect_interval_for("person"))
    res = pt.update(frame)
    return FollowController().decide(res, frame_w=320, frame_h=240, brightness=180)


def test_person_center_far_drives_forward():
    # Centered horizontally, SMALL box (far) -> forward, no turn.
    dec = _steer_from_net([(PERSON, 0.9, 0.44, 0.40, 0.56, 0.60)])
    assert dec.target_visible is True
    assert abs(dec.turn) < 1e-9
    assert dec.forward > 0.0


def test_person_offcenter_turns_toward():
    # Person off to the LEFT -> turn left.
    dec = _steer_from_net([(PERSON, 0.9, 0.02, 0.40, 0.18, 0.80)])
    assert dec.target_visible is True
    assert dec.turn < 0


def test_person_close_stops():
    # A large, centered box (close) -> hold (reached).
    dec = _steer_from_net([(PERSON, 0.9, 0.30, 0.05, 0.70, 0.95)])
    assert dec.target_visible is True
    assert dec.forward == 0.0
    assert dec.reason == REASON_REACHED


def test_person_detected_from_behind_still_tracks():
    """Orientation-agnostic: a back-turned body is just another person box.

    The detector is a BODY detector — it returns a person Detection regardless of
    facing — so the tracker locks on and steering centers/approaches exactly as for
    a front-facing person. (We can't feed real pixels here; the contract is that any
    person-class box, whatever the pose, becomes a usable target.)
    """
    dec = _steer_from_net([(PERSON, 0.85, 0.40, 0.20, 0.62, 0.85)])
    assert dec.target_visible is True
