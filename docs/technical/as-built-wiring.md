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
>   bench-tested, and **Gate E (power/brownout) passed 2026-07-03**.
> - **NEXT PHYSICAL STEP:** chassis rebuild — remount motors **one per side** (not
>   one per axle — see the "train chassis" WARNING in `hardware-runbook.md` §7),
>   then run `yalp calibrate` to set direction polarity per the rebuilt body.
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
| **Motors (DRV8833 + 2× TT + 4×AA)** | ✅ **wired & Gate E PASSED (2026-07-03)** | `yalp hwtest --check motors`; `vcgencmd get_throttled` (want 0x0) |

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

---

## Cross-references

- `hardware.md` — the *planned* design: BCM pin map (§5), power architecture (§2),
  and the divider theory (§4–§5). The 1 kΩ + 1.5 kΩ substitution and the
  mobile/untethered power plan are also recorded there.
- `hardware-runbook.md` — the step-by-step bench procedure that produced this wiring.
- `pi-bringup.md` — the software bring-up (install, GPIO-stack proof, `yalp hwtest`).
- `roadmap.md` — build sequencing and the go/no-go gates; the live status of each
  milestone (camera/vision/ultrasonic done on hardware, motors next).
