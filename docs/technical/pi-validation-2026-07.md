# Pi Validation — 2026-07 (no-motor, camera/CPU/mic only)

This is a **measurement** pass on the real Pi 5 (`izzy`), run while the physical
build is **mid-way — motors are NOT wired**. Everything here is deliberately
**hardware-safe**: camera, CPU, and microphone only. No `sudo`, no GPIO writes,
no motor commands, no `yalp reactive --backend real`, no `yalp calibrate`, no
`yalp hwtest` without `--dry-run`. For the software bring-up path and the
milestone→command map see [pi-bringup.md](./pi-bringup.md); for the physical
build see [hardware-runbook.md](./hardware-runbook.md).

**The headline (Gate H): person-detection is viable on a no-NPU Pi 5.** The
`person` MobileNet-SSD detector sustains **~26.6 Hz** and `hog` **~55 Hz** on the
real C270 — both an emphatic **GO** against `config.GATE_H_GO_HZ = 3 Hz`
(~8.8× and ~18× margin). No accelerator, no ONNX/ncnn swap needed to clear the gate.

---

## 1. Environment

| Field | Value |
|---|---|
| Host | `izzy` (`izzy@izzy.local`, key-auth SSH) |
| Model | **Raspberry Pi 5 Model B Rev 1.0** (BCM2712) |
| CPU | 4× cores, ARM @ 2.40 GHz |
| RAM | 4 GB (`MemTotal 4,146,320 kB`) |
| OS | Debian GNU/Linux 13 (trixie) |
| Kernel | `6.18.34+rpt-rpi-2712` (aarch64, 2026-06-09) |
| Python | 3.13.5 — venv at `/home/izzy/yalp/.venv` (system-site-packages) |
| GPIO stack | `gpiozero.pins.lgpio` (lgpio/native), `RPi.GPIO` absent |
| OpenCV | `opencv-python` **4.13.0** (full GUI build, Qt/xcb) — see §8 issue #1 |
| Storage | 117 GB, 4.7 GB used (5%) |
| Camera | **Logitech C270 HD** (`046d:0825`) on `/dev/video0` |
| Idle temp | ~51–57 °C. Under detector load: peaks **80–83 °C** with soft thermal throttling (see §5) |

Repo synced to `main` @ `61ac73a` on both the workstation (pushed to origin) and
the Pi (`git pull --ff-only`: `cc87d48..61ac73a`). `pip install -e .` (no `[pi]`
reinstall — gpiozero/lgpio already satisfied via system site-packages) succeeded.

---

## 2. GPIO stack verification (read-only)

`scripts/verify_gpio_stack.py` is a pin-**factory** check — it never drives a pin.

```
Pin factory module: gpiozero.pins.lgpio
GPIO stack OK — lgpio/native factory confirmed, RPi.GPIO absent.
EXIT_CODE=0
```

**PASS** — the required lgpio/native backend is in use and `RPi.GPIO` is not
loaded (both mandatory on Pi 5 per [hardware.md](./hardware.md)).

---

## 3. Full test suite on the Pi (fakes only)

Run: `~/yalp/.venv/bin/python -m pytest -q` from `~/yalp`. **570 tests collected.**

> ⚠️ **The full suite as-is ABORTS on the Pi** (SIGABRT, exit 134) at ~50%, in
> `tests/test_follow_runner.py::test_gui_available_is_boolean_and_never_raises`.
> Root cause is a **cv2 GUI abort**, not a yalp-logic failure — see §8 issue #1.
> Two tests (`test_gui_available_is_boolean_and_never_raises`,
> `test_preview_true_is_headless_safe`) both call `gui_available()` →
> `cv2.namedWindow()` → Qt `qFatal→abort()` on the headless Pi. An `abort()` is a
> native C++ call Python's `except Exception` cannot catch, so it kills the whole
> process.

With those two GUI-probe tests deselected, the rest of the suite runs:

```
6 failed, 562 passed, 2 deselected, 4 warnings in 32.33s     (~33 s wall)
```

The **6 failures are false-failures on real hardware, not product bugs** — each
one *passes in isolation* (`6 passed in 0.13s`). They are order-dependent: all six
are "module X imports without gpiozero present" tests that assert
`"gpiozero" not in sys.modules` as a **global** condition. That holds on a laptop
(gpiozero is genuinely unimportable) but not on a Pi, where gpiozero *is* present
and an earlier hardware test loads it into `sys.modules`. See §8 issue #2.

| Failing test (passes in isolation) |
|---|
| `test_hardware_config.py::test_config_import_does_not_import_gpiozero` |
| `test_hardware_interface.py::test_imports_with_no_hardware_libs_present` |
| `test_motor_driver.py::test_module_imports_without_gpiozero` |
| `test_real_backend.py::test_module_imports_without_gpio_libraries` |
| `test_real_backend.py::test_injected_fakes_skip_the_hardware_imports` |
| `test_ultrasonic.py::test_module_imports_without_gpiozero` |

**Net:** 562/570 pass on real hardware; 2 abort (cv2/Qt); 6 are order-dependent
false-failures. No yalp *logic* defect surfaced on the Pi.

---

## 4. DNN model pre-stage (`yalp follow --fetch-model`)

Ran online — succeeded (exit 0). The `person` detector is now offline-ready.

| File | Size (bytes) | sha256 |
|---|---|---|
| `MobileNetSSD_deploy.caffemodel` | 23,147,564 (~22.1 MiB) | `761c86fb…30f6af` |
| `MobileNetSSD_deploy.prototxt` | 29,353 (~28.7 KiB) | `e781559c…f0d25e` |

Cache location: **`/home/izzy/.cache/yalp/models`** (`config.FOLLOW_MODEL_CACHE_DIR`).

---

## 5. Gate H — person-detector fps on the Pi (THE key measurement)

Real **Logitech C270** attached (`/dev/video0`), headless, `person` model staged.
`config.GATE_H_GO_HZ = 3 Hz` sustained. The built-in `--benchmark` reports mean
throughput; a companion per-frame latency sampler (8 s per detector on real camera
frames) provides p50/p99.

### fps results

| Detector | Input px | `--benchmark` mean fps | Sampler median (1/p50) | Sampler worst-1% (1/p99) | Verdict vs 3 Hz |
|---|---|---|---|---|---|
| **person** (MobileNet-SSD DNN, robot default, orientation-agnostic) | 300 | **26.4 Hz** | **26.6 Hz** | **25.5 Hz** | 🟢 **GO** (~8.8×) |
| **hog** (OpenCV standing-body) | 384 | **56.0 Hz** | **55.3 Hz** | **37.0 Hz** | 🟢 **GO** (~18×) |

Latency (per-frame `detect()`), sampler:

| Detector | n / window | p50 ms | p99 ms | min ms | max ms | mean ms |
|---|---|---|---|---|---|---|
| person | 213 / 8 s | 37.6 | 39.2 | 36.5 | 39.5 | 37.5 |
| hog | 421 / 8 s | 18.1 | 27.1 | 17.1 | 27.3 | 18.8 |

The `person` distribution is extremely tight (p99 ≈ p50). `hog` is faster but has
a wider thermal tail (p99 latency 27 ms → 37 fps worst-case) — still ~12× the gate.

### Gate H verdict

**🟢 GO on a no-NPU Pi 5.** Track-by-detection is viable *with the heavier DNN
detector*, on CPU alone. The benchmark's own note ("the Pi is slower — we'd swap
in a faster detector") turns out unnecessary for clearing Gate H: even MobileNet-SSD
clears 3 Hz by ~8.8×. A NO-GO (≤1–2 Hz, forcing the blob/color fallback of
milestone M) is implausible given this margin.

> **Caveat — this is the detector-fps criterion of Gate H, measured without motors.**
> The roadmap's full Gate H asks for sustained fps "under real load (reactive loop
> + camera capture + **motor-PWM stress**)". Motor-PWM stress cannot be exercised
> until motors are wired. Given ~8.8× headroom on the *default* detector, concurrent
> load is very unlikely to drop it below 3 Hz — but the motor-loaded flavor remains
> a Pi-with-motors follow-up (§7).

### Thermals (vcgencmd)

| Run | temp before | temp after | `get_throttled` after |
|---|---|---|---|
| person benchmark (~18 s) | 52.1 °C | **80.7 °C** | `0xe0000` |
| hog benchmark (~18 s) | 52.7 °C | **81.2 °C** | `0xe0000` |
| latency sampler (~16 s) | 57.1 °C | **83.4 °C** | `0xe0008` |

Decoding: `0xe0000` = bits 17/18/19 → *arm-freq-capping / throttling / soft-temp-limit
**has occurred*** (sticky until reboot; no "currently" bits, **no under-voltage** →
thermal, not power). `0xe0008` adds bit 3 → *soft temperature limit **currently
active***. The Pi recovers to ~53–57 °C within ~30–60 s of idle.

> **Sustained-FOLLOW implication:** continuous CPU person-detection drives the Pi
> to 80–83 °C and engages soft thermal throttling. Even throttled, fps stays well
> above Gate H, but **for prolonged real FOLLOW the Pi wants active cooling**
> (fan/heatsink). Power is clean throughout (no under-voltage bit ever set).

---

## 6. Gate K ceiling proxy — `yalp bench --backend fake`

Fake drivers → **zero GPIO**. This is a **fake-hardware ceiling, NOT the real
Gate K** (the real Gate K needs `--backend real` with live motor writes + real
ultrasonic on the robot — motors required, §7).

```
backend   : fake — fake wheels (simulated)
detector  : face (FaceDetector @ 384px)   camera: webcam 640x480
ticks recorded            : 1107 over 20.01s
tick p50 / p95 / p99 (ms) : 17.95 / 18.48 / 18.63
Gate K (tick p99 < 33 ms budget) : PASS  (18.63 ms vs 33 ms)
```

**Tick p99 = 18.63 ms vs the 33 ms budget (`config.TICK_BUDGET_MS`) → PASS
(fake-hardware ceiling).** Note this bench runs the detector *synchronously on the
tick*, so p99 is dominated by face-detect time and still fits with ~14 ms to spare.
Temp 59.3 → 80.1 °C, `throttled=0xe0000`.

---

## 7. What still needs motors (out of scope for this pass)

Everything below is blocked on the wired drivetrain (DRV8833 + 2× TT motors +
4×AA) and cannot be measured now:

| Gate / Milestone | Why it needs motors |
|---|---|
| **Gate E — power/brownout (milestone F)** | PASS = no Pi resets AND `get_throttled` stays `0x0` under a **stall-heavy motor drive** AND motor-rail voltage stays above the driver's logic VIH. Requires driving real motors. |
| **Gate K real (milestone K)** | Tick **p99 < 33 ms** under real load: `yalp bench --backend real` on the Pi with **live motor writes** + real ultrasonic. §6 is only the fake-driver ceiling. |
| **Milestone H — "Hello motors"** *(NEXT)* | Drive wheels forward/turn/stop from Python through the driver. Not yet wired. |
| **Milestone J — collision-stop reflex** | Obstacle inside `SAFE_STOP_THRESHOLD_M` must zero the **motors** first; needs real motors + ultrasonic to demonstrate. |
| **Milestone M — it follows / explores** | The GO branch runs the (now Pi-confirmed) track-by-detection pipeline and keeps a walking person centered **on the bench loop** — needs motors to actually follow. |
| **Gate H, motor-loaded flavor** | The detector-fps criterion is already GO (§5); the "under motor-PWM stress" measurement is a Pi-with-motors follow-up. |

Adjacent, also out of scope here (need GPIO writes / sensor wiring, not motors):
**milestone G** (GPIO first-light / LED blink) and **milestone I** (HC-SR04
resistor-divider bring-up).

---

## 8. Microphone & voice-stack findings

**C270 built-in mic enumerates** — but not through `yalp audio --list`:

```
$ yalp audio --list
Audio device listing needs the optional 'voice' extra (sounddevice), which is
not available (No module named 'sounddevice'). ...
AUDIO_EXIT=1
```

`yalp audio --list` depends on `sounddevice` (the `[voice]` extra), which is **not
installed**, so it exits 1. At the ALSA level, however, the mic is present and
usable:

```
card 0: WEBCAM [C270 HD WEBCAM], device 0: USB Audio   # arecord -l
plughw:CARD=WEBCAM,DEV=0 / default:CARD=WEBCAM         # arecord -L
```

**Capture confirmed non-silent.** 3 s via `arecord -D plughw:CARD=WEBCAM,DEV=0
-f S16_LE -r 16000 -c 1 -d 3`:

```
wav: rate=16000Hz ch=1 frames=48000 dur=3.00s
samples: peak=2741 (8.4% FS)  rms=577.1 (1.76% FS)  rms=-35.1 dBFS
VERDICT: NON-SILENT (mic captures signal)
```

(Quiet room; clearly capturing ambient signal, not a dead/zeroed stream.)

### `[voice]` extra — installable from wheels, **no long compiles**

Wheel-availability probe (`pip download --no-deps --only-binary=:all:` — forces
wheels, so **no build can ever start**) on aarch64 / cp313. Every dependency,
including the C++/compile-risk ones, has a prebuilt wheel:

| Package | Wheel found (aarch64/cp313) |
|---|---|
| sounddevice | `0.5.5` (py3-none-any) |
| faster-whisper | `1.2.1` (py3-none-any) |
| **ctranslate2** | `4.8.0` (cp313 manylinux aarch64) ✅ |
| **onnxruntime** | `1.27.0` (cp313 manylinux aarch64) ✅ |
| av | `17.1.0` (abi3 manylinux aarch64) |
| tokenizers | `0.23.1` (abi3 manylinux aarch64) |
| huggingface-hub | `1.21.0` (py3-none-any) |

**Conclusion:** `pip install 'yalp[voice]'` on this Pi would install entirely from
wheels — no >10-minute compile. **One runtime caveat:** `sounddevice` needs the
system PortAudio library (`libportaudio2`), which is **not installed**
(`ldconfig -p` shows none). Installing it is `sudo apt install libportaudio2` —
out of scope for this no-sudo pass. Until then, even after a `[voice]` pip
install, `yalp audio --list` (and any `sounddevice` capture) will fail to import;
`faster-whisper`/`ctranslate2` do **not** need PortAudio.

---

## 9. Issues found (for follow-up — not motor-blocked)

**Issue #1 — cv2 GUI abort on the headless Pi (breaks the suite + `yalp follow --preview`).**
`pyproject.toml` pins full **`opencv-python`** (not `opencv-python-headless`).
Its bundled Qt ships only the `xcb` plugin; with no display, `cv2.namedWindow()`
calls `qFatal → abort()` (SIGABRT). That is a native C++ abort — Python's
`except Exception` in `follow_runner.gui_available()` **cannot** catch it, so the
"never raises, headless-safe" contract is violated and the process dies. This
aborts the full test suite (§3) and would hard-crash `yalp follow --preview` on
the real robot. `QT_QPA_PLATFORM=offscreen` does **not** help (the bundled Qt has
no `offscreen` plugin). *Recommended fix:* use `opencv-python-headless` on the Pi
(headless build; `namedWindow` then raises catchably), **and** have
`gui_available()` pre-check for a display (e.g. `DISPLAY`/`WAYLAND_DISPLAY`)
before probing, since a C++ `abort()` is uncatchable by design.

**Issue #2 — 6 tests are order-dependent false-failures on real hardware.**
The "module imports without gpiozero" tests assert `"gpiozero" not in sys.modules`
as a *global* condition. Valid on a laptop (gpiozero unimportable), invalid on a
Pi where an earlier hardware test loads gpiozero into `sys.modules`. All six pass
in isolation. *Recommended fix:* skip/guard these when hardware libs are actually
importable, or assert the "no side-effect import" in a fresh subprocess.

---

## 10. Summary

- **Gate H: 🟢 GO** — person **26.6 Hz** (p99 25.5), hog **55.3 Hz** (p99 37.0) on
  a no-NPU Pi 5; ≥ 8.8× the 3 Hz floor. Track-by-detection is viable on CPU.
- **Gate K (fake proxy): PASS** — tick p99 **18.63 ms** < 33 ms (ceiling, not the
  real gate).
- **GPIO stack**: lgpio/native confirmed. **Model**: staged offline-ready
  (~22.1 MiB). **Mic**: C270 captures non-silent audio; `[voice]` deps are all
  prebuilt wheels (no compiles), pending `libportaudio2`.
- **Tests**: 562/570 pass on hardware; 2 abort via a cv2/Qt bug (§8 #1); 6 are
  order-dependent false-failures (§8 #2).
- **Thermals**: sustained detection → 80–83 °C + soft throttling; fps stays over
  the gate but prolonged FOLLOW wants active cooling. Power is clean throughout.
- **Still needs motors**: Gate E (F), Gate K real (K), milestones H / J / M, and
  the motor-loaded flavor of Gate H.
