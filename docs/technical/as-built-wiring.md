# As-Built Wiring — what is actually on the robot right now

> The ground-truth record of the *physical* wiring as it exists on **Izzy** (the real Raspberry Pi 5, hostname `izzy`) — wire colors, breadboard nodes, and Pi **physical pin numbers** — so a photo of the bench and this page agree. `hardware.md` is the *planned* spec (BCM pin map, power architecture, the divider theory); this doc is the *as-built* reality and records every place the build deviated from that spec and why.

---

This page is updated as each subsystem is wired. It is the companion to
`hardware.md` (the design) and `hardware-runbook.md` (the bench procedure): when
those two and the real hardware disagree, **this page describes what is actually
connected today.** Pin numbers here are **physical header pins** (the 1–40 board
positions) with the BCM/GPIO name alongside, because that is what you count when
you plug a jumper in.

> ## 🟢 BUILD STATUS — You are here
>
> - **DONE & recorded:** HC-SR04 ultrasonic (1 kΩ / 1.5 kΩ divider on ECHO — §1),
>   jumpers to the Pi, USB webcam (Logitech C270).
> - **DONE & recorded:** DRV8833 drivetrain (§3) — headers soldered, wired,
>   bench-tested, **Gate E (power/brownout) passed 2026-07-03**, and **milestone H
>   (Hello Motors) PASSED 2026-07-15** on the replacement Pi 5 — both wheels forward,
>   in-place LEFT/RIGHT turns confirmed.
> - **DONE & recorded (§3.4):** motor direction calibration — both wheels wired
>   reversed (mirror-mount, symmetric); corrected in software (`left_invert=True`,
>   `right_invert=True`), persisted to `~/.config/yalp/calibration.json` on the Pi.
> - **DONE (2026-09-15/16, §3.6):** left motor leads found swapped → `left_invert=false`;
>   DRV8833 decay-mode asymmetry fixed in software (uniform slow decay); **visual heading
>   hold + closed-loop turns + trim learning** verified on the floor. Body is now the
>   two-deck acrylic chassis (§0).
> - **NEXT PHYSICAL STEP:** third deck + screwed camera mount, then the I2C sensors
>   (MPU-6050 IMU, INA219 pack monitor) and the FIT0450 encoder motors — the code for
>   all three is in place and auto-detects them. See `shopping-list-2026-09.md`.
>
> The board is the **DRV8833** (`MOTOR_DRIVER_KIND="drv8833"`, the `config.py`
> default). It has **no STBY pin**; TB6612FNG is a fallback only if the DRV8833
> runs hot near its current limit.

**Wired and verified so far:**

| Subsystem | Status | Verify command (on the Pi) |
|---|---|---|
| Power / brain (Pi 5, wall PD) | ✅ booting, headless over SSH | — |
| Camera (Logitech C270, USB) | ✅ working | `yalp hwtest --check camera` (grabs a 640×480 frame) |
| Vision pipeline | ✅ working | `yalp see` (frame → Claude vision → scene description) |
| **Ultrasonic HC-SR04** | ✅ **wired & working** | `yalp hwtest --check ultrasonic` (real distances) |
| **Motors (DRV8833 + 2× TT + 4×AA)** | ✅ **wired & Gate E PASSED (2026-07-03); milestone H PASSED (2026-07-15)** | `yalp hwtest --check motors`; `vcgencmd get_throttled` (want 0x0) |

---

## 0. Body — as built (2026-09-16)

Izzy is on the **two-deck 2WD acrylic chassis** from `chassis-v2.md` §12 (Stage 1 of
that plan is done as far as the deck goes; the caster/encoder-motor/electrical-hardening
stages are still open). The v1 cardboard body is retired.

| Deck | Carries | Notes |
|---|---|---|
| **1 (bottom)** | 4×AA NiMH pack with its switch; **HC-SR04** ultrasonic at the front | Sonar low and level (see `chassis-v2.md` §7 for the beam-vs-floor geometry). |
| **2 (top)** | **Pi 5**; the **drivetrain breadboard** (DRV8833, §3) and the **sonar-divider mini breadboard** (§1); **C270 camera** | **Out of room.** The camera is clipped, not screwed. |

> **TODO (fill in when the third plate is ordered — see `shopping-list-2026-09.md` §1):**
> plate outer dimensions, standoff hole spacing, standoff height, and the camera lens
> height above the floor once it moves to deck 3. Record the transducer-face-to-bumper
> offset for the sonar too (served distance vs actual clearance).

**Planned (code in place, hardware not yet fitted):** MPU-6050 IMU and INA219 pack
monitor on I2C-1 (SDA GPIO2 / pin 3, SCL GPIO3 / pin 5, 3V3, GND); FIT0450 encoder
motors with A/B on GPIO16/26 (left, pins 36/37) and GPIO20/21 (right, pins 38/40),
encoder VCC from 3V3. When any of these is wired, add its jumper table here in the §1/§3
style.

---

## 1. Ultrasonic HC-SR04 — as built

The HC-SR04 is wired through a small **separate breadboard** that carries the
ECHO voltage divider and acts as the 5 V / GND distribution rail for the sensor.
Pin assignments are unchanged from `hardware.md` §5 — **TRIG = GPIO5, ECHO =
GPIO6** — only the physical divider values changed (see the as-built note below).

### 1.1 As-built divider — 1 kΩ + 1.5 kΩ (3.0 V tap), not 1 kΩ + 2 kΩ

> **NOTE (as-built) —** `hardware.md` specs a **1 kΩ + 2 kΩ** divider (≈ 3.33 V at
> the tap). The build kit had **no 2 kΩ resistor**, so the as-built divider is
> **1 kΩ + 1.5 kΩ, which yields exactly 3.0 V at the tap**
> (5 V × 1.5 / (1 + 1.5) = **3.0 V**). This is a **purely physical substitution** —
> the software and pin assignments are untouched. It is, if anything, slightly
> *better*: the lower total impedance gives cleaner edges, and 3.0 V sits with more
> margin under the Pi's 3.3 V GPIO limit while still being a solid logic-HIGH for
> the ECHO read. No code change is needed or was made.

### 1.2 Breadboard node legend

The divider lives on its own mini breadboard:

| Node | Role |
|---|---|
| `+` rail | **5 V bus** (fed from Pi physical Pin 2) |
| `−` rail | **GND bus** (tied to Pi physical Pin 6) |
| Column 5 | **ECHO-in node** — divider input (raw 5 V echo from the sensor) |
| Column 9 | **TAP node** — divided echo (3.0 V) → GPIO6 |
| Column 20 | **TRIG passthrough** tie point (no divider — 3.3 V drive is fine for TRIG) |

Resistors:
- **R1 = 1 kΩ** spans **column 5 ↔ column 9** (top leg of the divider).
- **R2 = 1.5 kΩ** spans **column 9 ↔ `−` rail / GND** (bottom leg).

So the tap at column 9 = 5 V × R2 / (R1 + R2) = 5 V × 1.5 / 2.5 = **3.0 V**.

### 1.3 Connection list (every jumper is male-to-female)

Each row is one physical jumper. Multi-hop nets (VCC, GND) are split into their
two segments so each segment's wire color is unambiguous.

| # | From | Wire color | To | Pi pin (physical / BCM) |
|---|---|---|---|---|
| 1 | HC-SR04 **VCC** | red | breadboard `+` rail (5 V bus) | — |
| 2 | breadboard `+` rail | red | **Pi Pin 2** (5 V) | Pin 2 / 5 V |
| 3 | HC-SR04 **GND** | black | breadboard `−` rail (GND bus) | — |
| 4 | breadboard `−` rail | black | **Pi Pin 6** (GND) | Pin 6 / GND |
| 5 | HC-SR04 **TRIG** | brown | breadboard column 20 | — |
| 6 | breadboard column 20 | green | **Pi Pin 29** (TRIG) | Pin 29 / GPIO5 |
| 7 | HC-SR04 **ECHO** | orange | breadboard column 5 (divider input) | — |
| 8 | divider **TAP** (column 9) | yellow | **Pi Pin 31** (ECHO, divided to 3.0 V) | Pin 31 / GPIO6 |

Pi header pins used (physical → name): **Pin 2 = 5 V**, **Pin 6 = GND**,
**Pin 29 = GPIO5 (TRIG)**, **Pin 31 = GPIO6 (ECHO)**.

### 1.4 As-built ASCII sketch

```
   HC-SR04                         mini breadboard                     Raspberry Pi 5
   ┌──────────┐                                                        (physical pins)
   │ VCC ─── red ──────────────►  + rail (5V bus) ── red ───────────►  Pin 2  (5V)
   │ GND ─── black ────────────►  − rail (GND bus) ─ black ─────────►  Pin 6  (GND)
   │ TRIG ── brown ────────────►  col 20 ────────── green ─────────►  Pin 29 (GPIO5 TRIG)
   │ ECHO ── orange ───────────►  col 5
   │          │                    │
   │          │            R1 1kΩ  │ col 5 ─┬─ col 9   (TAP = 3.0V)
   │          │                    │        │
   │          │            R2 1.5kΩ│ col 9 ─┴─ − rail (GND)
   │          │                    │
   │          └───── divided tap: col 9 ── yellow ──────────────────►  Pin 31 (GPIO6 ECHO)
   └──────────┘
                tap = 5V × 1.5/(1+1.5) = 3.0V  (≤ 3.3V GPIO limit ✓)
```

---

## 2. Troubleshooting notes (from the real bring-up)

> **NOTE (loose breadboard connection) —** Initial ultrasonic reads were very
> flaky — mostly echo **timeouts**, with the success rate visibly *degrading* over
> a few minutes. Root cause was **not** software: it was a **loose breadboard
> connection**. **Re-seating the jumpers and the resistor legs — especially at the
> TAP column (column 9) — fixed it**, jumping the success rate to **~22 of 25 good
> reads** on a live 25-read stream, with the distance clearly tracking a hand moving
> toward and away. If reads go flaky again, re-seat the divider before suspecting
> code.

> **NOTE (gpiozero software echo timing on Pi 5) —** gpiozero on the Pi 5 measures
> the echo pulse with **software timing** (it logs a `PWMSoftwareFallback` warning)
> because **`pigpio` is unavailable on the Pi 5**. This causes **occasional echo
> timeouts** even when the wiring is good. That is expected and *safe by design*:
> the reactive safety logic treats a missing/timed-out echo as **STOP**, never as
> "clear" (see `hardware.md` §4 and `pi-bringup.md`). So a stray timeout in the read
> stream is normal, not a wiring fault — only a *sustained* timeout pattern (as in
> the loose-connection case above) is a problem.
>
> **MITIGATION (bounded "coast last-known" grace) —** the software echo timing
> above produces *isolated* timeouts often enough — **especially at longer range
> (~2–4 m open room, where the return echo is weak)** — that, because every miss
> correctly biases to `SAFE_STOP`, the robot would **phantom-STOP every second or
> two during a `DRIVE_GOAL`** and never actually drive. `GpiozeroUltrasonicSensor`
> (`src/yalp/reactive/hardware.py`) therefore applies a **bounded grace**: when a
> read times out but a *recent VALID* reading exists, it briefly **re-serves that
> last good distance** (coasts) instead of instantly declaring blindness. It is
> bounded by **BOTH**:
>
> - `ULTRASONIC_GRACE_MS` (default **150 ms** — wall-clock window since the last
>   valid read), and
> - `ULTRASONIC_GRACE_MAX_MISSES` (default **2** — consecutive echo misses).
>
> Whichever bound trips **first** ends the grace, and the read reverts to
> `known=False` → `SAFE_STOP` exactly as before; a single valid read resets both
> bounds. **Tradeoff, stated plainly:** the grace coasts on the *last good* reading
> for at most ~150 ms / 2 misses — a **bounded, deliberate risk** taken to stop the
> phantom `SAFE_STOP`s. At ~1 m/s that is ≤ ~0.15 m of blind travel, well inside the
> `SAFE_STOP_THRESHOLD_M` = 0.30 m margin. The grace **only ever re-serves the exact
> last measured distance** — it never fabricates a larger/clear value — so a **near
> obstacle is coasted as an obstacle (still STOP)** and **sustained sensor loss still
> STOPs**. Both knobs are env-overridable (`YALP_ULTRASONIC_GRACE_MS`,
> `YALP_ULTRASONIC_GRACE_MAX_MISSES`); set the miss budget to `0` to disable the
> grace and get the pre-mitigation "any miss = instant STOP" behaviour.

---

## 3. Motors (DRV8833) — as built (wired 2026-07-02/03, Gate E passed 2026-07-03)

**Module:** DRV8833 clone breakout (chip marking verified DRV8833; clone silkscreen
calls nSLEEP **"STBY"**). Seated on the drivetrain breadboard straddling the trench.
Module pin rows 1–8: **left pins in column C**, **right pins in column G**.

**Silkscreen readout (as installed):**

| Column C (left row) | Column G (right row) |
|---|---|
| C1 NC | G1 VM |
| C2 AIN2 | G2 NC |
| C3 AIN1 | G3 GND |
| C4 STBY (= nSLEEP) | G4 AO1 |
| C5 BIN1 | G5 AO2 |
| C6 BIN2 | G6 BO2 |
| C7 NC | G7 BO1 |
| C8 GND | G8 GND (spare, unused) |

> **Note:** the output row order is **BO2 (row 6) above BO1 (row 7)** on this board —
> the reverse of the labeling order on many datasheets.

> **2026-07-06 field session:** the DRV8833 is an **IN/IN** part, but the driver code
> was treating it as **phase/enable** — a decay-mode dialect mismatch that drove an
> "idle" channel (and `stop()`) at full reverse. **The wiring was and is correct;**
> the bug was fixed in code (`GpiozeroMotorDriver._drive_channel`, DRV8833 IN/IN truth
> table). No re-wiring required.

### 3.1 Logic / signal wiring (DRV8833 ↔ Pi) — as built

Jumpers are male-to-female (female end on the Pi header). The "breadboard hole"
column is the A-row hole (opposite side of the trench from the C/G module pin).

| # | Silkscreen label | Breadboard hole | Wire color | Pi physical pin / BCM | Signal |
|---|---|---|---|---|---|
| 1 | **AIN2** | A2 | yellow | **Pin 11 / GPIO17** | left direction (`MOTOR_LEFT_DIR_PIN`) |
| 2 | **AIN1** | A3 | blue | **Pin 32 / GPIO12** | left speed / HW PWM0 (`MOTOR_LEFT_PWM_PIN`) |
| 3 | **STBY** (= nSLEEP) | A4 | white | **Pin 1 / 3V3** | enable — tie-high to 3V3; **not GPIO-controlled** |
| 4 | **BIN1** | A5 | orange | **Pin 33 / GPIO13** | right speed / HW PWM1 (`MOTOR_RIGHT_PWM_PIN`) |
| 5 | **BIN2** | A6 | green | **Pin 15 / GPIO22** | right direction (`MOTOR_RIGHT_DIR_PIN`) |
| 6 | **GND** | A8 | black | **Pin 9 / GND** | common ground, Pi side |
| — | ~~GPIO24~~ | — | — | Pin 18 / GPIO24 — **left empty** | TB6612-only STBY; inert for DRV8833 per `config.py` |

### 3.2 Motor outputs (DRV8833 ↔ TT motors) — as built

Motor lead polarity is intentionally arbitrary at this stage; forward/reverse
direction polarity will be set via `yalp calibrate` after the chassis rebuild.

| Channel | Breadboard holes | Motor |
|---|---|---|
| **Left** (A channel) | **H4** (AO1) + **H5** (AO2) | Left TT motor leads |
| **Right** (B channel) | **H6** (BO2) + **H7** (BO1) | Right TT motor leads |

> **Important for chassis mount:** the motor wired to channel A (AO1/AO2) must be
> mounted on the robot's **LEFT** side. Identify it before mounting by pulsing the left
> channel only (see the chassis WARNING in `hardware-runbook.md` §7).

### 3.3 Motor power & decoupling — as built

**Pack:** 4×AA NiMH WITH integral on/off switch (the switch is the bench master
cutoff — "batteries out" now means switch off, not physically removing cells).
Measured rail: **5.55 V** (~1.39 V/cell, freshly charged).

| Connection | Breadboard hole | Detail |
|---|---|---|
| Pack red (+) | **J1** (VM) | Motor supply positive |
| Pack black (−) | **J3** (GND, G3 net) | Motor supply negative |
| Common ground | — | Achieved through the module's internal GND net: Pi GND is at the C8 net (A8), pack − is at the G3 net (J3); the module ties all GND pins internally |

**Decoupling as built:**

| Cap | Value | Placement |
|---|---|---|
| Bulk electrolytic | **1000 µF 16 V** | + leg **I1** (VM net), stripe/− leg **I3** (GND net) |
| HF cap — **substitution** | **0.1 µF 50 V ELECTROLYTIC** (4×7 mm body) — **not** the specced ceramic | + leg **H1**, stripe/− leg **H3** |

> **HF cap substitution note:** A 0.1 µF 50 V *electrolytic* (4×7 mm) was installed
> in place of the specced 0.1 µF ceramic because the kit had no ceramic disc. It is
> polarized (unlike a ceramic), is electrically safe at this DC rail voltage, and the
> clone module carries its own onboard ceramic caps. A **"104" ceramic disc** remains
> the recommended drop-in if noise gremlins ever appear.

### 3.4 Motor direction calibration — as built (2026-07-15)

> **As-built finding:** both drive wheels were wired with **reversed polarity** relative to
> the DRV8833 channel's forward convention — i.e., what the driver considers "forward" spun
> each wheel backward. This is a **symmetric** consequence of mirror-mounting the two TT
> motors on opposite sides of the chassis: the left motor's "correct" forward lead becomes
> its natural reverse lead when flipped to face the other direction, and the same is true
> of the right. No re-soldering was needed or performed.

**Correction:** applied entirely in software using yalp's motor calibration layer:

| Parameter | Value | Effect |
|---|---|---|
| `left_invert` | `True` | Swaps the logic sense of the left channel forward/reverse |
| `right_invert` | `True` | Swaps the logic sense of the right channel forward/reverse |

Persisted to **`~/.config/yalp/calibration.json` on the Pi** (machine-local, per-robot
state under `~/.config` — **not committed to the repo**). This is exactly the use-case the
calibration layer exists for: wiring polarity that is correct for one physical build but may
differ on another.

**Verified at milestone H (2026-07-15):** both wheels roll forward together when commanded
forward; in-place LEFT turn (left wheel back, right wheel forward) and RIGHT turn (right
wheel back, left wheel forward) confirmed, all via `GpiozeroMotorDriver` through the saved
calibration.

### 3.5 Pi 5 board replacement and header re-seat (2026-07-15)

> **PMIC hardware failure:** the original Pi 5 suffered a power-management IC failure —
> reproducible overheating near the USB-C/PMIC area with any OS boot media, on multiple
> PSUs and fresh SD cards (see `hardware-runbook.md` §11 for the diagnostic pattern). The
> board was replaced under warranty.

**Re-seat procedure (2026-07-15):** the drivetrain header wiring (5 motor-driver jumpers:
AIN2 → Pin 11, AIN1 → Pin 32, STBY → Pin 1, BIN1 → Pin 33, BIN2 → Pin 15, GND → Pin 9)
was re-seated onto the **new Pi's 40-pin header using the documented pin map above (§3.1)
and the same wire colors and holes**. The breadboard side was untouched throughout the swap.
The ultrasonic jumpers (§1) were re-seated identically. The USB camera required no
re-seating (USB). After re-seat, `yalp hwtest --check motors` and the full milestone H
drive test were run to confirm correct operation on the new board.

### 3.6 Left motor leads swapped, decay-mode asymmetry, and the visual heading hold (2026-09-15)

> **As-built finding (2026-09-15):** on power-up after the summer, a forward
> command spun Izzy **in place to the LEFT** (right wheel forward, left wheel
> backward). The left TT motor's two leads at **H4/H5** had been swapped relative
> to the July build (the breadboard was handled during storage/charging). Both
> wheels still moved in both directions when pulsed alone, so this was purely a
> polarity change. **Corrected in software:** `left_invert=false` in
> `~/.config/yalp/calibration.json` on the Pi (backup of the July file:
> `calibration.json.bak-2026-09-15`). `right_invert` stays `true`. §3.4 above
> describes the July state ("both wheels inverted"); the live file is now
> **left `false`, right `true`**.

**Why she still would not drive straight after that fix — the DRV8833 decay-mode
asymmetry.** Only xIN1 (GPIO12/13) was being PWMed. In that "mixed" dialect a
channel's *forward* is fast decay (xIN2 low, PWM on xIN1) and its *reverse* is
slow decay (xIN2 high, PWM at 1-duty). With one wheel inverted and the other not,
the same "forward" command therefore drove the two wheels in **different decay
modes**, and slow decay produces much more torque at a given duty. Measured with
the camera (single-wheel pulses, |yaw rate| in deg/s, no trims):

| Mode (duty) | LEFT fwd | RIGHT fwd | LEFT rev | RIGHT rev |
|---|---|---|---|---|
| mixed (0.45) | 48 | **77** | **115** | 44 |
| mixed (0.60) | **117** | 100 | **124** | 90 |
| **slow (0.45)** | 104 | 66 | 111 | 76 |
| **slow (0.60)** | 121 | 89 | 81 | 102 |

The strong wheel in "mixed" is always the slow-decay one and the gap **flips sign
between duty 0.45 and 0.60** — no static trim can fix that. **Fix (software, no
re-wiring):** `config.MOTOR_DECAY_MODE = "slow"` (default since 2026-09-15) —
both channels, both directions use slow decay (one input held HIGH, the other
PWMed at 1-duty). This needs PWM on the DIR pins (GPIO17/22) too; the lgpio pin
factory software-times PWM on any GPIO, so **no wiring change**. The July
`right_trim 0.90` was tuned for the mixed dialect and is now wrong; the live file
has both trims at `1.0`.

**What handles the residual imbalance (~20–35% left-stronger in slow mode) and
any future surface change:** the **visual heading hold** — `ReactiveTickCore`
integrates the camera's frame-to-frame yaw (phase correlation on a 160x120 copy,
left/right halves averaged to cancel looming; `yalp/reactive/visual_odometry.py`)
and biases the left/right duty split during every straight `DRIVE_GOAL`
(`config.HEADING_HOLD_*`). Rotate goals close the loop on the same estimate
(`yalp drive --turn 90` turned +96 deg then +91 deg with a stop lead; the old
open-loop timer delivered 2–15 deg for 30–60 deg). After each straight drive the
mean correction is learned into `straight_bias_fwd` / `straight_bias_rev` in the
calibration file (`config.TRIM_LEARNING_*`), so the next drive starts straighter
without a manual recalibration. Verified 2026-09-15: heading held within ~±5 deg
over 0.3–1.0 m legs forward AND reverse (was 14 deg / 42 deg of veer before).

**Camera warm-up matters:** the C270 publishes black frames for ~1 s after
opening; the reactive server's heading estimate is blind until then
(`camera.WARMUP_MAX_SECONDS` raised to 3 s). Give the server ~4 s after its port
comes up before sending a drive.

**Optional physical tidy-up:** swapping the left leads back at H4/H5 and setting
`left_invert=true` again restores the July wiring, but it is no longer needed —
slow-decay mode makes the inversion pattern irrelevant to symmetry.

---

## Cross-references

- `hardware.md` — the *planned* design: BCM pin map (§5), power architecture (§2),
  and the divider theory (§4–§5). The 1 kΩ + 1.5 kΩ substitution and the
  mobile/untethered power plan are also recorded there.
- `hardware-runbook.md` — the step-by-step bench procedure that produced this wiring.
- `pi-bringup.md` — the software bring-up (install, GPIO-stack proof, `yalp hwtest`).
- `roadmap.md` — build sequencing and the go/no-go gates; the live status of each
  milestone (camera/vision/ultrasonic done on hardware, motors next).
