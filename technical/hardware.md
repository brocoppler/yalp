# Hardware — the physical build spec

> Bill of materials, power architecture, drivetrain, sensing, and pin planning for yalp v1. The body that the brain drives.

---

## 1. Bill of Materials

This is the whole bot, part by part. v1 is a build-and-learn project — cardboard chassis, zero soldering (every module is bought with pre-attached headers or screw terminals). The Pi is mostly doing **I/O** here; the heavy AI runs in the cloud (see `architecture.md` and `software-spec.md`). So the spend goes to clean I/O and not getting electrocuted, not to compute.

| Component | Selected part | ~Cost | Rationale (one line) |
|---|---|---|---|
| **Brain** | Raspberry Pi 5 4GB via **CanaKit Starter Kit PRO** | ~$180 | RAM headroom + multiple real USB ports = fewer first-build gremlins; kit bundles 27W PD wall PSU, fan case, 128GB OS card. |
| microSD | 128GB card (in the kit) | have | OS + code; re-flash with Pi OS Lite + WiFi/SSH for headless. |
| **Eyes** | Logitech C270 USB webcam | ~$22 | UVC plug-and-play, no driver fuss; doubles as a laptop dev cam while building the brain. |
| **Motors** | 2× TT DC gear motors + wheels (4-pack) | ~$15 | Differential drive; the textbook cheap hobby drivetrain. Spares in the pack. |
| Balance | 1–2× ball caster | ~$3 ea | Passive roller, no power; the third contact point that makes two-wheel drive stable. |
| **Motor driver** | DRV8833 **or** TB6612FNG breakout | ~$5 | Cooler, cheaper, more efficient than an L298N (which drops ~2V and runs hot, wasting battery). Get the pre-soldered version. |
| Motor power | 4×AA holder (or 18650 pack) | ~$2–12 | **Separate** supply from the Pi; AA + common ground for v1 — simple, no lithium yet. |
| **Collision sensor** | HC-SR04 ultrasonic | ~$3 | Core, not optional — the collision-stop reflex. Cheapest insurance on the bot. |
| Wiring | Jumper wires (M-M, M-F, F-F) + mini breadboard | ~$10 | Solderless prototyping; F-F wires for module headers, breadboard for the level divider. |
| Chassis | Cardboard / hot glue / zip ties / foam tape | ~$19 | v1 — looks don't matter. Hot glue gun is the one tool worth buying. |
| **Voice** *(deferred)* | USB mini mic + small USB speaker | ~$15 | Voice I/O is a separate, later track — get the whole loop working text-first. |

> **DECISION —** Brain = Raspberry Pi 5 4GB via the CanaKit Starter Kit PRO. **Ordered.** Skip the AI HAT / NPU — local models on a Pi accelerator are a known hobbyist trap; CPU + cloud is the v1 move.

> **DECISION —** Eyes = Logitech C270 USB webcam. **Ordered.** UVC means zero driver work. Pi Camera Module 3 stays a possible later swap (needs a Pi-5 camera cable) only if autofocus is wanted.

> **DECISION —** Locomotion = differential drive: 2× TT gear motors + 1 ball caster. Approach locked; parts are in the Phase 2 order, not yet bought.

> **DECISION —** Motor driver = DRV8833 / TB6612FNG, **not** L298N. Cooler and more efficient; only fall back to an L298N if one is already in the parts bin.

> **THESIS —** The Pi is an I/O hub in v1, not a compute engine. Every dollar buys debugging time (real USB ports, plug-and-play camera, pre-soldered modules), not FLOPS. Spend accordingly.

### Deliberately NOT bought

These are tempting but wrong for v1, and naming them keeps the budget honest:

- **Monitor / keyboard / mouse** — headless; everything is over SSH from the laptop.
- **Separate Active Cooler / heatsink** — the CanaKit case fan already covers cooling.
- **AI HAT / NPU accelerator** — the heavy AI runs in the cloud; on-device models on a Pi accelerator are a known hobbyist trap for build #1.
- **Depth camera / LiDAR** — that's the post-v1 navigation upgrade, not a v1 part.
- **Soldering iron** — every module is bought pre-headered; ~$20 nice-to-have, not a must.

---

## 2. Power architecture — treat this as a first-class hazard

Two independent supplies, one shared ground. This is the single most failure-prone part of the build and deserves more care than anything else here.

```
   ┌─────────────────────┐         ┌──────────────────────────┐
   │  Pi supply           │         │  Motor supply             │
   │  27W USB-C PD wall    │        │  4×AA holder (~6V)        │
   │  (bench) / PD bank    │        │  (or 18650 pack)          │
   │  → Pi 5 wants 5V/5A   │        │  → DRV8833 VM (motors)    │
   └─────────┬────────────┘         └────────────┬─────────────┘
             │                                    │
          Pi 5V/GND                          driver VM/GND
             │                                    │
             └──────────── COMMON GROUND ─────────┘
                    (GND tied together — REQUIRED)
```

**Why this matters.** The Pi 5 wants a proper 5V/5A PD source. TT motors draw a modest ~150mA running but **spike to ~1A+ at stall** (and every direction-change is a brief stall). If the motors and the Pi share a single supply, those spikes sag the rail and the Pi **browns out and reboots** — the #1 cause of "why did my robot restart when it turned." Separate supplies fix the sag; a common ground is non-negotiable because the driver's logic inputs are referenced to the Pi's ground.

**Mitigations to build in from the start:**
- **Common ground.** Tie the motor-supply GND to a Pi GND pin. Without it the driver sees garbage logic levels.
- **Bulk capacitance.** A large electrolytic (~470–1000µF) across the motor supply (VM↔GND) at the driver to absorb stall spikes; a small ceramic (~0.1µF) for high-frequency decoupling.
- **Flyback handling.** Motors are inductive; the DRV8833/TB6612FNG have internal protection diodes — good. Do **not** substitute a bare H-bridge without flyback diodes, and keep motor leads twisted/short to cut noise.
- **Never back-power the Pi from the motor rail**, and never run motors off the Pi's 5V pin.

> **RISK —** Shared-ground noise + TT stall spikes browning out the Pi is the most likely "ghost reboot" bug in the whole build. Budget the caps and the separate supply now; retrofitting after chasing phantom crashes is miserable.

**Power bring-up checklist (run in order, do not skip ahead):**

1. **Pi alone** on the PD supply — boot, confirm stable, check `vcgencmd get_throttled` reads `0x0` under a CPU load.
2. **Motor supply alone** — driver powered from AA pack, Pi powered separately, **logic inputs disconnected**; confirm the driver spins both motors by jumpering inputs by hand.
3. **Join common ground** — tie the grounds, connect the 4 logic GPIOs, and drive motors from Python while watching for Pi resets and re-checking `get_throttled`. Add caps if any sag/reset appears.
4. **Only past step 3** do motors + brain run as one trusted system.

> **DECISION —** Gate motors-with-Pi behind the bring-up checklist above. No autonomous driving until step 3 passes clean (no resets, `get_throttled` stays `0x0`). This gate sits in sequence in `roadmap.md` — it stands between "Hello motors" and everything after it.

---

## 3. Drivetrain reality — open-loop, and honest about it

Differential drive with TT motors and **no encoders** means **open-loop control**: there is no feedback on how far a wheel actually turned.

- **"Turn left"** = spin the wheels in opposite directions for *N milliseconds and hope*. The angle is a guess calibrated by eye.
- **Drift.** Two TT motors are never identical — same PWM gives slightly different speeds — so "drive straight" curves, and the curve changes with battery voltage and floor surface.
- **"Back up"** is equally a guess — distance is open-loop too.
- **Vision closes the loop only in follow mode**, where the on-device tracker re-centers on the target each frame and corrects course. Outside follow mode, the robot is dead-reckoning blind.

This is *fine for v1*. The design leans into it: high-level intent comes from the slow cloud layer, but motion is "modes for the reactive layer," and the only tight feedback loop that matters (person-following) is closed by the camera, not by wheel odometry. See `architecture.md` for how the fast reactive layer wraps these open-loop primitives.

> **RISK —** Open-loop drive means no repeatability: the same command travels a different distance/angle run-to-run, worse as the battery drains. Any behavior that assumes precise dead reckoning ("go forward exactly 1m, turn exactly 90°") will not work reliably. Keep movement coarse and vision-corrected.

> **OPEN —** Encoders are cheap (~$2–4 for a pair of slotted/Hall wheel encoders) and would add closed-loop speed matching + real odometry if repeatability ever becomes necessary. Not in v1; revisit if open-loop drift becomes the limiting factor.

---

## 4. Sensing & safety coverage

v1 ships **one** front-facing HC-SR04. That is enough to prove the collision-stop reflex, but it is not enough coverage for a robot that turns, follows, and will eventually be near a child.

Honest limitations of a single front ultrasonic:
- **Narrow cone** (~15–30°) — it sees a pencil-beam straight ahead, not a wide field. A table leg just off-axis is invisible.
- **No side or rear coverage** — the robot is blind to anything it's turning into or backing toward.
- **Fails on hard cases** — glass and smooth angled surfaces deflect the ping away (no echo → "clear"); soft/fuzzy surfaces absorb it. Cliffs/stairs are invisible entirely (no downward sensing).

For a bot that *turns and follows* — and a future version near a 5–8-year-old — one forward cone is thin. Plan to grow to **2–3 sensors** (e.g. front + two angled front-corners, or front + rear) so turning and backing aren't blind.

> **RISK —** A single front HC-SR04 leaves the robot blind to the sides, the rear, glass, and drop-offs. It validates the reflex but is not adequate collision coverage for autonomous turning/following, and definitely not for the kid version.

> **OPEN —** Final sensor count and placement undecided. Leading candidate: 2–3 HC-SR04s (front + corners) for v1.x, with an IR cliff sensor considered before the bot is ever left running near stairs or a child. Decide once the chassis geometry is real.

---

## 5. GPIO / pin planning

A DRV8833 needs **4 logic inputs** (2 per motor: AIN1/AIN2, BIN1/BIN2) driven by PWM-capable GPIOs. The HC-SR04 needs a **TRIG** output and an **ECHO** input — and ECHO swings to **5V**, which would damage the Pi's **3.3V-only GPIO**, so it must pass through a resistor divider.

Pin map (BCM numbering — adjust to taste, keep PWM-capable pins for motor inputs):

| Signal | Pi GPIO (BCM) | Notes |
|---|---|---|
| Motor A IN1 | GPIO 17 | left motor direction/PWM |
| Motor A IN2 | GPIO 27 | left motor direction/PWM |
| Motor B IN1 | GPIO 22 | right motor direction/PWM |
| Motor B IN2 | GPIO 23 | right motor direction/PWM |
| Driver STBY/EN | GPIO 24 | (TB6612FNG only; tie DRV8833 nSLEEP high) |
| Ultrasonic TRIG | GPIO 5 | 3.3V out → HC-SR04 trigger (fine as-is) |
| Ultrasonic ECHO | GPIO 6 | **via divider** — 5V echo → 3.3V GPIO |
| Common ground | any GND pin | shared with motor supply (see §2) |

ASCII wiring sketch:

```
   Raspberry Pi 5 (3.3V GPIO)            DRV8833 driver            Motors
   ┌──────────────────────┐          ┌────────────────┐
   │ GPIO17 ─────────────────────────► AIN1            │        ┌────────┐
   │ GPIO27 ─────────────────────────► AIN2   AOUT1/2 ─┼───────►│ Motor A│ (left)
   │ GPIO22 ─────────────────────────► BIN1            │        └────────┘
   │ GPIO23 ─────────────────────────► BIN2   BOUT1/2 ─┼───────►┌────────┐
   │ GND    ────────────┬────────────► GND             │        │ Motor B│ (right)
   └────────────────────┼────────────┴───── VM ◄── motor supply └────────┘
                        │                    (4×AA, separate; see §2)
                        │  COMMON GROUND
                        │
   HC-SR04 ultrasonic   │
   ┌──────────────┐     │
   │ VCC ◄── 5V    │     │
   │ GND ──────────┼─────┘
   │ TRIG ◄──────── GPIO5  (3.3V drive — OK)
   │ ECHO ──┐                  voltage divider (5V → ~3.3V)
   │        │   ECHO ──[ R1 1kΩ ]──┬──[ R2 2kΩ ]── GND
   │        └────────────────────►─┘
   │                            tap ───► GPIO6  (safe 3.3V)
   └──────────────┘
```

> **RISK —** Wiring HC-SR04 ECHO straight to a Pi GPIO feeds 5V into a 3.3V pin and can fry it. The 1kΩ/2kΩ divider (or any ~2:1 ratio dropping 5V to ~3.3V) is mandatory, not optional.

---

## 6. Budget

| Phase | What it covers | Status | ~Cost |
|---|---|---|---|
| **Phase 1 — Brain** | CanaKit Pi 5 4GB Starter Kit PRO (~$180) + Logitech C270 (~$22) | ✅ Ordered | ~$200 |
| **Phase 2 — Body** | Card reader, PD power bank, TT motors, driver, ball caster, HC-SR04, AA power, wiring, glue/chassis | 🛒 To buy | ~$115 |
| **Phase 3 — Voice** | USB mic + small speaker (deferred) | 🛒 Later | ~$20 |
| | | **Total** | **~$335** |

- **Spent so far (~$200):** the entire brain — Pi kit + webcam — is ordered.
- **Remaining (~$135):** Phase 2 body + Phase 3 voice.
- **Total project (~$335):** low-hundreds, not thousands.

> **THESIS —** The brain was the expensive, lumpy buy (and the one hit by the 2026 DRAM/NAND price crunch — the Pi 5 4GB is ~$110 standalone now vs $60 at launch). Everything left is small, cheap, stable, and incremental. The financial risk is already behind us. See `roadmap.md` for how the phases sequence in time.

---

## 7. Prior art — hardware side

- **LeKiwi** (Hugging Face's LeRobot) — a wheeled mobile base that installs and runs directly on a Raspberry Pi. It's the closest existing reference to this chassis-and-Pi setup; study **how they structure the Pi-side motor/control wiring and code** before writing our own driver layer. Don't reinvent the motor-control patterns.

Software-side prior art (Reachy Mini's conversation loop and tracker) lives in `software-spec.md` and `architecture.md` — this section is hardware/wiring only.

> **DECISION —** Spend time reading LeKiwi's Pi-side motor/control code *before* wiring our own driver and writing the drive primitives. Borrow the patterns; adapt the parts.
