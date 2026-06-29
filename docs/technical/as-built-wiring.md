# As-Built Wiring — what is actually on the robot right now

> The ground-truth record of the *physical* wiring as it exists on **Izzy** (the real Raspberry Pi 5, hostname `izzy`) — wire colors, breadboard nodes, and Pi **physical pin numbers** — so a photo of the bench and this page agree. `hardware.md` is the *planned* spec (BCM pin map, power architecture, the divider theory); this doc is the *as-built* reality and records every place the build deviated from that spec and why.

---

This page is updated as each subsystem is wired. It is the companion to
`hardware.md` (the design) and `hardware-runbook.md` (the bench procedure): when
those two and the real hardware disagree, **this page describes what is actually
connected today.** Pin numbers here are **physical header pins** (the 1–40 board
positions) with the BCM/GPIO name alongside, because that is what you count when
you plug a jumper in.

**Wired and verified so far:**

| Subsystem | Status | Verify command (on the Pi) |
|---|---|---|
| Power / brain (Pi 5, wall PD) | ✅ booting, headless over SSH | — |
| Camera (Logitech C270, USB) | ✅ working | `yalp hwtest --check camera` (grabs a 640×480 frame) |
| Vision pipeline | ✅ working | `yalp see` (frame → Claude vision → scene description) |
| **Ultrasonic HC-SR04** | ✅ **wired & working** | `yalp hwtest --check ultrasonic` (real distances) |
| **Motors (DRV8833 + 2× TT + 4×AA)** | ⬜ **NOT yet wired — next step** | see the *Motors: TBD* placeholder below |

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
| 6 | breadboard column 20 | gray | **Pi Pin 29** (TRIG) | Pin 29 / GPIO5 |
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
   │ TRIG ── brown ────────────►  col 20 ────────── gray ──────────►  Pin 29 (GPIO5 TRIG)
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

## 3. Motors: TBD

> **PLACEHOLDER —** The drivetrain (**DRV8833** motor driver + **2× TT gear motors**
> + **4×AA NiMH** pack on a separate VM rail) is **not yet wired** — it is the next
> bench step. When it is built, record here, in the same per-jumper / wire-color /
> physical-pin format as §1:
> - DRV8833 logic inputs → Pi pins for **AIN1/AIN2 (left)** and **BIN1/BIN2 (right)**
>   — planned BCM map: GPIO12 (PWM, left speed), GPIO17 (left dir), GPIO13 (PWM,
>   right speed), GPIO22 (right dir); DRV8833 `nSLEEP` tied HIGH (see `hardware.md`
>   §5 and `pi-bringup.md` §2).
> - **VM rail**: 4×AA NiMH pack (+) → DRV8833 VM, pack (−) → DRV8833 GND, with the
>   bulk (470–1000 µF) + 0.1 µF ceramic across VM↔GND.
> - **Common ground**: DRV8833 GND ↔ a Pi GND pin (required — the driver's logic
>   inputs are referenced to the Pi ground).
> - Motor outputs: AOUT1/2 → left motor, BOUT1/2 → right motor.
>
> Do not wire motor power until **Gate E** (power/brownout, `roadmap.md` milestone
> **F**) and **GPIO first light** (**G**, already green) are both satisfied per the
> runbook. Fill in the wire colors and physical pins from the actual build, exactly
> as they go in.

---

## Cross-references

- `hardware.md` — the *planned* design: BCM pin map (§5), power architecture (§2),
  and the divider theory (§4–§5). The 1 kΩ + 1.5 kΩ substitution and the
  mobile/untethered power plan are also recorded there.
- `hardware-runbook.md` — the step-by-step bench procedure that produced this wiring.
- `pi-bringup.md` — the software bring-up (install, GPIO-stack proof, `yalp hwtest`).
- `roadmap.md` — build sequencing and the go/no-go gates; the live status of each
  milestone (camera/vision/ultrasonic done on hardware, motors next).
