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
> - **NEXT PHYSICAL STEP:** **solder the male header strips onto the DRV8833
>   breakout**, then wire it per **§3**.
> - **NOT yet wired:** DRV8833 motor driver, 2× TT gear motors, 4×AA NiMH pack.
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
| **Motors (DRV8833 + 2× TT + 4×AA)** | ⬜ **NOT yet wired — next step (solder DRV8833 headers first)** | see the §3 as-built table below |

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

## 3. Motors (DRV8833) — as-built fill-in table

> **NOT YET WIRED — this is the next bench step.** First **solder the male header
> strips onto the DRV8833 breakout**, then wire it per this table. **New to
> soldering?** Follow the step-by-step **"Soldering the DRV8833 header pins
> (first-timer guide)"** section in `hardware-runbook.md` — it's the prerequisite for
> everything in this §3. The board is the
> **DRV8833** (`MOTOR_DRIVER_KIND="drv8833"`, the `config.py` default) — it has **no
> STBY pin**; TB6612FNG is a fallback only if the DRV8833 runs hot. **Do not wire
> motor power until Gate E** (power/brownout, `roadmap.md` milestone **F**) **and GPIO
> first light** (**G**, already green) are both satisfied per the runbook.

The **Driver pin / Signal / To / Expected Pi pin** columns below are pre-filled from
the canonical map in `config.py` (cited by constant — the numbers live there, not
here). **Fill the _Wire color_ and _breadboard hole_ columns at the bench** as each
jumper actually goes in, exactly as in §1. Expected Pi pins are shown *physical / BCM*;
**verify each against the real header** before trusting it.

### 3.1 Logic / signal wiring (DRV8833 ↔ Pi)

| # | DRV8833 pin | Signal | To (Pi / rail) | Expected Pi pin (physical / BCM) — config constant | Wire color | Breadboard hole |
|---|---|---|---|---|---|---|
| 1 | **AIN1** | left speed / PWM (hardware PWM0) | Pi GPIO12 | Pin 32 / GPIO12 — `MOTOR_LEFT_PWM_PIN` | | |
| 2 | **AIN2** | left direction | Pi GPIO17 | Pin 11 / GPIO17 — `MOTOR_LEFT_DIR_PIN` | | |
| 3 | **BIN1** | right speed / PWM (hardware PWM1) | Pi GPIO13 | Pin 33 / GPIO13 — `MOTOR_RIGHT_PWM_PIN` | | |
| 4 | **BIN2** | right direction | Pi GPIO22 | Pin 15 / GPIO22 — `MOTOR_RIGHT_DIR_PIN` | | |
| 5 | **nSLEEP** | enable — **tie HIGH to Pi 3V3** (not GPIO-controlled) | Pi 3V3 | Pin 1 (or 17) / 3V3 | | |
| — | ~~STBY~~ | **DRV8833 has no STBY** — `MOTOR_STBY_PIN` (GPIO24) is inert / **left unwired** here; only driven when `MOTOR_DRIVER_KIND=="tb6612fng"` | — | (GPIO24 unused) | — | — |

### 3.2 Motor outputs (DRV8833 ↔ TT motors)

| # | DRV8833 pin | Signal | To | Wire color | Terminal |
|---|---|---|---|---|---|
| 6 | **AOUT1** | left motor drive | left TT motor terminal 1 | | |
| 7 | **AOUT2** | left motor drive | left TT motor terminal 2 | | |
| 8 | **BOUT1** | right motor drive | right TT motor terminal 1 | | |
| 9 | **BOUT2** | right motor drive | right TT motor terminal 2 | | |

### 3.3 Power rail & decoupling (VM / GND)

| # | DRV8833 pin | Signal | To | Wire color | Hole |
|---|---|---|---|---|---|
| 10 | **VM** | motor supply + | 4×AA NiMH pack **(+)** | | |
| 11 | **GND** | common ground | pack **(−)** *and* a Pi **GND** pin (both — required; logic inputs are referenced to Pi ground) | | |
| 12 | across **VM ↔ GND** | bulk decoupling | **470–1000 µF electrolytic** (observe polarity) | | |
| 13 | across **VM ↔ GND** | HF decoupling | **0.1 µF ceramic** | | |

> **Note —** the VM rail (4×AA) is **separate** from the Pi's USB-C supply; only the
> **GND is common** (pack − ↔ DRV8833 GND ↔ Pi GND). Never run pack + into the Pi.

---

## Cross-references

- `hardware.md` — the *planned* design: BCM pin map (§5), power architecture (§2),
  and the divider theory (§4–§5). The 1 kΩ + 1.5 kΩ substitution and the
  mobile/untethered power plan are also recorded there.
- `hardware-runbook.md` — the step-by-step bench procedure that produced this wiring.
- `pi-bringup.md` — the software bring-up (install, GPIO-stack proof, `yalp hwtest`).
- `roadmap.md` — build sequencing and the go/no-go gates; the live status of each
  milestone (camera/vision/ultrasonic done on hardware, motors next).
