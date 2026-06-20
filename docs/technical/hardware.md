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
| Motor power | 4×AA holder + **NiMH** rechargeable AAs (or 18650 pack) | ~$4–18 | **Separate** supply from the Pi; NiMH AA + common ground for v1 — low internal resistance, flatter discharge, no lithium charging yet. **Not alkaline.** |
| **Collision sensor** | HC-SR04 ultrasonic | ~$3 | Core, not optional — the collision-stop reflex. Cheapest insurance on the bot. |
| Wiring | Jumper wires (M-M, M-F, F-F) + mini breadboard | ~$10 | Solderless prototyping; F-F wires for module headers, breadboard for the level divider. |
| Chassis | Cardboard / hot glue / zip ties / foam tape | ~$19 | v1 — looks don't matter. Hot glue gun is the one tool worth buying. |
| **Voice** *(deferred)* | USB mini mic + small USB speaker | ~$15 | Voice I/O is a separate, later track — get the whole loop working text-first. |

> **DECISION —** Brain = Raspberry Pi 5 4GB via the CanaKit Starter Kit PRO. **Ordered.** Skip the AI HAT / NPU — local models on a Pi accelerator are a known hobbyist trap; CPU + cloud is the v1 move.

> **DECISION —** Eyes = Logitech C270 USB webcam. **Ordered.** UVC means zero driver work. Pi Camera Module 3 stays a possible later swap (needs a Pi-5 camera cable) only if autofocus is wanted.

> **DECISION —** Locomotion = differential drive: 2× TT gear motors + 1 ball caster. Approach locked; parts are in the Phase 2 order, not yet bought.

> **DECISION —** Motor driver = DRV8833 / TB6612FNG, **not** L298N. Cooler and more efficient; only fall back to an L298N if one is already in the parts bin.

> **DECISION —** Motor cells = **NiMH AA, not alkaline.** Alkaline AAs have high internal resistance: they sag below 5V under TT stall spikes and droop steadily as they drain, hurting both the brownout margin (see §2) *and* run-to-run repeatability (which open-loop drive already struggles with — see §3). NiMH in the same form factor has far lower internal resistance and a flatter discharge curve, so the motor rail holds up under stall and behaves the same at 80% charge as at 20%. Same 4×AA holder; just buy NiMH cells + a charger.

> **OPEN —** Driver headroom vs. the *actual* motors is unconfirmed pending a bench measurement. The DRV8833 breakout is ~1.5A RMS / 2A peak per channel; typical yellow TT motors stall ~0.7–1A at 6V, which fits — but this must be verified by **measuring TT stall current at 6V** before trusting it. If a motor measures hot or sits near the limit, switch to the **TB6612FNG** (1.2A continuous / 3.2A peak per channel). Resolve to a `> **DECISION —**` once the stall current is on paper.

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
- **Bulk capacitance.** A large electrolytic (~470–1000µF) across the motor supply (VM↔GND) at the driver to absorb stall spikes; a small ceramic (~0.1µF) for high-frequency decoupling. **Pick the value from the measured sag (see Gate E below), not a guess** — start small, add more only if the rail still droops under a stall-heavy drive.
- **Flyback handling.** Motors are inductive; the DRV8833/TB6612FNG have internal protection diodes — good. Do **not** substitute a bare H-bridge without flyback diodes, and keep motor leads twisted/short to cut noise.
- **Never back-power the Pi from the motor rail**, and never run motors off the Pi's 5V pin.

> **RISK —** Shared-ground noise + TT stall spikes browning out the Pi is the most likely "ghost reboot" bug in the whole build. Budget the caps and the separate supply now; retrofitting after chasing phantom crashes is miserable.

**Power bring-up checklist (run in order, do not skip ahead):**

1. **Pi alone** on the PD supply — boot, confirm stable, check `vcgencmd get_throttled` reads `0x0` under a CPU load.
2. **Motor supply alone** — driver powered from AA pack, Pi powered separately, **logic inputs disconnected**; confirm the driver spins both motors by jumpering inputs by hand.
3. **Join common ground** — tie the grounds, connect the logic GPIOs, and run a deliberately **hard, stall-heavy drive script** (rapid direction reversals, both motors stalled against a hand) from Python while metering the motor rail and re-checking `get_throttled`. Add bulk cap sized to the measured sag if the rail droops or the Pi resets.
4. **Only past step 3** do motors + brain run as one trusted system.

> **DECISION —** Gate E (power/brownout) is **quantitative**, not a vibe check. It **PASSES** only when, under a hard stall-heavy motor drive script, *all three* hold: (1) **no Pi resets**, (2) `vcgencmd get_throttled` **stays `0x0`**, and (3) the measured **motor-rail voltage** (multimeter, or a ~$1 ADC on the bench) **stays above the driver's logic VIH**. Choose the bulk-cap value from that measured sag — don't guess. **NO-GO recovery:** add **470–1000µF bulk + 0.1µF ceramic across VM**, twist and shorten the motor leads, switch to **NiMH** cells (§1), then re-test. This gate sits in sequence in `roadmap.md` — it stands between "Hello motors" and everything after it; no autonomous driving until it passes clean.

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

**Reading the HC-SR04 safely and sanely.** Two failure modes are specific to this sensor on a Pi:

- **The 5V echo line.** The ECHO pin swings to 5V and the Pi's GPIO is 3.3V-only, so it goes through a resistor divider (see §5). **Meter the divider tap with 5V applied and confirm it reads ~3.3V *before* connecting it to any GPIO** — this is its own tiny bring-up milestone in `roadmap.md`, ahead of any sensor code.
- **Jittery software timing.** The echo pulse is measured in software, and on a busy, non-realtime Python loop that timing is noisy. Spec an **echo TIMEOUT**: if no echo returns inside the window, treat the reading as **"unknown" and bias to STOP** — never decay a missed echo into "clear." Also respect the sensor's **~60 ms minimum cycle**: don't poll faster than **~15 Hz**, or pings overlap and readings corrupt each other.

> **DECISION —** HC-SR04 handling: (1) meter the divider tap at ~3.3V before wiring it to a pin; (2) a missed/timed-out echo means **STOP, not clear**; (3) cap the poll rate at ~15 Hz (≥60 ms between pings). The voltage-divider check and the first ranged reading are each their own milestones in `roadmap.md`.

> **RISK —** A single front HC-SR04 leaves the robot blind to the sides, the rear, glass, and drop-offs. It validates the reflex but is not adequate collision coverage for autonomous turning/following, and definitely not for the kid version.

> **OPEN —** Final sensor count and placement undecided. Leading candidate: 2–3 HC-SR04s (front + corners) for v1.x, with an IR cliff sensor considered before the bot is ever left running near stairs or a child. Decide once the chassis geometry is real.

---

## 5. GPIO / pin planning

A DRV8833 channel has **two logic inputs** (AIN1/AIN2, BIN1/BIN2). The naive approach PWMs all four in software — but the **Pi 5 header exposes only 2 *hardware*-PWM lines**, so four software-PWM inputs would mean software PWM on every motor pin. Software PWM jitters under any CPU load, and that jitter shows up as inconsistent motor speed — which directly *worsens* the open-loop drift already documented in §3. Don't do it.

Instead drive each channel **phase/enable**: PWM **one** input per channel on a **hardware-PWM** pin (speed/enable), and drive the **other** input as a plain GPIO (direction/phase). Two motors → exactly **2 hardware-PWM pins + 2 plain GPIOs**, which fits the Pi 5's two PWM lines perfectly. The Pi 5's hardware-PWM pins are **GPIO12/13** (PWM0/PWM1) and **GPIO18/19**; this map uses GPIO12/13.

The HC-SR04 needs a **TRIG** output and an **ECHO** input — and ECHO swings to **5V**, which would damage the Pi's **3.3V-only GPIO**, so it must pass through a resistor divider.

Pin map (BCM numbering — the two motor PWM pins **must** be hardware-PWM pins; the rest can shift):

| Signal | Pi GPIO (BCM) | Notes |
|---|---|---|
| Motor A PWM (enable/speed) | **GPIO 12** | **hardware PWM0** — left channel speed (PWM AIN1) |
| Motor A DIR (phase) | GPIO 17 | plain GPIO — left channel direction (AIN2) |
| Motor B PWM (enable/speed) | **GPIO 13** | **hardware PWM1** — right channel speed (PWM BIN1) |
| Motor B DIR (phase) | GPIO 22 | plain GPIO — right channel direction (BIN2) |
| Driver STBY/EN | GPIO 24 | (TB6612FNG only; tie DRV8833 nSLEEP high) |
| Ultrasonic TRIG | GPIO 5 | 3.3V out → HC-SR04 trigger (fine as-is) |
| Ultrasonic ECHO | GPIO 6 | **via divider** — 5V echo → 3.3V GPIO |
| Common ground | any GND pin | shared with motor supply (see §2) |

ASCII wiring sketch:

```
   Raspberry Pi 5 (3.3V GPIO)            DRV8833 driver            Motors
   ┌──────────────────────┐          ┌────────────────┐
   │ GPIO12 (HW PWM) ────────────────► AIN1 (speed)    │        ┌────────┐
   │ GPIO17 (plain)  ────────────────► AIN2 (dir)  AOUT1/2 ─────►│ Motor A│ (left)
   │ GPIO13 (HW PWM) ────────────────► BIN1 (speed)    │        └────────┘
   │ GPIO22 (plain)  ────────────────► BIN2 (dir)  BOUT1/2 ─────►┌────────┐
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

> **DECISION —** Motors run **phase/enable**, not four software-PWM inputs. PWM one input per channel on a **hardware-PWM** pin (GPIO12/13), drive the other input as plain GPIO for direction. This is forced by the Pi 5 exposing only **2 hardware-PWM lines**, and it's the better design anyway: it kills the software-PWM speed jitter that would otherwise compound the open-loop drift (§3).

> **RISK —** On the Pi 5 the GPIO moved behind the **RP1 southbridge**, so the classic **`RPi.GPIO` library does not work** — and a large fraction of HC-SR04 and DRV8833 tutorials are written against it, where they will **silently fail** (no error, just no pin activity). **Mitigation:** use **`gpiozero` on the `lgpio` / native pin factory** (the default Pi 5 backend), and do **not** copy-paste `RPi.GPIO` snippets. Prove the stack works with a "GPIO first light" step — blink one LED / toggle one motor pin — which belongs **first** in `roadmap.md`, before any motor or sensor code.

> **RISK —** Wiring HC-SR04 ECHO straight to a Pi GPIO feeds 5V into a 3.3V pin and can fry it. The 1kΩ/2kΩ divider (or any ~2:1 ratio dropping 5V to ~3.3V) is mandatory, not optional.

---

## 6. Budget

| Phase | What it covers | Status | ~Cost |
|---|---|---|---|
| **Phase 1 — Brain** | CanaKit Pi 5 4GB Starter Kit PRO (~$180) + Logitech C270 (~$22) | ✅ Ordered | ~$200 |
| **Phase 2 — Body** | Card reader, PD power bank, TT motors, driver, ball caster, HC-SR04, AA power, wiring, glue/chassis | ✅ ORDERED 2026-06-20 (inbound) | ~$115 |
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
