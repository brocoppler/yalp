# Roadmap

> Build sequencing, the risk register, and the consolidated open questions for yalp v1 — the one place where every DECISION, RISK, and OPEN call lives.

---

This doc is the project's spine. It orders the work smallest-win-first, inserts the
go/no-go gates the raw milestone list hides, ranks what will actually bite, and
collects every pending decision so nothing rots in a side conversation. For *what*
each layer does see `architecture.md`; for *how* the code is shaped see
`software-spec.md`; for the parts and power see `hardware.md`; for who it's for see
`product-spec.md`.

> **THESIS —** The hard part of this robot is not the motors, the camera, or the
> cloud model — each of those is a solved, well-trodden hobby problem. The hard part
> is the *seam* between the fast local loop and the slow cloud loop. The sequencing
> below is built to de-risk that seam first and earliest, on the laptop, before any
> hardware can hide it.

---

## ⭐ Current status & next steps — read this first

> **Phase 1 (the brain) is COMPLETE and laptop-tested — 597+ tests passing.** The full
> two-loop brain runs on the laptop against the fake reactive backend ("real eyes / fake
> wheels"), and the whole **voice → follow → voice-stop loop works end to end**. **8 of 16
> rungs are green (0, A, B, C, D, E + the first two Pi-hardware rungs G and I)** plus voice
> OUTPUT *and* INPUT (milestone O is now functionally done on the laptop); the remaining
> hardware rungs (F, H, J–N) are **gated on the Pi 5 + Phase 2**. **Phase 2 / Wave 3
> hardware bring-up is UNDERWAY on the real robot — see the next note.** The full §3 status
> and the §1 ladder have the detail; this is the skim.

> **⚙️ HARDWARE BRING-UP UPDATE (Izzy, Phase 2 / Wave 3) — camera, vision, and the
> ultrasonic sensor are DONE on real hardware; motors are next.** The physical robot —
> named **Izzy** (Raspberry Pi 5, hostname `izzy`) — is up: flashed with Raspberry Pi OS
> Lite 64-bit (headless, SSH, hostname `izzy`, on WiFi, fully apt-updated), yalp installed
> from GitHub, and `scripts/pi_setup.sh` hardened during bring-up (venv now
> `--system-site-packages` so the apt `python3-lgpio` is visible; swig + python3-dev
> fallback; libgl1 + libglib2.0-0 so `import cv2` works on Lite — all committed). Green on
> the real Pi: **GPIO stack verified** (gpiozero lgpio/native factory, no RPi.GPIO —
> milestone **G**, `scripts/verify_gpio_stack.py` passes); **camera** (Logitech C270 USB —
> `yalp hwtest --check camera` grabs a 640×480 frame, **B** Pi-confirmed); **vision** (`yalp
> see` runs on Izzy — captures a frame and describes the scene via the Claude vision API,
> key in `~/yalp/.env`, **C** Pi-confirmed); and the **HC-SR04 ultrasonic** (milestone **I**
> — `yalp hwtest --check ultrasonic` returns real distances, a live 25-read stream tracked a
> hand at ~22/25 good reads). The as-built divider is **1 kΩ + 1.5 kΩ → 3.0 V** (kit had no
> 2 kΩ; pins/software unchanged) and the full as-built record is in `as-built-wiring.md`.
> **NEXT: motors** — DRV8833 + 2× TT gear motors + 4×AA pack (Gate E **F** → Hello motors
> **H**), not yet wired.

**DONE — the laptop phase (run any of these today):**

- **`yalp see`** — webcam still → Claude → spoken-style description. `--speak` to hear
  it, `--image PATH` for a file, free-text question supported. *(steps B, C ⭐)*
- **`yalp agent "..."`** — deliberative loop: Claude picks tools (`drive`, `turn`,
  `stop`, `look`, `check_distance`, `describe_scene`, `enter_follow_mode`, `explore`,
  `speak`, `set_speed_limit`), drives the fake robot, narrates. Flags `--speak`,
  `--synthetic`, `--steps N`, positional words or `--command`. *(steps A, D, E)*
- **`yalp follow --detector person --preview`** — track-by-detection FOLLOW. Detectors
  `face` (desk default) / `hog` / `person` (cv2.dnn MobileNet-SSD, orientation-agnostic,
  robot default) / `auto`; lost-grace hysteresis; flags `--preview`, `--benchmark`,
  `--seconds`, `--hz`, `--synthetic`. **The follow brain is fully built** — Gate H (L) is
  only a Pi fps confirmation.
- **Voice OUTPUT (TTS, `--speak`) is SHIPPED** (macOS `say` / Linux/Pi `espeak-ng`), and
  **voice INPUT (push-to-talk STT) is SHIPPED too** — `yalp agent --listen` records a ~5 s
  window and transcribes locally with faster-whisper (default model `tiny`). Saying
  "follow me" brings up the camera and follows live until you say **"stop"/"halt"**
  (hands-free) or hit Ctrl-C / `q`. Optional deps: `pip install -e ".[voice]"`; the base
  install and test suite need none. *(milestone O, both halves)*
- **Model routing:** Haiku default → Sonnet (multi-step `explore` / `need_more_reasoning`)
  → Opus (`describe_scene` full / read-text); model IDs env-overridable. **Adaptive
  thinking is attached only on Sonnet/Opus — Haiku does not support it.**

**NEXT TARGET — Raspberry Pi 5 bring-up:** move the proven brain onto the Pi 5 hardware
(follow `hardware-runbook.md`). What can proceed **NOW (no battery pack)**: flash
Raspberry Pi OS Lite 64-bit (headless, SSH, Wi-Fi); install Python 3.11+ + gpiozero/lgpio;
**GPIO first-light / LED blink (milestone G)**; build & bench-check the HC-SR04 1k/2k
voltage divider to ~3.3 V **(milestone I)**; wire the drivetrain + sensor power-off (§5) —
no soldering (parts pre-headered). What **WAITS on the inbound 4×AA NiMH battery holder**:
**Gate E power/brownout (F) → Hello motors (H) → collision-stop reflex (J) → the
load/detector-fps gates (K, L) → follow on hardware (M)** — anything where motors actually
spin. `RealReactiveBackend` is **already fully implemented** (tick loop, collision-stop,
motor paths, `MotorWatchdog` wired into `run()`); the hardware bring-up **confirms** it on
the real body — no backend code remains to write.

**THEN (post-v1):** kid-friendly enclosure and the rest of §6.

---

## 1. The build path — one canonical ladder

One sequence, ordered smallest-win-first. Every rung has a **self-certifiable
done-signal** — something you can *run or measure*, not just write — so you always
know whether a rung is green without opening another doc. The **magic moment** — the
first time the thing feels alive — lands at **C**, reachable on the bench with only
the Phase 1 hardware already in hand.

> **Progress: 8 of 16 green — the entire laptop phase plus the first two Pi-hardware
> rungs are DONE.** Steps **0, A, B, C, D, E** are green (plus voice OUTPUT **and** INPUT
> and the full follow brain — the whole voice → follow → voice-stop loop works end to end),
> all laptop-tested (**597+ tests passing**); and on the **real Pi 5 ("Izzy")** the
> power-off bring-up rungs are now green too — **GPIO first light (G)** and the **HC-SR04
> divider + first ranged read (I)** — with **B (camera)** and **C (vision)** re-confirmed on
> the actual hardware (`yalp hwtest --check camera`, `yalp see` both run on Izzy). The
> remaining hardware rungs (**F, H, J–N**) need the 4×AA pack + motors wired. **Next
> target: motors** — the motor-spinning rungs (Gate E **F**, Hello motors **H**, reflex
> **J**, fps gates **K/L**) are next now that the no-battery Pi-side steps are done. Update
> this line as each done-signal goes green.

| Step | Milestone / gate | Done-signal — self-certify with exactly this | Needs | ⭐ |
|---|---|---|---|---|
| **0** | **Order Phase 2 parts now** ✅ ORDERED 2026-06-20 | Phase 2 order placed (motors, driver, ball caster, HC-SR04, divider resistors, battery + holder, breadboard/wiring, 470–1000 µF + 0.1 µF caps, glue/zip-ties) the day Step A starts; order/tracking numbers recorded. Parts ship in parallel with laptop work. | Laptop / none | |
| **A** ✅ DONE | **Loop-to-loop contract** (intent/mode schema, preemption, shared state) | An agent stub and a fake reactive stub exchange **one hand-authored Intent and one RobotState over the REAL socket**, both printed to console. Documentation alone is **NOT** done. **Verify:** `python scripts/contract_demo.py` (prints the handshake and 'STEP A OK'); `pytest tests/test_contract.py`. | Laptop | |
| **B** ✅ DONE *(Pi-confirmed)* | **Hello eyes** — capture a photo | Photo captured and saved to disk; file opens and shows the scene. **Now also green on the real Pi 5 (Izzy):** `yalp hwtest --check camera` grabs a 640×480 frame from the C270 over USB. **Verify:** `yalp see --image PATH` (or a webcam grab via `yalp see`); `pytest tests/test_camera.py`. | Phase 1 (C270 / laptop cam) | |
| **C** ✅ DONE *(Pi-confirmed)* | **It sees and talks** | Photo → vision model "what do you see?" → the answer prints to console. **Now also green on the real Pi 5 (Izzy):** `yalp see` captures a frame and describes the scene via the Claude vision API (key in `~/yalp/.env`). **Verify:** `yalp see` (webcam still → spoken-style description; add `--speak` to hear it, or a free-text question); `python scripts/magic_moment.py`. | Phase 1 | ⭐ **the magic moment** |
| **D** ✅ DONE | **It acts** (agent loop) — three checkpoints | **D1:** model calls ONE tool and the fake backend prints the tool call. **D2:** a multi-step plan that reads RobotState back between steps. **D3:** the full agent loop runs on the laptop webcam. Done = **D3** green. **Verify:** `yalp agent "drive forward and tell me what you see"` (or `--synthetic`, `--steps N`, `--command`); `pytest tests/test_agent.py`. | Laptop (webcam stand-in) | |
| **E** ✅ DONE | **Laptop integration checkpoint** | The full agent loop drives the **FAKE** robot through a scripted scene end-to-end (command → Intent over the socket → fake reactive executes → RobotState updates → goal completes); the transcript prints/logs cleanly. Last all-software green before hardware. **Verify:** `python scripts/agent_demo.py` (drives the fake robot end-to-end and prints 'AGENT LOOP OK'); full suite `pytest` (597+ passing). | Laptop | |
| **F** | 🚦 **Gate E — Power / brownout bring-up** | PASS = **no Pi resets** AND `vcgencmd get_throttled` **stays 0x0** under a hard, stall-heavy motor drive script AND measured **motor-rail voltage stays above the driver's logic VIH**. NO-GO recovery (part of this milestone): add 470–1000 µF bulk + 0.1 µF ceramic across VM, twist/shorten motor leads, switch to NiMH cells, re-test. Motors do not move under Pi control until this passes. | Pi 5 + Phase 2 | |
| **G** ✅ DONE *(Pi)* | **GPIO first light** | On the real Pi 5, blink one LED / toggle one motor-driver input pin via **gpiozero**; confirm gpiozero reports the **lgpio** pin factory and that **no RPi.GPIO is anywhere in the import path** (RPi.GPIO does not work on Pi 5). Verify with a meter. **Green on Izzy:** `scripts/verify_gpio_stack.py` passes (gpiozero lgpio/native factory active, RPi.GPIO absent). | Pi 5 (+ one LED) | |
| **H** | **Hello motors** *(NEXT)* | Drive the wheels from Python through the driver: forward, turn, stop. **Not yet wired** — DRV8833 + 2× TT motors + 4×AA pack is the next bench step (gated on Gate E **F**). | Pi 5 + Phase 2 + F + G | |
| **I** ✅ DONE *(Pi)* | **HC-SR04 resistor-divider bring-up** | Build the ECHO divider; **meter the 3.3 V tap and confirm ≤ 3.3 V BEFORE it touches any GPIO pin**; then read one sane distance. **Green on Izzy:** wired with an as-built **1 kΩ + 1.5 kΩ → 3.0 V** divider (kit had no 2 kΩ; pins/software unchanged); `yalp hwtest --check ultrasonic` returns real distances (~22/25 good reads tracking a hand). Full as-built wiring: `as-built-wiring.md`. | Pi 5 + HC-SR04 + resistors | |
| **J** | **Safety reflex** (collision-stop) | A fast local loop overrides any drive command when something's too close: commanded forward drive halts within the threshold distance on the bench. In *before* any autonomous driving. | Pi 5 + Phase 2 | |
| **K** | 🚦 **Combined-load gate** (NEW) | **Reactive-tick p99 latency < 33 ms** with tracker + detector + capture + motor writes **all live simultaneously**; record the config. NO-GO recovery: drop detector cadence/resolution, move detection off the tick onto a slower thread feeding the tracker, re-measure. | Pi 5 + Phase 2 | |
| **L** | 🚦 **Gate H — Person-detector fps benchmark on Pi** | ⚠️ **Scope narrowed to a Pi fps benchmark — the brain is already built (laptop-tested ✅):** the follow *brain* — `Detector` interface, track-by-detection tracker, and steering logic — is **already implemented and laptop-tested** (`yalp follow`, `enter_follow_mode`; detectors `face`/`hog`/`person`/`auto`, where `person` is the **cv2.dnn MobileNet-SSD** — orientation-agnostic, works front/back/side — and is the robot default; lost-grace hysteresis; graceful lost/too-dark → stop). Gate H is now a **benchmark-confirmation only**: measure **SUSTAINED** detector fps at ~320×240 on the Pi under real load (reactive loop + camera capture + motor-PWM stress), record the triple **(model, resolution, runtime)**. Try ONNX Runtime or ncnn with int8. PASS: **≥ 3 Hz sustained = GO** (same pipeline, Pi confirmed). NO-GO: **≤ 1–2 Hz** → swap in the blob/color `Detector` behind the same interface; ship that as milestone **M** NO-GO. **Laptop fps baseline (already runnable):** `yalp follow --benchmark --detector person`. See `software-spec.md`. **→ DETECTOR-FPS CRITERION MEASURED GO (2026-07-01, real Pi 5 + C270, no motors):** `person` (MobileNet-SSD) ~26.6 Hz sustained (p99 25.5 Hz), `hog` ~55 Hz — vs the 3 Hz floor (~8.8× and ~18× margin). See [pi-validation-2026-07.md](./pi-validation-2026-07.md) §5. **Caveat:** this is the detector-fps criterion only; the motor-loaded flavor of Gate H (sustained fps under reactive loop + camera + motor-PWM stress) remains open until motors are wired. **Thermal note:** sustained detection drives the Pi to 80–83 °C with soft throttling (`0xe0000`/`0xe0008`); fps stays well above the gate but active cooling is recommended for prolonged FOLLOW. **Gate K fake-backend ceiling (§6 of validation doc):** tick p99 18.63 ms vs 33 ms budget — PASS as a fake-driver ceiling, NOT the real Gate K (real Gate K needs `--backend real` with live motor writes). | Pi 5 + Phase 2 | |
| **M** | **It follows / explores** (local tracker, no cloud round-trip; collision-stop underneath) | **GO branch** (Gate H ≥ 3 Hz): the laptop-proven track-by-detection pipeline runs on the Pi — robot keeps a walking person centered on the bench loop. **NO-GO branch** (Gate H ≤ 1–2 Hz): swap in the blob/color `Detector` behind the same pluggable interface — robot follows a colored target, collision-stop underneath, own bench demo. The follow *logic/steering* is already proven; only the Pi's detector fps is the open question Gate H answers. A NO-GO is a *different detector, not a demotion*. EXPLORE behavior per `architecture.md` / `software-spec.md`. | Pi 5 + Phase 2 + L (+ K) | |
| **N** | **WiFi-degradation test** (gated milestone) | Kill the link **mid-DRIVE_GOAL** and **mid-FOLLOW** and assert: DRIVE_GOAL **safe-stops within the bounded-goal timeout**; FOLLOW **continues locally then safe-stops on target loss**. Both asserts logged green. | Pi 5 + Phase 2 | |
| **O** ✅ DONE (laptop) | **It listens and speaks** *(separate track)* | **TTS OUTPUT SHIPPED ✅** (cross-platform: macOS `say` / Linux/Pi `espeak-ng`, the `--speak` flag on `yalp see` / `yalp agent`, headless-safe no-op if absent). **Voice INPUT SHIPPED ✅** — `yalp agent --listen` records a ~5 s window and transcribes locally with faster-whisper (default `tiny`, `YALP_STT_MODEL`); saying "follow me" follows live until a hands-free "stop"/"halt" (`--no-voice-stop` to disable). Optional `[voice]` extra (sounddevice + faster-whisper); base install + tests need none (fake STT + synthetic/file audio). Future: voice-activity-detection + a "hey Yalp" wake-word (still fixed-duration push-to-talk today). **Verify:** `pytest tests/test_stt.py tests/test_voice.py tests/test_microphone.py`. | Laptop today (mic optional via `[voice]`) | ⭐ |

> **DECISION —** **Item 0 is real work, not a footnote.** Ordering Phase 2 fires the
> day Step A starts, so motors, driver, HC-SR04, caps, and battery ship while the
> laptop phase (A–E) runs. There was previously no ordering step anywhere; this is it.

> **DECISION —** **Step A's done-signal is runnable, not written.** The contract is the
> single highest-risk, currently-unobservable task in the project. Forcing it to
> produce a live Intent/RobotState round-trip over the real socket converts it into the
> project's *first green check* instead of a doc nobody can falsify. See `software-spec.md`
> for the schema and `architecture.md` for the preemption model.

> **DECISION —** The magic moment is **C**, and it needs *no body*. Bench the vision
> path first; this is what keeps motivation high while the Phase 2 parts ship.

> **DECISION —** Voice (**O**) is a *separate track*, not a final step-in-sequence. It
> starts only after the full text loop (0–N) works, and it is allowed to lag
> indefinitely. The voice rabbit hole — mic quality, audio over USB, STT/TTS latency,
> wake-words — must never block the core magic.

> Legacy footnote: the original plan used an unordered 1–7 milestone list (Hello motors,
> Safety reflex, Hello eyes, It sees and talks, It acts, It follows, It listens). It is
> retired in favor of the single A–O ladder above; the seven intents survive inside it.

---

## 2. Gates, contracts & recovery paths

The raw milestone list was correct but incomplete: it hid the gates that sink hobby
robots when discovered late, and it let the riskiest task (the contract) hide behind
prose. This section gives the *why* behind the rungs above; the *pass/fail* criteria
live inline in the §1 table so you can self-certify without leaving the page.

### 2.1 The loop-to-loop contract must *run*, not just exist (Step A)

> **DECISION —** Start the loop-to-loop *contract* — the intent/mode schema, the
> safety-preemption rules, and the shared-state shape — on the laptop **immediately**,
> in parallel with B–E. It is pure software, needs zero hardware, and it dictates what
> the motor API (H) and the vision/agent API (C–D) must expose. Define the contract
> *before* building to it — and **prove it with a live socket round-trip** (Step A's
> done-signal), not a markdown file.

The contract is the artifact the whole two-loop architecture stands on. If the motor
code and the agent loop are built against two different mental models of "what is a
mode," **D** becomes a rewrite. A runnable stub-to-stub Intent/RobotState exchange
turns A, H, J, and M into implementations of an agreed, *demonstrated* interface
instead of negotiations. See `software-spec.md` and `architecture.md`.

### 2.2 Gate H — person-detector fps benchmark (decides M's detector, not its logic)

> **DECISION —** The follow *brain* — `Detector` interface, track-by-detection tracker,
> and steering — is **already implemented and tested laptop-side** (`yalp follow`,
> `enter_follow_mode`; detectors `face`/`hog`/`person`/`auto`, where `person` is the
> **cv2.dnn MobileNet-SSD** — orientation-agnostic, works front/back/side, robot default;
> lost-grace hysteresis; graceful lost/too-dark → stop). Gate H's role has therefore
> narrowed: it is a **benchmark-confirmation** that the chosen detector sustains ≥ 3 Hz on
> the Pi under concurrent load (reactive loop + camera capture + motor-PWM stress — never
> in isolation). Record the triple (model, resolution, runtime). Try ONNX Runtime or ncnn
> with int8, not just OpenCV DNN. A laptop fps baseline is already runnable via
> `yalp follow --benchmark --detector person`. See `software-spec.md`.

> **STATUS (2026-06-20) —** The follow *brain* is **already implemented and
> laptop-tested**: `yalp follow` / `enter_follow_mode` in `software-spec.md` ships the
> pluggable `Detector` interface, the track-by-detection tracker (detectors
> `face`/`hog`/`person`/`auto`, where `person` is the **cv2.dnn MobileNet-SSD** —
> orientation-agnostic, works front/back/side, robot default; the model is lazily
> downloaded + cached and gitignored), lost-grace hysteresis, proportional steering, and
> "lost / too-dark → stop" graceful behavior — all exercised against the fake reactive
> backend + real webcam. Gate H is therefore a **benchmark-confirmation only**: does the
> chosen detector sustain ≥ 3 Hz on the Pi under concurrent load? The laptop already
> provides a fps baseline via `yalp follow --benchmark --detector person`. The follow
> logic and steering are proven; the Pi's detector fps is the one remaining open question
> Gate H answers (and may force the lighter blob/color `Detector` fallback). See
> `software-spec.md`.

> **STATUS UPDATE (2026-07-01) — DETECTOR-FPS CRITERION: GO.** Measured on the real Pi 5
> (`izzy`) + Logitech C270, no motors: `person` (MobileNet-SSD) sustains **~26.6 Hz**
> (p99 25.5 Hz) and `hog` **~55 Hz** — both emphatic GO against the 3 Hz floor (~8.8× and
> ~18× margin). A NO-GO forcing the blob/color fallback is implausible. See
> [pi-validation-2026-07.md](./pi-validation-2026-07.md) §5. **The motor-loaded flavor of
> Gate H** (fps under reactive loop + motor-PWM stress) remains open until motors are wired
> — the ~8.8× headroom makes a sub-3 Hz outcome under concurrent load very unlikely.

The Pi 5 has no NPU (the AI HAT is deliberately skipped). The follow *logic/steering*
is proven; the Pi's *detector fps* is the remaining open question. The NO-GO path is
**not a dead end**: the blob/color tracker slots in behind the same pluggable `Detector`
interface as a **first-class deliverable** (milestone **M**, NO-GO branch). A low
number means you swap the detector — not redesign the follow loop. See `software-spec.md`.

### 2.3 Gate E — power / brownout bring-up (blocks motors-under-Pi)

> **RISK —** Motors and a Pi 5 sharing a power story is the #1 cause of mysterious
> reboots in hobby robots. Motor inrush and flyback drag the rail down and the Pi
> brownouts mid-command.

> **DECISION —** Insert the **power bring-up gate (Gate E, milestone F)** before any
> motor moves under the Pi: separate motor and Pi supplies, common ground, decoupling
> caps, flyback protection. PASS = **no Pi resets** AND `vcgencmd get_throttled`
> **stays 0x0** under a hard, stall-heavy drive script AND **motor-rail voltage stays
> above the driver's logic VIH**. The **NO-GO recovery is part of the milestone**, not
> a fork: add 470–1000 µF bulk + 0.1 µF ceramic across VM, twist/shorten motor leads,
> switch to NiMH cells, and re-test until it passes. See `hardware.md`.

### 2.4 Combined-load gate (NEW) — the seam under real simultaneous load

> **DECISION —** Before trusting the reactive layer in motion, prove the tick holds
> under everything at once: **reactive-tick p99 latency < 33 ms with tracker +
> detector + capture + motor writes all live simultaneously** (milestone **K**). The
> NO-GO recovery is a known sub-task: drop detector cadence/resolution, move detection
> off the tick onto a slower thread that only re-seeds the tracker, and re-measure.
> This catches the failure that Gates E and H each miss in isolation — the seam jams
> only when all four consumers fight for the same core. See `software-spec.md`.

### 2.5 Pi 5 GPIO reality — first light & the HC-SR04 divider

> **DECISION —** **GPIO first light (milestone G)** comes *before* Hello motors. Blink
> one LED / toggle one pin via **gpiozero**, confirm the **lgpio** backend is the
> active pin factory, and confirm **no RPi.GPIO is in the import path** — RPi.GPIO does
> not work on Pi 5 and will fail silently or wrongly. Debug the toolchain on an LED, not
> on a spinning motor.

> **DECISION —** The **HC-SR04 resistor-divider bring-up (milestone I)** is its own
> tiny checkable milestone: HC-SR04 ECHO is 5 V and the Pi's GPIO is 3.3 V-only.
> **Meter the divided tap and confirm ≤ 3.3 V BEFORE it ever touches a GPIO pin.** A
> mis-wired divider is a quiet way to kill a Pi input; metering first makes it a
> two-minute check instead of a mystery. See `hardware.md`.

### 2.6 WiFi-degradation is a gated test, not an assumption (milestone N)

> **DECISION —** Graceful degradation on WiFi loss gets a **gated milestone with an
> observable done-signal (N)**, not a hand-wave. Kill the link mid-DRIVE_GOAL and
> mid-FOLLOW; assert DRIVE_GOAL **safe-stops within the bounded-goal timeout** and
> FOLLOW **continues locally then safe-stops on target loss**. The reactive layer is
> network-independent by design — this milestone *proves* it. See `architecture.md`
> and `software-spec.md`.

---

## 3. Current status

> **Status as of this writing:** Phase 1 (the brain) is **COMPLETE and laptop-tested
> (597+ tests passing)**, and **Phase 2 / Wave 3 hardware bring-up is UNDERWAY on the real
> robot, "Izzy"** (Raspberry Pi 5, hostname `izzy`). **8 of 16 rungs green** — the entire
> laptop phase (**0, A, B, C, D, E**) plus the first two Pi-hardware rungs, **GPIO first
> light (G)** and the **HC-SR04 divider (I)**, with **B (camera)** and **C (vision)**
> re-confirmed on Izzy itself — plus voice OUTPUT *and* INPUT and the full follow brain, so
> the whole **voice → follow → voice-stop loop works end to end** (milestone O is
> functionally done on the laptop). **On the real Pi today:** Pi OS Lite headless over SSH,
> yalp installed, GPIO stack verified (gpiozero lgpio/native, no RPi.GPIO), the C270 camera
> grabs frames (`yalp hwtest --check camera`), `yalp see` describes the scene via Claude,
> and the HC-SR04 returns real distances (`yalp hwtest --check ultrasonic`, as-built
> 1 kΩ + 1.5 kΩ → 3.0 V divider — see `as-built-wiring.md`). **The next target is motors**
> (DRV8833 + 2× TT + 4×AA): Gate E **F** → Hello motors **H** → reflex **J** → fps gates
> **K/L**. **Laptop brain covers all three headline behaviors:**
> **see** (`yalp see` — webcam still → Claude → spoken-style description,
> `--speak`/`--image`/free-text), **agent** (`yalp agent` — full deliberative loop D1–D3
> driving the fake reactive backend, with `--listen` push-to-talk voice input), and
> **follow** (`yalp follow` / `enter_follow_mode` — track-by-detection pipeline;
> detectors `face`/`hog`/`person`/`auto`, where `person` is the cv2.dnn MobileNet-SSD
> (orientation-agnostic, robot default); lost-grace hysteresis, steering, graceful
> lost/too-dark → stop). The Pi is the remaining unknown for detector fps (Gate H /
> milestone L) — the follow logic itself is already proven.

- **Spent (~$200):** the CanaKit Raspberry Pi 5 4GB Starter Kit PRO (board, 27W PD
  wall supply, active-cooling case, 128GB OS card) plus the Logitech C270 webcam.
- **Remaining (~$135):** microSD card reader (if no laptop SD slot), Phase 2 body
  parts (~$105), Phase 3 voice (~$20). Total project ~$335.
- **Phase 2 ORDERED 2026-06-20 (inbound):** motors, driver, ball caster, HC-SR04,
  divider resistors, battery + holder, breadboard/wiring, bulk + ceramic caps,
  glue/zip-ties.
- **Phase 3 deferred:** USB mic + small speaker.
- **Laptop brain covers all three headline behaviors:** `yalp see` (**see**), `yalp agent` (**agent**), and `yalp follow` (**follow**) are all implemented and laptop-tested; the follow pipeline (`Detector` interface, track-by-detection tracker, steering, lost/too-dark → stop) was built ahead of the hardware.

**DONE — already shipped and laptop-tested (597+ tests passing):**

1. **Milestone 0 fired** — Phase 2 order placed 2026-06-20; parts inbound.
2. **Laptop phase (A–E) green:** the loop contract proven with a live socket round-trip
   (**A**), "it sees and talks" benched (**B–C**, the magic moment), the agent loop built
   in three checkpoints (**D1–D3**), and the fake-robot integration run closed (**E**).
3. **Beyond the ladder — milestone O is functionally done on the laptop:** voice OUTPUT
   (TTS, `--speak`), voice INPUT (push-to-talk STT, `yalp agent --listen` → local
   faster-whisper), voice-driven FOLLOW with a hands-free "stop"/"halt", and the full
   follow brain (`yalp follow` — pluggable `Detector`, cv2.dnn MobileNet-SSD person
   detector, track-by-detection tracker, lost-grace hysteresis, steering, graceful stop).
   The whole **voice → follow → voice-stop loop works end to end** on the laptop.
4. **Pi-side bring-up started on Izzy (no battery pack needed):** Pi 5 flashed with
   Raspberry Pi OS Lite 64-bit (headless, SSH, hostname `izzy`, WiFi, apt-updated); yalp
   installed from GitHub (`scripts/pi_setup.sh` hardened during bring-up — venv
   `--system-site-packages` for the apt `python3-lgpio`, swig + python3-dev fallback,
   libgl1 + libglib2.0-0 for `import cv2` on Lite — all committed); **GPIO first light
   (G)** verified (`scripts/verify_gpio_stack.py`, lgpio/native factory, no RPi.GPIO);
   **camera (B)** and **vision (C)** re-confirmed on Izzy; and the **HC-SR04 (I)** wired
   and reading real distances (as-built **1 kΩ + 1.5 kΩ → 3.0 V** divider — see
   `as-built-wiring.md`).

**NEXT TARGET — motors on Izzy** (follow `hardware-runbook.md`): wire the drivetrain and
prove it under power. The no-battery Pi-side steps (above) are done; what's left needs the
4×AA pack + motors:

1. **The motor chain:** wire the **DRV8833 + 2× TT gear motors + 4×AA NiMH pack** (separate
   VM rail, common ground), then **Gate E power/brownout (F)** → **Hello motors (H)** →
   **safety reflex (J)** *before* any autonomous driving → the load/fps gates (**K**, **L**
   — Gate H is now just a Pi fps confirmation of the already-built follow brain) → follow on
   hardware (**M**) → the WiFi-degradation test (**N**) — anything where motors actually
   spin. Record the as-built motor wiring in `as-built-wiring.md` (the §3 motor
   as-built table) as it goes in.
2. **Then:** bring `RealReactiveBackend` (already fully implemented — tick loop,
   collision-stop, motor paths, `MotorWatchdog`) up on the real body and confirm the
   same brain drives real wheels. No backend code remains to write.

> **THESIS —** Develop the brain on the laptop, not the Pi. Only motor-control and
> camera-capture genuinely need the Pi. Iterating the vision/agent loop over SSH on a
> headless Pi is painfully slow — and there's no reason to debug Python *and* hardware
> *and* Linux all at once. See `software-spec.md`.

---

## 4. Risk register

Ranked by how much each will actually bite — top of the table is where to spend worry.

| # | Risk | Why it bites | Severity | Mitigation / where addressed |
|---|---|---|---|---|
| 1 | **The loop-to-loop interface is the hard part and the least specified** | Intent/mode schema, safety preemption, and shared state are the seam the whole two-loop design rests on. If motors and the agent loop are built against different mental models, **D** becomes a rewrite. | 🔴 Critical | Make Step A's done-signal a **runnable** Intent/RobotState round-trip over the real socket — the project's first green check (§2.1). `software-spec.md` |
| 2 | **Person-following physics on a Pi 5 with no NPU** | A real person *detector* may not hit usable fps without an accelerator; follow-mode could stutter and lose a moving target. | 🔴 Critical | **Gate H (milestone L)** under concurrent load decides the build; both branches — track-by-detection (GO) and the blob/color tracker (NO-GO) — are first-class deliverables (M) (§2.2). `software-spec.md` |
| 3 | **A single front ultrasonic is thin** | Narrow cone, side/rear blind spots, silent failures on glass and soft/angled surfaces. | 🟠 High | **Settled in `hardware.md`:** v1 brings up one front HC-SR04 (milestone I); whether to add corner sensors is owned there, not an open roadmap question. Post-collision behavior settled in `architecture.md`. |
| 4 | **No encoders = open-loop control** | Without wheel encoders, distance and heading are guesses; the robot drifts and "back up 30cm" is dead reckoning. | 🟠 High | Accept open-loop for v1; lean on vision feedback in follow mode; encoders are a post-v1 upgrade. `hardware.md` |
| 5 | **Power / brownout** | Motor inrush and flyback drag the shared rail down and the Pi reboots mid-command — the #1 cause of mysterious resets. | 🟠 High | **Gate E (milestone F)** with an inline pass/fail and a built-in NO-GO recovery path; the **combined-load gate (K)** catches the under-load case (§2.3–§2.4). `hardware.md` |
| 6 | **Voice STT under real far-field/motor-noise conditions** | The product's pitch is "talk to it." Voice **OUTPUT (TTS) and INPUT (push-to-talk STT) are both shipped** on the laptop; the remaining risk is robustness — far-field STT on a cheap mic, over USB, near motor noise on the Pi is genuinely hard, and today's loop is fixed-duration push-to-talk (no VAD/wake-word yet). | 🟡 Medium | Voice track (**O**) is done laptop-side; on the Pi keep `tiny` model + push-to-talk, revisit VAD / "hey Yalp" wake-word later. `software-spec.md`, `product-spec.md` |
| 7 | **Graceful degradation on WiFi loss** | The deliberative layer needs WiFi; if it drops mid-roam the robot must not go haywire. | 🟡 Medium | Reactive layer is network-independent by design, and the **WiFi-degradation test (milestone N)** proves safe-stop on link loss with observable asserts (§2.6). `architecture.md`, `software-spec.md` |

> **RISK —** Risks 1 and 2 are the ones that can invalidate the *design*, not just
> cost time — which is exactly why §1–§2 front-load runnable work against both before
> any code commits to them. Everything below #2 is a known engineering cost, not an
> unknown.

---

## 5. Consolidated open questions

Every still-pending decision in one place. Items the sibling docs have now settled are
converted to DECISIONs and cross-linked here so the backlog reflects reality.

> **DECISION —** **Final sensor count — settled in `hardware.md`.** v1 brings up one
> front HC-SR04 first (milestone I); whether to add corner sensors for the blind spots
> in risk #3 is owned by `hardware.md`, not tracked as an open roadmap question.
> → `hardware.md`, `architecture.md`

> **DECISION —** **Follow-mode tracker choice — decided by Gate H.** The §2.2 fps spike
> decides it: ≥ 3 Hz sustained → track-by-detection; ≤ 1–2 Hz → the blob/color tracker.
> Both branches are first-class deliverables (milestones L→M), so this is no longer open
> — only the measured number is pending. → `software-spec.md`

> **DECISION —** **Post-collision behavior — settled in the sibling docs.** After a
> collision-stop fires, the response (back off / rotate to clear / halt and escalate to
> the deliberative layer) is specified in `architecture.md` with the open-loop caveat of
> risk #4. Roadmap no longer tracks it as open. → `architecture.md`, `hardware.md`

> **DECISION —** **EXPLORE mode — settled in the sibling docs.** EXPLORE behavior is
> specified in `architecture.md` / `software-spec.md` and reflected in milestone M;
> roadmap defers to those rather than holding it open. → `architecture.md`, `software-spec.md`

> **DECISION —** **Model / provider choice — settled and implemented.** Provider is
> Anthropic (Claude). The per-step loop runs on **Haiku** by default and escalates to
> **Sonnet** for multi-step work (`explore` / `need_more_reasoning`) and to **Opus** for
> the hardest vision (`describe_scene` full / read-text). Model IDs are env-overridable
> (`YALP_MODEL_FAST` / `_MID` / `_BIG`). **Thinking-capability gating:** adaptive
> "thinking" is attached **only on Sonnet/Opus — Haiku does not support it** (sending it
> would 400), so the router never asks Haiku to think. → `software-spec.md`,
> `architecture.md`

> **DECISION —** **Voice INPUT (STT) — settled and implemented.** Voice **OUTPUT** (TTS,
> `--speak`) and **INPUT** (push-to-talk STT, `yalp agent --listen` → local faster-whisper,
> default `tiny`) are **both shipped on the laptop**, including voice-driven FOLLOW with a
> hands-free "stop". What remains OPEN is only robustness/UX: far-field STT near motor
> noise on the Pi, and whether to add voice-activity-detection or a "hey Yalp" wake-word
> (today's loop is fixed-duration push-to-talk). → `software-spec.md`, `product-spec.md`

> **OPEN —** **How much should v1 anticipate the kid version?** Where do we spend now
> to avoid a rewrite later (interfaces, modes) versus deliberately throwaway cardboard
> scaffolding? → `product-spec.md`

> **OPEN —** **Roaming power budget.** Which PD power bank, and does the ~1–1.5h
> runtime estimate survive motors + Pi + camera under real load? Gates **E** and **K**
> inform this, but the bank choice is still open. → `hardware.md`

---

## 6. Future / stretch (post-v1)

Explicitly out of scope for v1, recorded so good ideas don't get re-litigated as if
they were on the critical path.

- **Kid-friendly enclosure** — cute, durable, safe; small parts and lithium handled
  responsibly. This is the v1 → kid-version transition. See `product-spec.md`.
- **Smooth mode** — real-time streaming multimodal so the deliberative layer narrates
  and decides continuously instead of in photo-by-photo bursts. Heavier and pricier
  over home WiFi while driving motors. See `architecture.md`.
- **Better navigation** — depth camera or LiDAR + SLAM for reliable "go to the
  kitchen." This is what unlocks named-place navigation, which v1 explicitly punts.
- **A stronger on-device tracker** — smoother person-following and the beginnings of
  room-mapping, beyond what the v1 blob/detector fork can do.
- **Onboard AI (e.g. Jetson Orin Nano)** — move inference on-device to cut latency and
  the cloud dependency. Directly attacks risks #1, #2, and #7.
- **The dream: balance-on-a-ball.** A ballbot that actually balances — a hard control
  problem deliberately saved for last, after the wheeled base is boring and reliable.

> **THESIS —** Every stretch item maps to a v1 limitation we chose on purpose. v1
> exists to make the two-loop architecture real and boring; the stretch list is what
> gets exciting *only once that foundation holds*.
