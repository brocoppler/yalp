# Hardware Bring-Up Runbook — the session you do when the parts arrive

> The hands-on, checklist-style guide for building yalp's body: flash the Pi, prove the GPIO stack, wire the drivetrain and sensor, pass the power gate, and drive the wheels — in order, with copy-pasteable commands and the exact pass/fail checks. `roadmap.md` says *what order and why*; this says *exactly what to do this session*.

---

This runbook is the operational companion to the specs. It does not re-decide
anything — it executes the settled plan. For the parts, power architecture, and
the canonical pin map, see `hardware.md`; for the build sequence and the go/no-go
gates, see `roadmap.md`; for the two-process split you are deploying, see
`software-spec.md`. The laptop side is already done in `SETUP.md` — this picks up
the moment the Raspberry Pi 5 and the Phase 2 body parts are on your bench.

> **What already exists today (before you touch any hardware).** The whole
> deliberative brain is **built and laptop-tested** against a *fake* reactive
> backend, so you can exercise it now on the laptop webcam:
> - `yalp see [question] [--image PATH] [--speak]` — vision Q&A on a still.
> - `yalp agent "<command>"` (or `--command`, `--steps`, `--synthetic`, `--speak`)
>   — the full tool-use agent loop driving the fake wheels with real vision.
> - `yalp follow` (`--detector face|hog|person|auto`, `--preview`, `--benchmark`,
>   `--seconds`, `--hz`, `--synthetic`) — **FOLLOW, including the real person
>   detector, is testable NOW**: the track-by-detection loop, the pluggable
>   `Detector` (the `person` cv2.dnn body detector tracks you front/back/side), and
>   the lost-grace/coast-then-stop behavior all run on the laptop. `--benchmark`
>   even gives a laptop fps baseline for the Gate H comparison in §9.
>
> So this runbook's job is *only* to bring the **body** up and marry it to that
> already-working brain. Two notes carried in from `software-spec.md`: **voice
> INPUT (mic + STT) has SHIPPED** — `yalp agent --listen` records a push-to-talk
> window and transcribes it locally with faster-whisper (optional `[voice]` extra);
> voice OUTPUT (TTS, the `--speak` flag) also ships. Both are laptop-proven and
> carry over to the Pi once a mic/speaker is present. And the model tiers use
> **capability-gated thinking** — the fast per-step tier (Haiku) has **no** extended
> thinking (sending it 400s), only the Sonnet/Opus escalations do.

**Who does what.** You — the owner — do the physical build: wire the breadboard,
meter voltages, plug things in, run the commands below, and report the result.
Where a step needs *code* written (most importantly implementing the on-Pi motor
and sensor backend), that work is **dispatched to the retinue (Woland)** — it is
called out inline as **⚙️ RETINUE**. You never have to write Python; you wire,
run, and verify.

**How to read a step.** Each numbered section is one bench task with a **DO**
(what to physically do / run) and a **DONE WHEN** (the concrete signal that proves
it worked — the same self-certifiable done-signal as the matching `roadmap.md`
rung). Do not advance past a gate until its DONE WHEN is green.

**Roadmap mapping.** The sections below are ordered for a productive bench
session, which is *not* identical to the milestone letters in `roadmap.md` — cheap,
power-off wins (GPIO first light, the divider bench check) come first because they
need almost no hardware and de-risk the toolchain before any motor power is
present. The mapping: §3 → milestone **G**, §4 → **I**, §6 → **Gate E / F**,
§7 → **H**, §8 → **J**, §9 → **Gate H / L** (+ combined-load **K**). The roadmap's
own rule still holds: **both Gate E (§6) and GPIO first light (§3) must be green
before any motor turns under the Pi (§7).**

---

## 0. Before you start — parts in hand & safety preamble

Lay everything out and confirm you have it before you power anything on. From the
Phase 2 order in `hardware.md` and `roadmap.md`:

**Brain (Phase 1, already in hand):**

- [ ] Raspberry Pi 5 4GB (CanaKit Starter Kit PRO) — board, **27W USB-C PD wall
      supply**, active-cooling case
- [ ] microSD card (128GB, in the kit) + a way to write it (laptop SD slot or USB
      card reader)
- [ ] Logitech C270 USB webcam

**Body (Phase 2):**

- [ ] 2× TT DC gear motors + wheels
- [ ] **DRV8833** motor-driver breakout (the board in use; TB6612FNG is a fallback only if the DRV8833 runs hot) — get the pre-soldered-header version
- [ ] HC-SR04 ultrasonic sensor
- [ ] NiMH AA cells + 4×AA holder + a NiMH charger (**not alkaline** — see
      `hardware.md` §1)
- [ ] 1–2× ball caster (the passive third contact point)
- [ ] mini breadboard + jumper wires (M-M, M-F, F-F)
- [ ] **1 kΩ and 2 kΩ resistors** for the echo voltage divider
- [ ] **470–1000 µF electrolytic** cap + **0.1 µF ceramic** cap for the motor rail
- [ ] cardboard / hot glue / zip ties for the chassis
- [ ] (handy) a **multimeter** — you cannot pass Gate E without metering voltage

**⚠️ Safety preamble — read this once, follow it every session:**

- **Power OFF while wiring.** Unplug the Pi PD supply *and* pull the NiMH pack (or
  leave its holder switch off) before changing any connection. Hot-wiring a Pi
  header is how pins die.
- **Common ground is mandatory.** The motor supply's GND and a Pi GND pin must be
  tied together, or the driver sees garbage logic levels (`hardware.md` §2). This
  is the one wire people forget.
- **Never wire the 5V HC-SR04 ECHO straight to a 3.3V GPIO.** It goes through the
  1k/2k divider (§4). 5V into a 3.3V-only pin can fry the input.
- **Never run motors off the Pi's 5V pin, and never back-power the Pi from the
  motor rail.** Two separate supplies, one shared ground — full stop.
- **Double-check polarity** on the electrolytic cap and the battery holder before
  applying power. Electrolytics are polarized; backwards is a pop.

> **RISK —** The power story (motor stall spikes browning out the Pi over a shared
> rail) is the single most failure-prone part of this build — see `hardware.md` §2
> and Gate E in §6. Budget the separate supply, the common ground, and the caps
> *now*; retrofitting after chasing phantom reboots is miserable.

---

## 1. Flash the OS — Raspberry Pi OS Lite (64-bit), headless

You run yalp headless: no monitor, no keyboard. Everything is over SSH from your
laptop, exactly as `software-spec.md` §1 specifies (Pi OS Lite, headless, SSH
enabled at flash time).

> **Note —** the CanaKit microSD may ship preloaded with the full desktop OS.
> **Re-flash it to Lite anyway** — you want the 64-bit *Lite* image and the
> headless settings baked in.

**DO:**

1. Install **Raspberry Pi Imager** on your laptop (`raspberrypi.com/software`).
2. Insert the microSD (via the laptop slot or a USB card reader).
3. In Imager: **Choose Device** → Raspberry Pi 5. **Choose OS** → *Raspberry Pi OS
   (other)* → **Raspberry Pi OS Lite (64-bit)**. **Choose Storage** → the microSD.
4. Click the **gear / "Edit Settings"** (advanced options) and set, before
   writing:
   - **Hostname:** `izzy` (so you reach it at `izzy.local`)
   - **Enable SSH** → *Use password authentication* (or paste a public key)
   - **Username & password:** pick a username (this runbook uses `pi`) and a real
     password — write it down
   - **Wireless LAN:** your **SSID + password**, and set the correct **Wi-Fi
     country** (required, or Wi-Fi stays disabled)
   - **Locale:** your time zone and keyboard layout
5. **Write**, wait for verify, eject.
6. Put the card in the Pi, connect the **27W PD wall supply**, power on. Give it
   ~60–90 s on first boot to expand the filesystem and join Wi-Fi.
7. From your laptop terminal:

   ```bash
   ssh pi@izzy.local
   ```

   (Use the username you set. If `izzy.local` doesn't resolve, see the "can't SSH"
   entry in §11.)

**DONE WHEN:** you get a shell prompt on the Pi over SSH — `pi@izzy:~ $` — with no
monitor attached.

---

## 2. Pi software setup — Python, the GPIO stack, and the code

Now bring the Pi's software up to where it can run the reactive layer. All of this
runs **on the Pi over SSH**.

**DO — update and install the base tools:**

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3 python3-venv python3-pip git python3-lgpio
python3 --version          # confirm 3.11 or newer
```

**DO — get yalp onto the Pi and install it** (mirrors `SETUP.md` §2–§4, on the Pi
this time):

```bash
git clone <your-yalp-repo-url> yalp     # or copy the folder over with scp/rsync
cd yalp
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**DO — install the GPIO stack and prove the pin factory.** On the Pi 5, `gpiozero`
must talk to the GPIO through **`lgpio`** (the native Pi 5 backend):

```bash
pip install gpiozero lgpio
# Prove gpiozero picks the lgpio/native factory and RPi.GPIO is NOT in the path:
python3 -c "from gpiozero import Device; Device.ensure_pin_factory(); print(type(Device.pin_factory).__module__)"
python3 -c "import sys; assert 'RPi.GPIO' not in sys.modules, 'RPi.GPIO leaked in!'; print('no RPi.GPIO — good')"
```

The first command should print an `lgpio`/native factory module (e.g.
`gpiozero.pins.lgpio`), **not** `rpigpio`.

> **RISK —** On the Pi 5 the GPIO moved behind the **RP1 southbridge**, so the
> classic **`RPi.GPIO` library does not work** — and most HC-SR04/DRV8833 tutorials
> online are written against it, where they will **silently fail** (no error, just
> dead pins). Use **`gpiozero` on the `lgpio`/native pin factory** and never
> copy-paste `RPi.GPIO` snippets. This is settled in `hardware.md` §5 and
> `software-spec.md` §1; §3 below proves the stack on an LED before any motor.

**The two-process deployment** (from `software-spec.md` §1, §2): yalp is always
**two separate OS processes** talking over a localhost socket (newline-delimited
JSON). They map to the config endpoint `IPC_HOST` / `IPC_PORT` (`127.0.0.1:8765`
in `config.py`):

- the **reactive layer** (motors, ultrasonic, camera, collision-stop) **must run
  on the Pi** — it owns the GPIO and the camera and runs at 10–30 Hz whether or not
  the cloud is reachable;
- the **deliberative layer** (the agent loop + cloud model calls) runs **on your
  laptop in dev** and connects to the Pi over the socket — or you can run **both on
  the Pi** to start, since the socket is just `127.0.0.1` either way.

For laptop-drives-Pi later you point the laptop's deliberative process at the Pi's
address instead of `127.0.0.1`; nothing else changes. See `software-spec.md` §2.

**DONE WHEN:** `pip install -e ".[dev]"` succeeds on the Pi, `pytest` passes, and
the two probe commands above print an lgpio/native factory and "no RPi.GPIO".

---

## 3. GPIO first light — blink an LED before any motor (milestone G)

The first cheap hardware win, and you do it **before wiring any motor**: prove the
`gpiozero`+`lgpio` stack actually toggles a physical pin. Debug the toolchain on a
20-cent LED, not on a spinning motor.

**DO — wire one LED (power can stay on; this is 3.3V logic only):**

```
   Pi GPIO17 ──[ 330Ω ]──►|── GND
                          LED
              (long leg = +, to the resistor/GPIO side)
```

(If you don't have an LED, use a multimeter on the pin instead and watch it toggle
between ~0V and ~3.3V.)

**DO — blink it:**

```bash
python3 - <<'PY'
from gpiozero import LED
from time import sleep
led = LED(17)          # uses the lgpio/native factory automatically on Pi 5
for _ in range(10):
    led.on();  sleep(0.5)
    led.off(); sleep(0.5)
print("blink done")
PY
```

**DONE WHEN:** the LED blinks ten times (or the meter swings 0 ↔ 3.3V in step), and
the §2 probe already confirmed the **lgpio** factory with **no `RPi.GPIO`** in the
import path. That is milestone **G** green: the GPIO toolchain is proven on the
real Pi 5.

---

## 4. HC-SR04 echo divider — build it and METER it before it touches a pin (milestone I)

The HC-SR04 ECHO pin swings to **5V**, but the Pi's GPIO is **3.3V-only**. A
resistor divider drops it. You build the divider on the breadboard, apply 5V, and
**meter the tap** — and only when it reads ~3.3V do you connect it to GPIO6. This
is its own tiny, checkable milestone (`roadmap.md` **I**), deliberately separate
from any sensor code.

**DO — build the divider on the breadboard:**

```
   HC-SR04 ECHO (5V swing)
        │
      [ R1 = 1 kΩ ]
        │
        ├──────────────►  TAP  → (later) Pi GPIO6
        │
      [ R2 = 2 kΩ ]
        │
       GND  (shared with Pi GND and motor GND)
```

The tap sits between R1 and R2. With 5V in, the tap = 5V × R2/(R1+R2) =
5 × 2/(1+2) = **~3.33V** — safe for the Pi.

**DO — meter it BEFORE wiring to any GPIO:**

1. With the **GPIO side of the tap left disconnected**, power the HC-SR04 VCC from
   a **5V** source and tie its GND to your common ground.
2. To simulate the echo high level, temporarily jumper the **top of R1 to 5V**
   (i.e. drive the divider input high by hand).
3. Put the multimeter across **TAP → GND**.

**DONE WHEN:** the meter reads **≤ 3.3V (≈3.3V)** at the tap. *Only then* connect
the tap to **GPIO6**. If it reads ~5V or near 0V, you have R1/R2 swapped or a
broken leg — fix it before connecting anything.

> **RISK —** Wiring the 5V ECHO straight to a Pi GPIO, or connecting a mis-built
> divider you haven't metered, can **fry the Pi input**. The 1k/2k divider (any
> ~2:1 ratio dropping 5V→~3.3V) is mandatory, and the meter check is a two-minute
> insurance policy against a dead pin. Settled in `hardware.md` §4–§5.

---

## 5. Wire the drivetrain + sensor — per the `hardware.md` pin map

Now wire the full body. **Power OFF for all of this** (Pi unplugged, NiMH pack
disconnected). Use the canonical pin table in `hardware.md` §5 as the source of
truth — it is reproduced here for the bench, but if this and `hardware.md` ever
disagree, **`hardware.md` wins**.

| Signal | Pi GPIO (BCM) | Goes to | Notes |
|---|---|---|---|
| Motor A PWM (speed/enable) | **GPIO 12** | driver AIN1 | **hardware PWM0** — left speed |
| Motor A DIR (phase) | GPIO 17 | driver AIN2 | plain GPIO — left direction |
| Motor B PWM (speed/enable) | **GPIO 13** | driver BIN1 | **hardware PWM1** — right speed |
| Motor B DIR (phase) | GPIO 22 | driver BIN2 | plain GPIO — right direction |
| Driver STBY/EN | GPIO 24 | driver STBY | **TB6612FNG only.** The DRV8833 in use has no STBY pin — GPIO24 (`MOTOR_STBY_PIN`) is inert / **left unwired**, driven only when `MOTOR_DRIVER_KIND=="tb6612fng"`. On the DRV8833, tie **nSLEEP HIGH to Pi 3V3**. |
| Ultrasonic TRIG | GPIO 5 | HC-SR04 TRIG | 3.3V out — fine as-is |
| Ultrasonic ECHO | GPIO 6 | divider **tap** (§4) | 5V echo → ~3.3V via the divider |
| Common ground | any GND pin | driver GND **and** NiMH GND | the non-negotiable shared ground |

**Why phase/enable and not four PWM pins:** the Pi 5 exposes only **2
hardware-PWM lines** (GPIO12/13 are PWM0/PWM1). So each driver channel gets PWM on
**one** input (speed) on a hardware-PWM pin and a **plain GPIO** on the other input
(direction). Four software-PWM inputs would jitter under CPU load and worsen the
open-loop drift — don't. This is settled in `hardware.md` §5.

> **DECISION —** Motors run **phase/enable** (PWM one input per channel on
> hardware-PWM GPIO12/13, the other input plain GPIO for direction), **not** four
> software-PWM inputs. Forced by the Pi 5's two hardware-PWM lines, and the better
> design anyway. (`hardware.md` §5.)

**DO — wiring overview (the freed-up shape; mirror `hardware.md` §5):**

```
   Raspberry Pi 5 (3.3V GPIO)         DRV8833 (board in use)     Motors
   ┌──────────────────────┐        ┌────────────────────┐
   │ GPIO12 (HW PWM) ──────────────► AIN1 (speed)        │      ┌────────┐
   │ GPIO17 (plain)  ──────────────► AIN2 (dir)   AOUT1/2├─────►│ Motor A│ left
   │ GPIO13 (HW PWM) ──────────────► BIN1 (speed)        │      └────────┘
   │ GPIO22 (plain)  ──────────────► BIN2 (dir)   BOUT1/2├─────►┌────────┐
   │ GPIO24 (plain)  ──────────────► STBY (TB6612 only)  │      │ Motor B│ right
   │ GND ───────────┬──────────────► GND                 │      └────────┘
   └────────────────┼──────────────┴── VM ◄── NiMH 4×AA (SEPARATE supply)
                    │                       │
                    │ COMMON GROUND         ├──[ 470–1000 µF ]── GND  (bulk, polarity!)
                    │                       └──[ 0.1 µF ]─────── GND  (ceramic, HF)
                    │                          ^ caps across VM↔GND, at the driver
   HC-SR04          │
   ┌──────────┐     │
   │ VCC ◄─ 5V │    │
   │ GND ──────┼────┘
   │ TRIG ◄──── GPIO5            (3.3V drive — OK)
   │ ECHO ──[ 1kΩ ]──┬──[ 2kΩ ]── GND        (divider from §4)
   │                 └──► tap ──► GPIO6       (safe ~3.3V)
   └──────────┘
```

Key points while wiring:

- **NiMH pack → driver VM only.** Never to the Pi. The Pi runs off its own 27W PD
  supply.
- **Common ground:** NiMH GND ↔ driver GND ↔ a Pi GND pin, all tied. (Settled,
  `hardware.md` §2.)
- **Caps across the motor rail (VM↔GND) at the driver:** the 470–1000 µF
  electrolytic (watch polarity) absorbs stall spikes; the 0.1 µF ceramic handles
  high-frequency noise. Start with these; size up only if Gate E (§6) shows the
  rail still droops.
- **Keep motor leads twisted and short** to cut noise.
- The DRV8833/TB6612FNG have **internal flyback diodes** — good. Never substitute a
  bare H-bridge without them.

**DONE WHEN:** every row of the pin table is wired, the common ground is tied, the
caps are across VM↔GND with correct electrolytic polarity, and you've
double-checked there is **no wire from the NiMH pack to any Pi pin**. Do **not**
power on for a drive test yet — that's Gate E, next.

---

## 6. 🚦 Gate E — power / brownout bring-up (GO / NO-GO)

This is the milestone that stands between "wired" and "trusting motors under the
Pi." It is **quantitative, not a vibe check** (`hardware.md` §2, `roadmap.md`
milestone **F**). Run the power bring-up checklist in order; do not skip ahead.

**DO — the staged bring-up (`hardware.md` §2 checklist):**

1. **Pi alone** on the PD supply. Boot, SSH in, load the CPU and confirm it stays
   stable:

   ```bash
   vcgencmd get_throttled        # want 0x0
   ```

2. **Motor supply alone.** Power the driver from the NiMH pack with the Pi powered
   separately and the **logic GPIOs disconnected**. Confirm the driver spins both
   motors by briefly jumpering its inputs by hand. (This proves motors + driver +
   pack work on their own.)

3. **Join common ground**, reconnect the logic GPIOs, and run a deliberately
   **hard, stall-heavy drive script** — rapid direction reversals with both motors
   stalled against your hand — while you **meter the motor rail** and re-check
   `get_throttled`.

   **⚙️ RETINUE:** Woland provides the stall-heavy drive script (`gpiozero`
   phase/enable on GPIO12/13 + GPIO17/22, rapid reversals at high duty). You run
   it on the Pi, hold the wheels, and watch the meter + `get_throttled`.

**Watch `get_throttled` live during the stall test** (run in a second SSH window):

```bash
watch -n 1 vcgencmd get_throttled
```

**PASS criteria — ALL THREE must hold under the hard, stall-heavy drive:**

- [ ] **No Pi resets** (it never reboots — the SSH session survives the whole test)
- [ ] **`vcgencmd get_throttled` stays `0x0`** throughout
- [ ] **Motor-rail voltage stays above the driver's logic VIH** (meter VM↔GND under
      stall; it must not sag below the driver's minimum logic-high input level)

**GO →** proceed to §7. The power story is trusted.

**NO-GO recovery path (part of this milestone, not a fork — `hardware.md` §2):**

1. Add **470–1000 µF bulk + 0.1 µF ceramic across VM** (size the bulk cap from the
   *measured* sag — start at 470 µF, go up only if the rail still droops; don't
   guess).
2. **Twist and shorten** the motor leads.
3. Confirm you're on **NiMH** cells, not alkaline (alkalines sag under stall — see
   the `hardware.md` §1 decision).
4. **Re-test** the stall script from step 3 until all three criteria pass clean.

> **DECISION —** Gate E PASSES only when, under a hard stall-heavy drive script,
> *all three* hold: no Pi resets, `get_throttled` stays `0x0`, and motor-rail
> voltage stays above the driver's logic VIH. **No autonomous driving until it
> passes clean.** (`hardware.md` §2, `roadmap.md` §2.3.)

> **RISK —** Motor stall spikes browning out the Pi over a shared/noisy rail is the
> #1 "ghost reboot" bug in hobby robots. If the Pi reboots when the motors move,
> that is brownout — go to the NO-GO recovery above, do not chase it as a software
> bug. (See §11.)

---

## 7. Hello motors — drive forward / turn / stop (milestone H)

With Gate E (§6) and GPIO first light (§3) both green, drive the wheels from
Python through the driver: forward, turn, stop.

**⚙️ RETINUE — the on-Pi reactive backend is already implemented; this step
*confirms it on hardware*.** `src/yalp/reactive/real_backend.py` is **fully built**:
`RealReactiveBackend` satisfies the **same `ReactiveBackend` contract** the fake
laptop backend does (`apply_intent`, `tick`, `get_state`), with motor control via
`gpiozero` phase/enable (PWM on GPIO12/13, direction on GPIO17/22, clamped to
`RobotState.speed_limit`), the HC-SR04 read, collision-stop, and the `MotorWatchdog`
wired into `run()`, exactly per `hardware.md` §5. No backend code remains to write —
**you wire and run; the job here is verifying the real body behaves.**

**DO:** with the body powered (Pi on PD, motors on NiMH, common ground), run the
drive primitives Woland hands you — a forward nudge, a left turn (wheels opposite),
a right turn, and a stop. Start the bot **up on a stand/book so the wheels spin
free** for the first run, then put it on the floor.

> **Reality check (`hardware.md` §3):** drive is **open-loop** — no encoders. "Turn
> 90°" is a timed guess calibrated by eye, "drive straight" drifts because no two TT
> motors are identical, and the drift changes with battery charge and floor surface.
> This is expected and fine for v1. Keep moves coarse; precise dead-reckoning will
> not work.

**DONE WHEN:** the wheels obey **forward**, **turn**, and **stop** on command — the
robot moves the way you tell it. If a motor spins the wrong way, swap that
channel's two motor-output leads (or flip its DIR pin polarity in the backend —
tell Woland). That is milestone **H** green.

---

## 8. Safety reflex — HC-SR04 collision-stop that overrides drive (milestone J)

Before *any* autonomous driving, prove the reflex: a fast local check that
**overrides any drive command** when something's too close. This is the one
behavior that must work even when everything else is broken (`architecture.md` §4).

**⚙️ RETINUE:** Woland wires the HC-SR04 read into the reactive tick's **safety
override** (top priority, every tick — `software-spec.md` §2.3): read distance,
and if `distance_m < SAFE_STOP_THRESHOLD_M` (0.30 m in `config.py`) **the wheels
stop this tick** before any drive command is applied, the mode latches to
`SAFE_STOP`, and `goal_status` is published as `BLOCKED` upward. Critically:

- **Echo timeout = "unknown" → STOP, never "clear".** A missed/timed-out echo sets
  `distance_known = False` and biases to STOP — a missed ping is *never* decayed
  into "the path is clear" (`hardware.md` §4, `software-spec.md` §2.3).
- **Respect the ~60 ms sensor cycle:** poll no faster than ~15 Hz, or overlapping
  pings corrupt readings (`hardware.md` §4).
- **No open-loop reverse.** On collision-stop the robot HALTs and surfaces
  `BLOCKED`; it does **not** auto-back-up — there's no rear sensor, so reversing
  blind is unsafe. Recovery is the deliberative layer's job (`software-spec.md`
  §2.3, `architecture.md` §3).

**DO:** drive the robot slowly toward a wall/box (on the floor or on a stand with
an obstacle moved into the beam) and watch it stop.

**DONE WHEN:** commanded forward drive **halts within the threshold distance**
before hitting the obstacle, the robot reports **BLOCKED**, and it does **not**
reverse on its own. That is milestone **J** green — the reflex is in *before* any
autonomous driving.

---

## 9. 🚦 Gate H — person-detector fps spike (GO / NO-GO)

This gate decides the *shape* of follow-mode. It is the single biggest unknown in
the reactive layer: the Pi 5 has **no NPU**, so whether a real person *detector*
runs fast enough to drive track-by-detection is unknown until measured on this Pi
(`roadmap.md` milestone **L**, `software-spec.md` §4).

**⚙️ RETINUE:** Woland provides the detector spike harness (try **ONNX Runtime or
ncnn with int8**, not just OpenCV DNN; input downscaled to **~320×240 before
inference**). **Crucial:** measure **SUSTAINED** fps **concurrent with the reactive
loop + camera capture + a motor-PWM stress load — never in isolation**. Record the
triple **(model, resolution, runtime)** with the number.

**DO:** run the spike harness on the Pi under that combined load and read off the
sustained detector fps.

**Decision threshold** (`config.GATE_H_GO_HZ` = 3):

- **≥ 3 Hz sustained = GO** → **track-by-detection**: cheap tracker every tick,
  re-seeded by the slow detector (the `software-spec.md` §4 design).
- **≤ 1–2 Hz = NO-GO** → ship the **blob/color tracker** as a first-class
  deliverable (follow a colored target, collision-stop underneath) and defer robust
  person-follow. A NO-GO is *a different build, not a demotion*.

**Also run the combined-load gate (milestone K, `software-spec.md` §4):** with the
tracker + detector child + capture + motor writes **all live at once**, the
**reactive-tick p99 latency must stay < 33 ms** (`config.TICK_BUDGET_MS`), i.e. the
safety loop holds ≥ 30 Hz at the 99th percentile under full load. Record the
config. NO-GO recovery: drop detector cadence/resolution, move detection off the
tick onto a slower thread that only re-seeds the tracker, re-measure.

**DONE WHEN:** you've recorded the sustained detector fps (with its model/res/
runtime triple) under concurrent load **and** the tick-p99-under-full-load number,
and you know which follow-mode branch (GO/NO-GO) you're building.

> **DECISION —** Follow-mode's tracker choice is decided by this measured number,
> not by preference: ≥ 3 Hz → track-by-detection; ≤ 1–2 Hz → blob/color tracker.
> Both branches are first-class deliverables. (`roadmap.md` §2.2, `software-spec.md`
> §4.)

---

## 10. Bring it together — real reactive layer on the Pi + agent loop from the laptop

You now have a body that drives, stops for obstacles, and a measured follow-mode
branch. Marry it to the brain you built on the laptop in `SETUP.md`.

**DO:**

1. On the **Pi**, run the real reactive process (the implemented `real_backend.py`
   serving the socket at `127.0.0.1:8765` per `config.py`). **⚙️ RETINUE** provides
   the launch command/service.
2. On your **laptop**, run the deliberative agent loop pointed at the Pi's address
   (instead of `127.0.0.1`). The agent's `ANTHROPIC_API_KEY` stays in the laptop
   environment / `.env` (`software-spec.md` §6) — never on the Pi in source.
3. Exercise it:

   ```bash
   yalp see                # capture a still on the real C270 → Claude → words
   yalp agent --command "drive forward a bit and tell me what you see"
   ```

   These commands now act on the **real robot**: `yalp see` captures from the C270,
   and `yalp agent` issues intents over the socket that the on-Pi reactive layer
   executes — with collision-stop overriding everything (§8).

**DONE WHEN:** `yalp see` returns a description of the real scene and `yalp agent`
drives the real wheels through the socket while the reflex keeps it safe.

**➡️ NEXT STEPS after this runbook (the path, in order — canonical ledger in
`roadmap.md`):** run **follow-mode on the hardware** (milestone **M**, the GO/NO-GO
branch decided by §9 — the laptop-proven follow loop now confirmed on the Pi, or
its blob/color `Detector` fallback) → the **WiFi-degradation test** (milestone
**N**) → and, on a deliberately separate later track, **voice INPUT** (milestone
**O** — STT, bolted on only after the full text loop works end to end; voice OUTPUT
already ships). Everything past **N** — a stronger on-device tracker, depth/SLAM
navigation, onboard inference, the kid-version enclosure — is **post-v1 stretch**,
recorded in `roadmap.md` §6 so it doesn't get re-litigated onto the critical path.

---

## 11. Session checklist & "when something's wrong"

### Session checklist (the gates and milestones, in bench order)

- [ ] **§1** OS flashed (Pi OS Lite 64-bit, headless), SSH works → `pi@izzy.local`
- [ ] **§2** Python + `gpiozero`/`lgpio` installed; lgpio factory confirmed, no
      `RPi.GPIO`; `pip install -e ".[dev]"` + `pytest` pass on the Pi
- [ ] **§3** 🟢 **G — GPIO first light:** LED blinks via gpiozero+lgpio
- [ ] **§4** 🟢 **I — echo divider:** tap meters ≤ 3.3V *before* touching GPIO6
- [ ] **§5** Drivetrain + sensor wired per `hardware.md` pin map; common ground +
      caps in place; no NiMH-to-Pi wire
- [ ] **§6** 🚦 **Gate E (F):** no resets + `get_throttled` 0x0 + rail above VIH
      under stall — all three
- [ ] **§7** 🟢 **H — Hello motors:** wheels obey forward / turn / stop
- [ ] **§8** 🟢 **J — Safety reflex:** stops before the obstacle, reports BLOCKED,
      no blind reverse
- [ ] **§9** 🚦 **Gate H (L)** + combined-load **(K)**: sustained detector fps
      recorded under load; tick p99 < 33 ms under full load; follow-mode branch
      chosen
- [ ] **§10** Real reactive layer on Pi + agent loop from laptop; `yalp see` /
      `yalp agent` act on the real robot

### When something's wrong — quick reference

| Symptom | Likely cause → what to do |
|---|---|
| **Pi won't boot** (no green/activity LED, never appears) | Re-seat the microSD; re-flash Lite (§1); confirm the **27W PD** supply (a weak phone charger browns the Pi at boot); try a different USB-C cable. |
| **Can't SSH / `izzy.local` won't resolve** | Wait the full ~90 s on first boot. Confirm SSH was enabled and the **Wi-Fi SSID + country** were set in the Imager (§1). Try the Pi's IP from your router instead of `.local`. Confirm laptop and Pi are on the **same network**. |
| **`gpiozero` does nothing / pins dead, no error** | `RPi.GPIO` in the path — it **silently fails on Pi 5**. Re-run the §2 probes; ensure the **lgpio/native** factory is active and `RPi.GPIO` isn't imported. (`hardware.md` §5.) |
| **Motors don't move** | Check **common ground** (NiMH GND ↔ Pi GND) first — the #1 omission. Confirm NiMH pack is charged and on VM; on TB6612FNG, **STBY must be high** (GPIO24); verify GPIO12/13/17/22 wiring against the §5 table. Motor spins backwards → swap that channel's two output leads. |
| **Pi reboots / resets when motors move** | **Brownout** — motor stall spikes sagging the rail. This is **not** a software bug. Go to the **Gate E NO-GO recovery** (§6): add 470–1000 µF bulk + 0.1 µF ceramic across VM, twist/shorten leads, confirm NiMH (not alkaline), re-test. |
| **Sensor reads garbage / random distances** | Confirm the **divider tap meters ~3.3V** (§4) and ECHO is on GPIO6 *via the divider*, not direct. Check the HC-SR04 GND shares common ground. Don't poll faster than ~15 Hz (≥60 ms). A timed-out echo is **"unknown" → STOP**, not "clear" (§8). |
| **Robot doesn't stop for obstacles** | Verify the reflex is in the tick (§8) and `SAFE_STOP_THRESHOLD_M` = 0.30 m; meter the divider; confirm the sensor faces forward and the obstacle is in its narrow ~15–30° cone (`hardware.md` §4 — side/low/glass objects are blind spots). |

> **OPEN —** Coverage of a single front HC-SR04 is thin (narrow cone; no side/rear;
> blind to glass and drop-offs — `hardware.md` §4). v1 brings up one front sensor to
> prove the reflex; whether to add corner/rear sensors is owned by `hardware.md`,
> not decided here. Keep speeds coarse and don't trust it near stairs or a child.
