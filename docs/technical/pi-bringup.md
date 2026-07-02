# Pi Bring-Up — from a fresh Raspberry Pi to a driving robot

This is the **software** bring-up path: install yalp on the Pi, prove the GPIO
stack, then walk each build milestone with the exact `yalp` command that
validates it. For the **physical** side — flashing the SD card, wiring the
drivetrain/sensor, the power gate — follow
[hardware-runbook.md](./hardware-runbook.md); this doc assumes the wiring matches
that runbook and the pin map below.

`src/yalp/config.py` is the single source of truth for every pin number and
threshold cited here; the tables below are a convenience snapshot, not a second
definition. All pins are **BCM** numbering.

---

## Connecting to the Pi

Key-based SSH is configured — no password required:

```bash
ssh izzy                 # via the ~/.ssh/config alias
ssh izzy@izzy.local      # explicit form (same key, same host)
```

All commands in this doc that run "on the Pi over SSH" use one of the above.

---

## 1. Install on the Pi

Run the idempotent provisioner (safe to re-run; sudo is needed for the `apt`
steps):

```
bash scripts/pi_setup.sh
```

It updates packages, installs the system deps (incl. `python3-lgpio`), creates
`.venv`, and `pip install -e`s yalp with the `[pi]` and `[dev]` extras. The
`[pi]` extra (`pyproject.toml`) is what pulls in **gpiozero + lgpio** — the only
two hardware libraries yalp needs at runtime. You can install just the runtime
hardware stack by hand with:

```
pip install -e '.[pi]'
```

Then **prove the GPIO stack is correct** before touching motors:

```
.venv/bin/python scripts/verify_gpio_stack.py
```

It must print that gpiozero's pin factory is the **lgpio / native** backend and
exit 0. It exits non-zero (and says so) if `RPi.GPIO` got loaded or a wrong pin
factory is in use — neither is allowed on Pi 5 (see hardware.md). Do not proceed
to the hardware checks until this passes.

> Everything below imports gpiozero/lgpio **lazily**, only when a real driver or
> sensor is actually constructed. That is why the whole codebase — and the full
> test suite — imports and runs on a laptop with no hardware libraries present.

---

## 2. Pin map (source of truth: `src/yalp/config.py`)

| Function | Pin | config constant | Notes |
|---|---|---|---|
| Left motor PWM (speed / AIN1) | GPIO12 | `MOTOR_LEFT_PWM_PIN` | hardware PWM0 |
| Right motor PWM (speed / BIN1) | GPIO13 | `MOTOR_RIGHT_PWM_PIN` | hardware PWM1 |
| Left motor DIR (AIN2) | GPIO17 | `MOTOR_LEFT_DIR_PIN` | plain GPIO |
| Right motor DIR (BIN2) | GPIO22 | `MOTOR_RIGHT_DIR_PIN` | plain GPIO |
| Driver STBY (standby, active-LOW) | GPIO24 | `MOTOR_STBY_PIN` | **TB6612FNG only.** The board in use is the **DRV8833**, which has **no STBY pin** and ties nSLEEP HIGH — so `MOTOR_STBY_PIN` (default `24`) is **inert / left unwired** here; the hardware layer only drives GPIO24 when `MOTOR_DRIVER_KIND=="tb6612fng"`. |
| Ultrasonic TRIG | GPIO5 | `ULTRASONIC_TRIG_PIN` | HC-SR04 trigger |
| Ultrasonic ECHO | GPIO6 | `ULTRASONIC_ECHO_PIN` | via 5 V→3.3 V level divider |

Driver kind is selectable via `MOTOR_DRIVER_KIND`; **the board in use is the
`drv8833` (the default)**, and `tb6612fng` is only a fallback if the DRV8833 runs
hot near its current limit. Every pin is also overridable by the matching `YALP_*`
env var (see `config.py`). If you change wiring, change it in `config.py` (or the
env), not here.

---

## 3. Milestone → command map

Each milestone has one repeatable command. On a laptop, the dry-run / fake paths
let you exercise the *logic* before the parts arrive; the real validation happens
on the Pi.

### GPIO / motors / ultrasonic first light
```
yalp hwtest --check gpio          # toggle GPIO17 (left DIR) — LED-blink first light
yalp hwtest --check motors        # nudge forward / left / right / stop — PUT ROBOT ON A STAND
yalp hwtest --check ultrasonic    # print 5 distance reads at ~3 Hz
yalp hwtest --check all           # run gpio, motors, ultrasonic, camera in sequence
```
Add `--dry-run` to run the exact same check logic against laptop fakes
(`FakeMotorDriver`, `FakeRangeSensor`, synthetic `Camera`) with no GPIO present —
that is your pre-flight on a Mac. Without `--dry-run` the checks construct the
real gpiozero drivers and drive real pins.

### Direction / veer fix (calibration)
If a wheel spins the wrong way, or the robot veers when it should go straight,
run the calibration wizard:
```
yalp calibrate
```
It measures the motor inverts and the speed / turn-rate model and persists them
to JSON (path overridable with `--out`). `RealReactiveBackend` loads that file on
startup and applies the inverts/trim and `max_speed_mps` / `turn_rate_dps`
automatically — so the miswire is fixed in software, no re-soldering.

### Collision-stop (the safety override)
Collision-stop is the reactive layer's non-negotiable safety override: an
obstacle inside `SAFE_STOP_THRESHOLD_M` (0.30 m), **or an unknown echo**, forces
`SAFE_STOP` / `BLOCKED` and zeroes the motors *first*, before anything else in
the tick — and it latches stickily. This is proven identical between the fake and
real backends by `tests/test_backend_conformance.py`, which parametrizes one set
of safety assertions over **both** backends (so any divergence fails the build).
That suite runs on a laptop with injected fakes; on the Pi the same code path is
fed by the real ultrasonic sensor. Independently, a daemon `MotorWatchdog`
(`src/yalp/reactive/watchdog.py`) zeroes the motors if the control loop ever
stops heartbeating — a last-resort dead-man's switch that does not depend on the
tick's own logic.

### Performance gates (Gate K / Gate H)
```
yalp bench --backend real --seconds 20
```
`yalp bench` drives the backend in FOLLOW mode (tracker + detector + steering +
live motor writes — the heaviest steady-state load) and prints:
- **Gate K** — tick **p99** vs `TICK_BUDGET_MS` (33 ms ≈ 30 Hz). Verdict is
  `PASS` when `p99 < budget`, i.e. the safety loop holds its deadline under load.
  `NO-GO` prints a hint (lower `--hz`, shrink `--resolution`, raise the re-detect
  interval, or move the detector off the tick).
- **Gate H** — sustained **detector fps** vs `GATE_H_GO_HZ` (3 Hz). Verdict is
  `GO` when `fps >= floor`, i.e. track-by-detection is viable; `NO-GO` hints to
  swap in a cheaper detector behind the `Detector` interface.

On a laptop the numbers are a **ceiling**, not the verdict (no NPU, synthetic
camera) — bench says so explicitly. The real GO/NO-GO must be re-measured **on
the Pi** with `--backend real`.

Pre-stage the `person` detector's MobileNet-SSD model **while the Pi is online**
with `yalp follow --fetch-model` (it downloads into `config.FOLLOW_MODEL_CACHE_DIR`
and prints the path); otherwise an offline robot silently degrades to face-only.

> **Gate H is already measured on the real Pi (no motors needed) — it's a GO.**
> A 2026-07 no-motor validation pass on `izzy` recorded the `person`
> MobileNet-SSD detector sustaining **~26.6 Hz** (and `hog` ~55 Hz) on the real
> C270 — ~8.8× the 3 Hz floor, on a no-NPU Pi 5. The fake-driver Gate K ceiling,
> the model pre-stage, the C270 mic trial, the `[voice]`-wheels probe, and two
> Pi-only bugs (a cv2/Qt headless abort; order-dependent test failures) are all
> written up in **[pi-validation-2026-07.md](./pi-validation-2026-07.md)**. The
> real Gate K and everything that spins a wheel still wait on motors.

### Integration (two loops, two machines)
On the Pi, start the reactive layer driving real hardware and **exposing the IPC
contract on the LAN** (bind all interfaces so the laptop can reach it):
```
# On the Pi:
yalp reactive --backend real --host 0.0.0.0     # serves IPC on all interfaces, port 8765
```
Add `--port <port>`, `--hz`, `--camera-source`, and `--detector` as needed. The
reactive process keeps ticking and stays safe whether or not a deliberative client
is attached (`--host 127.0.0.1`, the default, keeps it loopback-only).

On the laptop, point the agent at the Pi with `--host` (real body over WiFi;
**real-webcam eyes stay LOCAL** on the laptop):
```
# On the laptop:
yalp agent --host izzy.local                    # remote real body, local webcam eyes
yalp agent --host izzy.local --port 8765 "drive forward and tell me what you see"
```
With `--host` set, the agent builds a `RemoteReactiveBackend`
(`yalp.deliberative.remote_backend`) that speaks the same line-framed socket
contract — `DeliberativeClient` → the Pi's `ReactiveServer` — that
`tests/test_contract.py` exercises: Intents up, `RobotState` snapshots down,
single-slot last-write-wins, monotonic `seq`. It reconnects with exponential
backoff and, while the link is down, serves the last-known snapshot so the agent
degrades exactly as the WiFi-degradation gate specifies (the robot stays safe on
the Pi regardless). Without `--host` the agent runs its own local fake body
exactly as before. `--synthetic` controls the **local** camera in both modes.

> **Camera is local (known follow-up).** In remote mode the *body* is the Pi's
> real hardware but the *eyes* are still the laptop's webcam — `describe_scene`
> reads the machine-local camera. Streaming stills from the **Pi's** camera back
> over the contract is a documented follow-up, not yet built.

Verify the round-trip over loopback (no hardware) with
`tests/test_remote_backend.py`, which stands up an in-process `yalp reactive`
equivalent, connects the agent's `RemoteReactiveBackend`, round-trips an intent +
state (`speed_limit`, `goal_status`), then kills the server to assert the
reconnect/backoff and degradation behavior.

### WiFi-degradation gate (Milestone N)
The robot must not run away if the deliberative link drops. The guarantee — the
reactive layer keeps ticking and stays safe when the client disappears mid-command
(a `DRIVE_GOAL` still halts within its bounded open-loop duration, FOLLOW degrades
to a clean stop, an obstacle still forces `SAFE_STOP`, and no exception escapes
the tick/publish path) — is validated by `tests/test_wifi_degradation.py`. It
proves this on a laptop with a real `RealReactiveBackend` (injected fakes) by
connecting a `DeliberativeClient` over loopback and then **closing it mid-command**
to simulate the WiFi drop.

---

## 4. Laptop (fakes / dry-run) vs. what truly needs the Pi

| Command | On a laptop it validates… | What still needs the Pi |
|---|---|---|
| `yalp hwtest --check … --dry-run` | The check **logic** against fakes (no GPIO) | Real pin toggles, real motor motion, real echo timing — `yalp hwtest` without `--dry-run`, on the Pi |
| `scripts/verify_gpio_stack.py` | `classify_factory()` logic is unit-tested (`tests/test_pi_setup_scripts.py`) | The actual lgpio/native factory + "no RPi.GPIO" check — only meaningful on the Pi |
| `yalp calibrate` | — (measures real motion) | The whole wizard: it drives wheels and times motion, Pi-only |
| Collision-stop | `tests/test_backend_conformance.py` proves fake≡real safety semantics | Real ultrasonic feeding the same code path on the robot |
| `yalp bench` | Runs end-to-end headless; numbers are a **ceiling** | The real Gate K / Gate H GO/NO-GO verdicts (`--backend real` on the Pi) |
| `yalp reactive --backend real` | On a laptop with no GPIO, `bench`'s real backend transparently falls back to fakes | True real-hardware ticking + live motors |
| `yalp agent` | Full deliberative loop with real-webcam eyes + simulated body | Driving the **real** body: `yalp agent --host <pi>` connects over IPC to the Pi's `yalp reactive --backend real` (eyes stay local; remote Pi stills are a follow-up) |
| WiFi-degradation | `tests/test_wifi_degradation.py` fully proves the safety contract over loopback | A real WiFi drop between laptop and Pi (behavior is identical — the link is the same socket contract) |

In short: the **logic** of every milestone is laptop-testable (that is the whole
point of the fake/real seam, and why `pytest` is all-green with no hardware). What
genuinely needs the Pi is anything that touches a real pin, real motor motion,
real echo timing, the real lgpio factory, or a true measured performance verdict.
