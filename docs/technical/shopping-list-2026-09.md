# Shopping list — September 2026 (third deck, closed-loop sensors, test rig)

> What to order now that Izzy drives straight on camera heading alone, the code for
> an IMU, a pack monitor and wheel encoders is written and waiting for hardware, and
> the two-level acrylic body is out of room. Companion to `chassis-v2.md` §12 (the
> rebuild BOM — items already bought there are not repeated) and
> `as-built-wiring.md` §0 (the body as it stands).

---

## 0. Where the body is (2026-09-16)

Two-level 2WD acrylic chassis (the `chassis-v2.md` §12 kit), TT motors, 4×AA pack.

| Deck | Carries | Room left |
|---|---|---|
| **1 (bottom)** | battery packs, HC-SR04 ultrasonic | some |
| **2 (top)** | Pi 5, the two breadboards (DRV8833 drivetrain + sonar divider), C270 camera | **none** |

The camera is the item that has run out of space, and it is also the item that most
wants a rigid, screwed mount looking straight down the drive axis (it is now the
**heading sensor**). Everything below is sized around a third deck.

---

## 1. Space — the third deck (order first)

| # | Part | Qty | ~Price | Notes / what to check |
|---|---|---|---|---|
| 1 | **Extra chassis plate matching your kit** (same hole grid; many 2WD kits sell an "extra layer"/"3rd layer" plate; else any 2–3 mm acrylic/ABS plate cut to the same footprint) | 1 | $6–15 | **Measure the existing plate first** — length × width, standoff hole spacing, and whether it has the centre slot for cables — and record it in `as-built-wiring.md` §0. A plate with an open hole grid beats a plain sheet. |
| 2 | **M3 brass female–female standoffs, 40–50 mm** | 4–6 | $8 | Tall enough to clear the Pi 5 fan case + HAT. The kit's short standoffs are for the deck you already have. |
| 3 | M3 × 8 mm screws + M3 nuts | 10 | (in a kit) | Usually in the `chassis-v2.md` #6 assortment. |
| 4 | **Camera mount** — a mini ball head or an L-bracket with a **1/4"-20** stud, plus 1/4"-20 nut | 1 | $6–10 | The C270 clip base has a tripod thread. Bolt it to deck 3 pointing straight down the drive axis, level. A camera that rotates when bumped puts a standing bias into the heading hold. |

**Height check:** three acrylic decks on 40–50 mm standoffs is ~15–18 cm to the camera
lens. That is fine (the sonar stays low on deck 1; the camera high is good for the
follow-mode view and keeps the pack, the heaviest item, at the bottom for traction).

**Alternative that frees a deck:** the GPIO screw-terminal / proto **HAT** already in
`chassis-v2.md` §12 #4 — solder the DRV8833 module and the ECHO divider to it and stack
it on the Pi. Both breadboards disappear. Do this as chassis-v2 Stage 2; get the plate
now anyway — the sensors below want the room.

---

## 2. Sensors the code is already written for

The 2026-09-16 code adds drivers for all three; each is optional and auto-detected.

| # | Part | Qty | ~Price | Why / notes |
|---|---|---|---|---|
| 5 | **MPU-6050 IMU module (GY-521 breakout)** | 1 (+1 spare) | $3–6 ea | Yaw at 100+ Hz with no camera latency, works in the dark and with a person walking through the frame (FOLLOW mode). I2C, 3.3 V. `yalp/reactive/imu.py`. Mount flat, X forward, on the top deck, away from the motors. Upgrade option: BNO085 (fused absolute heading, ~$25) — not needed for yaw hold. |
| 6 | **INA219 current/voltage breakout** (Adafruit 904 or generic) | 1 | $6–10 | Pack voltage + current → "is the pack on / how charged / how much sag". Goes in series with the pack's + lead. `yalp/reactive/power_monitor.py`. Shares the I2C bus (addr 0x40 vs the IMU's 0x68). |
| 7 | **DFRobot FIT0450 encoder TT motors** | 2 | $15 pair | Already in `chassis-v2.md` §12.1 #2 — if not yet ordered, order now: the quadrature encoder driver (`yalp/reactive/encoders.py`) closes the distance loop. Check the §12.3 fit risk. |
| 8 | Dupont jumpers **female–female 20 cm** (I2C + encoders) and a 4-pin I2C splitter or a 2×4 header strip | 1 pack | $6 | Both I2C devices share SDA/SCL/3V3/GND — a small header strip on the proto board or breadboard is the clean way. |

**Pin plan (already in the code; see `hardware.md` §5):** I2C-1 SDA = GPIO2 (pin 3),
SCL = GPIO3 (pin 5); encoders left A/B = GPIO16/26 (pins 36/37), right A/B = GPIO20/21
(pins 38/40); all sensor VCC from **3V3** (pins 1/17), never 5 V. Enable I2C once:
`sudo raspi-config nonint do_i2c 0`, then `i2cdetect -y 1` should show `40` and `68`.

---

## 3. Test rig — cheap, and it halves session time

| # | Part | Qty | ~Price | Why |
|---|---|---|---|---|
| 9 | **Wheels-up stand** — a wooden block / small box ~8 cm tall that fits between the wheels | 1 | $0–10 | Motor and encoder checks without motion or the SAFE_STOP latch. |
| 10 | **Painter's tape** + a printed **protractor** (or a floor tile grid) | 1 | $5 | `yalp calibrate --heading` needs a 90° floor mark you read by eye — this is what ground-truths every heading number. |
| 11 | **2 m tape measure** | 1 | $5 | `yalp calibrate` distance step; a marked 2 m lane ending at a wall is the ideal straight-drive test track. |

---

## 4. Power (from `chassis-v2.md` §12 #5, repeated because it is now measurable)

| # | Part | Qty | ~Price | Why |
|---|---|---|---|---|
| 12 | Eneloop / Eneloop Pro AA | 8 (two sets) | $20–30 | One set charging while one runs; the INA219 will show the sag curve directly. |
| 13 | A decent 4/8-bay NiMH charger (if you don't have one) | 1 | $20 | — |

---

## 5. Small parts (chassis-v2 Stage 2 electrical hardening — unchanged)

`chassis-v2.md` §12 #7–#10: perfboard + screw terminals, JST-XH/XT30 for the pack, "104"
0.1 µF ceramic, rubber washers. Add: **cable ties / adhesive cable clips** (the encoder
and I2C harnesses are 8 more wires on a moving body), and a **short right-angle USB-A
cable** for the C270 if the deck-3 route is tight.

---

## 6. Totals and order of ordering

| Wave | Items | ~Total |
|---|---|---|
| **Now** | #1–#4 (deck + mount), #5, #6, #8, #9–#11 | **$45–70** |
| **Next** | #7 encoder motors (if not already), #12–#13 | $35–60 |

**Before ordering #1:** measure the plate and check whether your kit's vendor lists an
extra layer. **Before ordering #7:** `chassis-v2.md` §12.3 (shaft/bracket fit).

---

## 7. What to do the day the parts arrive (in this order)

1. Deck 3 on; camera bolted to it, level, on the drive axis. Re-run
   `yalp calibrate --heading` (a new camera position = a new HFOV check).
2. IMU + INA219 on the I2C bus; `i2cdetect -y 1`; `yalp hwtest --check imu` and
   `--check power`. Set `YALP_IMU=1` (or let auto-detect find it) and drive: the
   timeline shows `hdg=` from the gyro with `src=imu`.
3. Encoders (with the FIT0450 swap, chassis-v2 Stage 1): `yalp hwtest --check encoders`
   (spin each wheel by hand: ticks count up forward, down backward), then
   `yalp calibrate` for `max_speed_mps` — after that, distance goals close on odometry.
