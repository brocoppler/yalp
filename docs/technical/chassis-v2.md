# Chassis v2 — the rebuild spec

> Why the cardboard body is now the limiting factor, what the replacement must do, and the staged plan to swap it without losing the known-good baseline. `hardware.md` is the v1 design; `as-built-wiring.md` is what is physically on Izzy today; this is the body that replaces it.

---

## 1. Why now

The v1 cardboard body did its job. It got the whole stack from parts-in-a-box to a
floor-driving robot: Gate E, milestone H, milestone J, the first floor drive, and
`yalp see` on-robot are all green on it. Nothing here retracts that.

What changed is that the body is now the thing standing between us and the next
milestone. The wheels are pressed into cardboard slots, the third contact point is a
small hard metal ball, and the power and signal paths run through solderless
breadboards on a moving platform. None of those hold a dimension.

> **THESIS —** Every calibration constant in the software is a fit to a physical
> object. The v1 body is not a fixed physical object — its geometry changes with load,
> with handling, and over time. That is why tuning does not stick, and why the fix for
> each new drive defect has been another fudge factor rather than a closed defect.

---

## 2. The evidence — the defect backlog is a body backlog

The `next-session-handoff.md` backlog lists six open defects. Five are mechanical:

| # | Filed as | Actual root cause |
|---|---|---|
| 1 | Open-loop timed distance over-reports ~1.8× | No encoders — nothing measures travel |
| 2 | Veer ~7 cm/m after `right_trim 0.90` | Axle alignment + caster, not motor mismatch |
| 3 | Floor-graze mode ~0.6 m | Sensor mount is not rigid |
| 4 | Battery sag ~26% per session | Open-loop drive cannot compensate for it |
| 5 | Rotation model over-predicts 5–10× | Caster swivel drag + pivot scrub |

The software has absorbed these as `right_trim`, `duty_deadband`, `turn_duty_deadband`,
and `TURN_STICTION_DUTY` — four constants that exist to describe a body that will not
hold still.

### 2.1 Rotation-vs-translation isolates the caster

The 2026-07-17 tuning data separates the two failure modes cleanly:

- **Translation**, after recalibrating `max_speed_mps` to 0.29: commanded 1.0 m
  delivered **0.94 m** sonar-true.
- **Rotation**, same session, same duty: commanded 30–60° delivered **2–15°**.

If the loss were motor-side — weak motors, a sagging pack, a driver limit — translation
and rotation would degrade *together*. They do not. Rotation is 5–10× worse than
translation at the same duty, which means the loss is **specific to rotation**. Only two
things resist rotation but not translation: the caster's swivel friction, and wheel
scrub from pivoting about a point that is not the wheel-axis midpoint.

### 2.2 Direction-dependence isolates the mounts

Measured on hardwood: forward reliably breaks stiction; backward and right-pivot bursts
often do not. Reverse is slower than forward and pulls the nose right.

> **DECISION —** Stop trying to trim the veer away. A per-wheel scalar multiplier
> (`left_trim` / `right_trim`) is **mathematically incapable** of correcting a
> direction-dependent error — it applies the same factor whichever way the wheel turns.
> That is why `right_trim 0.90` left ~7 cm/m of residual veer and why reverse still pulls
> right. Direction-dependent asymmetry comes from a trailing caster that must flip 180°
> on reversal, and from motors that settle at a different resting angle in a compliant
> mount depending on torque direction. Both are fixed in the body or not at all.

### 2.3 The connection failures are already costed

Two multi-hour losses are on the record, both purely mechanical:

- Ultrasonic reads degrading over minutes, traced to a loose breadboard leg at the
  divider TAP column (`as-built-wiring.md` §2).
- A morning lost to total immobility, traced to the pack's red VM lead pulling out of
  breadboard J1 during charging (`next-session-handoff.md`).

Neither was a software bug. Both are structural consequences of running power and
signal through friction-fit breadboard holes on a vehicle that moves and gets picked up.

---

## 3. This is not a violation of the v1 non-goals

`product-vision.md` §5.1 makes "a nice-looking or durable body" an explicit v1 non-goal,
and §3 warns specifically against merging the kid-version horizon into v1. This rebuild
does neither.

> **DECISION —** Chassis v2 buys **dimensional repeatability**, not aesthetics and not
> child-durability. Those two remain out of scope and remain the post-v1 pet's problem.
> Repeatability is a different axis, and it is a prerequisite for the *current*
> milestone — not an anticipation of a later one.

**It is on the critical path.** The next milestone is **M**, person-following: a
continuous closed-loop steering behavior that issues a stream of small turn corrections.
With pivots delivering 2–15° of a commanded 30–60°, every small correction lands inside
the dead zone — the controller commands, nothing moves, the error grows, it commands
harder. That body makes the follow controller untunable. Fixing it unblocks M rather
than delaying it.

---

## 4. Keep the architecture — rebuild the execution

> **DECISION —** v2 stays **2WD differential drive with a passive third contact point**.
> This is the correct architecture for a light indoor robot on hard floors: the simplest
> kinematics, the most predictable turn behavior, and it is already what the code models.
> 4WD adds turn scrub, which would make the worst measured defect worse. Tracks are worse
> for odometry. **The design is right; the execution failed.** Do not redesign — rebuild.

The v1 build also already burned the "train chassis" trap (one motor per axle → drives
straight, never turns; `hardware-runbook.md` §7). One driven wheel per *side* is settled
and carries forward unchanged.

---

## 5. What the body must do — the spec

1. **Hold both drive axles collinear and fixed, permanently.** This is what makes a
   calibration value mean something a week later.
2. **Put ~70–80% of the weight on the drive wheels**, with the caster lightly loaded.
   Traction is what breaks stiction — this attacks the duty-0.45 deadband directly.
3. **Place the drive axle at or near the CG**, so a pivot is a true in-place rotation
   rather than dragging the body through an arc.
4. **Provide a near-frictionless, direction-independent third contact point.**
5. **Run quietly enough to want to run it.** See §5.1 — this is a real requirement, not a
   polish item.
6. **Hold every sensor in a rigid, verifiable pose.** A hand adjustment must not be able
   to change the sonar's aim.
7. **Mechanically retain every electrical connection.** No load-bearing friction fits.
8. **Be serviceable** — charge and swap the pack without disturbing any wiring.
9. **Be encoder-ready**, even though the encoder code lands later.

### 5.1 Acoustics is a requirement, not polish

The v1 caster is **loud** on hardwood — a constant rattling buzz under way. This was not
previously written down anywhere, and it belongs in the spec for two reasons.

> **THESIS —** A robot that is unpleasant to run is a robot that gets run less, and
> session count is the actual rate limiter on this project. Noise is not an aesthetic
> concern like looks are — it directly suppresses the experiment loop. Separately,
> `product-vision.md` §3 has this becoming a kid's robot pet: a clattering pet is a failed
> pet regardless of how well it drives.

Note this is **not** the v1 "looks and durability" non-goal creeping back in (§3). Noise
is a functional property of the drivetrain, and its causes are the same mechanical
compliance and contact geometry this whole rebuild is about.

**Three sources, in likely order of contribution:**

1. **Surface-following.** A small ball tracks every bit of floor texture and every plank
   chamfer; a large ball rides over them. **Ball diameter acts as a low-pass filter on the
   floor profile.** This is a size variable, not a material one.
2. **Cage rattle.** A small ball loose in a plain plastic socket is excited by that same
   vibration and bounces inside its housing — a constant metallic buzz, usually louder than
   the floor contact itself. The *constant* character of the v1 noise points here.
3. **The deck as a soundboard.** A large, light, flat cardboard panel is an effective
   radiator; the body amplifies whatever the caster generates. **This one is fixed for free
   by the rigid kit deck** (§6.1) — heavier and stiffer radiates far less.

---

## 6. Decisions

### 6.1 Deck — a 2WD chassis kit

> **DECISION —** The deck is a **bought 2WD robot chassis kit** (acrylic or aluminum
> plate, pre-drilled, with proper TT motor brackets, hardware, and standoffs). ~$15–20.
> Rationale: the precision-critical part of this build is holding the two motor axles
> square and parallel, and a purpose-made bracket on a pre-drilled rigid plate does that
> by construction. The remaining learning in this project is in behavior and control, not
> in chassis fabrication — spend the evening on the swap, not on cutting and drilling.

**What to check when buying:** it must be a **2-wheel** chassis (one motor per side), not
a 4WD plate; it must include or accept **TT motor brackets**; and the plate should have
open hole grids or slots so the Pi, pack, and driver board can be placed where §7 needs
them rather than where the kit assumed.

**Discard from the kit:** the tiny ball caster it ships with (see §6.2), and its battery
box if it is a bare friction-contact holder.

### 6.2 Third contact point — a large, light, polymer ball caster

The v1 caster fails on **two** axes, and the noise one (§5.1) is the one that actually
prompted this rebuild:

- **Noise** — small diameter follows floor texture, and the ball rattles loose in its
  plain socket.
- **Pivot drag** — contributes to the rotation deficit measured in §2.1.

> **DECISION —** Replace it with a **large-diameter ball caster, ~25–40 mm, with a
> POM/nylon ball rather than steel, bearing-supported rather than a plain socket** (~$5–10).
> This is the one part that addresses both axes at once: large enough to ride over floor
> texture instead of tracking it, polymer contact instead of ringing metal, preloaded
> against bearings so there is no cage rattle, and omnidirectional so it introduces no
> reverse-flip asymmetry.

**Plus two measures that attack the noise *path* rather than its source:** a rubber or
foam washer between the caster housing and the deck to break the structure-borne route
into the panel, and the rigid deck itself (§6.1), which radiates far less than cardboard.

**On mass — "a light version of a lacrosse ball" is the right target.** The diameter
instinct is correct; the mass is the trap. A real lacrosse ball is ~145 g sitting at the
far end from the drive axle, which drags the CG toward the caster — exactly backwards from
the weight-on-the-drive-wheels requirement (§5 item 2, §7) that is the lever on the
duty-0.45 deadband. Specify **lacrosse-ball diameter, hollow-plastic mass.**

> **CORRECTION —** An earlier draft of this spec argued against a rubber ball on friction
> grounds. That objection does not survive the load numbers. With the CG placed per §7 the
> caster carries roughly 20–30% of a ~700 g–1 kg robot — a couple of newtons — and a ball
> caster **rolls** along the pivot arc rather than sliding, so material affects mainly
> contact-patch spin friction, which is small at that load. Rubber at the contact was never
> the pivot problem. **Diameter and cage fit are what matter**; contact material is chosen
> for noise, not for drag.

**Fallback if it is still audible:** a **rubber-tread swivel caster** — the quietest option
available (large diameter, damped tread, real axle bearing). The cost is that it is a
*trailing* caster and must flip 180° on reversal, reintroducing one of the contributors to
"reverse pulls the nose right" (§2.2). That is a measurable trade, not a guess: take it if
quiet is worth more than open-loop reverse accuracy, and note that closed-loop control
would later absorb the disturbance anyway.

### 6.3 Motors — encoder TT motors, wired but unread

> **DECISION —** Fit **TT gear motors with integral quadrature encoders** (~$10 each) in
> place of the plain TT motors. They are a physical drop-in — same gearbox, same body,
> same shaft — plus encoder power and two channel wires per motor. **Mount them, run the
> encoder wires to free GPIO, and leave them unread.** No code changes in this rebuild.

**Rationale.** This is the one decision that determines whether the drivetrain gets built
once or twice. When the encoder code does land it retires backlog items **1, 2, 4, and 5**
outright: closed-loop speed matching kills veer with no trim at all, real odometry kills
the 1.8× distance error, real angles replace the broken rotation model, and speed control
makes battery sag invisible to behavior. `hardware.md` §3 already flags encoders as the
answer if open-loop drift ever becomes the limiting factor. It has.

**GPIO budget — confirmed available.** In use today: **5, 6, 12, 13, 17, 22** (plus 24
reserved and inert for the TB6612 fallback). Four encoder channels fit comfortably; 16,
18–21, 23, 25–27 are all free. Pin assignment is deferred to whoever writes the encoder
code — this rebuild only has to route the wires and leave them accessible.

> **RISK —** Encoder wires terminated and abandoned invite the assumption that encoders
> are live. They are not. Label the harness, and leave a note in `as-built-wiring.md`
> stating the channels are physically present and software-unread.

---

## 7. Layout requirements

These are the parts the kit will not decide for you, and they are where the measured
defects actually get fixed.

| Requirement | Why | How to check |
|---|---|---|
| Drive axle at/near the CG, fore-aft | Makes a pivot a rotation rather than a drag; minimizes caster scrub radius | Balance the assembled deck on a pencil across the axle line — it should sit close to neutral |
| ~70–80% of mass on the drive wheels | Traction breaks stiction; this is the lever on the duty deadband | Lift the caster end — it should feel light |
| Caster as close to the axle as the footprint allows | Shortens the arc the caster traces during a pivot | Layout |
| Heavy items (pack, Pi) placed to satisfy the two rows above | The pack is the single biggest mass — it is the CG trim tool | Layout |

**Sonar geometry.** With a ~15° beam half-angle, a level sensor at height *h* first grazes
the floor at roughly **3.7 h**. Mount as high as the layout practically allows, **level or
1–2° upward** (never nosed down), on a screwed bracket square to the drive axis. Record
the transducer-face offset from the front bumper so served distance can be related to
actual clearance.

**Pre-cut the corner-sensor mounts.** `hardware.md` §4 already calls for growing to 2–3
sensors (front + angled front-corners) because one forward cone cannot cover a robot that
turns and follows. Establish those mounting points now even if only the centre sensor is
populated — it costs nothing during this build and avoids a third rebuild.

**Camera.** The C270's clip base has a tripod thread — screw it to a fixed bracket rather
than clipping it to an edge. Follow-mode steering assumes the camera looks along the drive
axis; a camera that rotates when bumped puts a standing bias into the centering error that
no amount of controller tuning will find.

---

## 8. Electrical hardening

The pin map does **not** change. What changes is how each connection is retained.

| v1 (as-built) | v2 | Kills |
|---|---|---|
| DRV8833 on a solderless breadboard | Soldered to perfboard, **screw terminals** for VM and motor outputs | The J1 VM-lead pull-out class |
| ECHO divider on a mini breadboard | Two resistors soldered to perfboard | The degrading-reads class |
| Pack leads into breadboard holes | **JST/XT30 connector** + strain relief | Charging disturbing the wiring |
| Loose jumper bundles | Bundled and strain-relieved at both ends | Vibration-induced intermittents |

> **DECISION —** Keep the two-supply / one-common-ground topology and the pin map exactly
> as `hardware.md` §5 and `as-built-wiring.md` §3.1 record them. v2 changes **retention
> only**, not topology and not pin assignment. This is what keeps the rebuild a
> zero-code-change operation.

**Cells.** Stay on 4×AA NiMH per the existing `hardware.md` §1 decision — the no-lithium
call is deliberate. Upgrade to high-capacity, low-internal-resistance cells (Eneloop
class) and a holder with solid contacts: part of the measured 26% session sag is
spring-contact resistance in a cheap holder, not cell depletion.

**Bulk capacitance.** The 1000 µF bulk and the 0.1 µF HF cap carry over. This is also the
natural moment to replace the substituted 0.1 µF *electrolytic* with the specced **"104"
ceramic disc** (`as-built-wiring.md` §3.3).

---

## 9. No code changes

Verified against the source, not assumed:

- There are **no wheel-diameter or track-width constants anywhere** in the codebase. The
  entire motion model is empirical.
- The pin map is unchanged, so `config.py` needs no edit.
- Everything the rebuild invalidates lives in `~/.config/yalp/calibration.json` and is
  re-derived by **`yalp calibrate`** — an interactive bring-up that catches a backwards
  miswire, drives a fixed straight to measure forward speed, and spins in place to measure
  turn rate.

> **DECISION —** The rebuild costs **one calibration run, not a line of code.** Expect
> `left_invert` / `right_invert` to change when the motors are remounted — `yalp calibrate`
> handles it. Back up the current `calibration.json` before starting; the pre-tune backup
> at `calibration.json.bak-pretune` should be left alone.

---

## 10. Staged plan

> **RISK —** The failure pattern in this project's history is **compounded debugging** —
> the train chassis, the PMIC failure, and the gpiozero timing defect all overlapped, and
> each made the others harder to see. The mitigation is the same one that eventually
> untangled them: change one class of thing at a time and re-verify with the tooling that
> already exists.

### Stage 0 — Measure the current body first (no purchases)

~45 minutes on the existing robot. This converts opinion into a spec and produces a
*before* number to beat.

- Narrow the true minimum-moving duty, forward and reverse, on hardwood. (Known: 0.30
  fails, 0.45 works.)
- Repeat with the caster shimmed clear of the floor. **If the deadband collapses, the
  caster is confirmed as the dominant loss** and the size of the win is on paper.
- Check axle parallelism with a square; spin each wheel by hand and watch for wobble.

**Noise source triage (~5 min, settles §5.1).** Push the robot by hand across hardwood,
then a rug, then a smooth table:

| Observation | Dominant source | Fix |
|---|---|---|
| Loud on hardwood, quiet on rug | Surface-following | Diameter (§6.2) |
| Loud on every surface | Cage rattle | Bearing-supported caster (§6.2) |
| Buzz stops when a finger holds the ball still in its socket | Cage rattle, confirmed | Bearing-supported caster |

### Stage 1 — Mechanical

Assemble the kit deck with the encoder motors on proper brackets and the ball transfer
unit. Lay out to §7 (axle at CG, weight on the drive wheels). Move the electronics across
using `as-built-wiring.md` as the map. **Do not change the pin map.**

### Stage 2 — Electrical

Driver and divider onto soldered perfboard with screw terminals; connectorize the pack;
strain-relieve every bundle. Route and label the encoder harness, unread.

### Stage 3 — Sensors

Rigid, square sonar bracket per §7 geometry. Screwed camera mount. Corner-sensor mount
points established.

### Stage 4 — Re-verify, in the original order

Use the tooling already built for exactly this. Nothing here is new work.

| Step | Command | Gate |
|---|---|---|
| GPIO stack | `python scripts/verify_gpio_stack.py` | lgpio factory, no RPi.GPIO |
| Motor channels | `yalp hwtest --check motors --matrix` | all 6 steps correct |
| Sonar | `yalp hwtest --check ultrasonic --seconds N --hz 15` | rigid box at 30 cm reads ~29–31 cm |
| **Gate E** | stall-heavy drive + `vcgencmd get_throttled` | **mandatory re-run** — the power path was rebuilt |
| Motion | frame-verified motion gate | one visible pivot burst in camera frames |
| Floor | `yalp drive` | a clean lap with collision-stop live |
| Calibration | `yalp calibrate` | new `calibration.json` written |

> **DECISION —** **Gate E is re-run, not inherited.** The 2026-07-03 pass certified a
> power and decoupling path that this rebuild physically replaces. Re-running it is not
> ceremony — the caps, the connector, and the pack contacts are all new.

### Stage 5 — Resume the roadmap at M

Person-following, on a body whose small turn commands actually produce small turns.

---

## 11. What this invalidates

- **`as-built-wiring.md` §3 becomes stale on Stage 1.** Its breadboard node references are
  cross-referenced elsewhere — notably the "red = VM = breadboard J1" note in
  `next-session-handoff.md` under *"Motor power path — do not re-diagnose."* That note
  must be rewritten, not just deleted, or the next session will chase a node that no
  longer exists.
- **`calibration.json` is invalidated.** Back it up; re-derive via `yalp calibrate`.
- **Backlog items 2, 3, and 5** should be re-measured after Stage 4 rather than carried
  forward — their numbers describe the old body.
- **Backlog items 1 and 4** stay open until the encoder code lands; the hardware for them
  will be physically present and waiting.

---

## 12. Bill of materials — what to buy

Prices and stock checked 2026-07-31. Vendors: **Pololu**, **DigiKey**, **Amazon**.

### 12.1 Spec-critical — buy exactly these

These two are the parts where the specific model matters. Both were checked against the
§6 spec directly.

| # | Part | Link | Price | Why this exact one |
|---|---|---|---|---|
| 1 | **Pololu Ball Caster, 1″ plastic ball + ball bearings** | [pololu.com/product/2692](https://www.pololu.com/product/2692) | **$9.95** | Exact match to §6.2. 1″ (25.4 mm) **POM/acetal** ball — polymer, not steel. Rides on **three 3×7×3 mm ball bearings**, not a plain socket, so there is no cage rattle (§5.1 source 2). 29 mm overall height, three M3/#4 mounting holes. Rated for differential-drive robots up to ~10 lb. |
| 2 | **DFRobot FIT0450 — TT Motor with Encoder, 6 V 160 RPM 120:1** ×2 | [digikey.com FIT0450](https://www.digikey.com/en/products/detail/dfrobot/FIT0450/7597205) | **$7.40 ea → $14.80** | §6.3. **Integrated quadrature** encoder (A + B phase) — gives direction, unlike slotted code-wheel kits. 3–7.5 V motor / 4.5–7.5 V encoder, so it runs on the existing ~5.55 V pack. In US stock at DigiKey. |

> **DECISION —** Take the **ball-bearing** caster (2692), not the plastic-roller version
> (2691). Same dimensions, ~$3 more, and the bearings are precisely what §5.1 identifies as
> the fix for cage rattle. This is the single part the noise complaint is about — do not
> economise here.

> **DECISION —** Take an **integrated quadrature** encoder over the cheap alternatives.
> Both the Adafruit TT Motor Encoder (`3782`) and the slotted code wheels bundled into most
> chassis kits are **single-channel photo-interrupters — they count ticks but cannot tell
> direction**, and they need a separately-mounted sensor held in alignment with a slotted
> disc. Given that this entire rebuild exists because alignment-sensitive mounts keep
> failing (§2.3), adding another one would be self-defeating. The FIT0450's encoder is
> sealed inside the motor with no alignment to lose.

### 12.2 Standard parts — brand doesn't matter, spec does

| # | Part | Example | ~Price | What to check |
|---|---|---|---|---|
| 3 | **2WD chassis kit** — acrylic plate, TT motor brackets, wheels, hardware | [Amazon B0G5N62L4W](https://www.amazon.com/2WD-Robot-Chassis-Platform-Raspberry/dp/B0G5N62L4W) · [RobotShop equivalent](https://www.robotshop.com/products/xiaor-geek-2wd-robot-car-chassis-kit-with-tt-motor-battery-box-acrylic-chassis-and-2-wheels-diy-project-smart-robot-chassis-robots-car-platform-arduino-raspberry-pi) | $15–20 | Must be **2WD** (one motor per side — §4), must include **TT motor brackets**, and the plate should have an open hole grid so §7 layout is free. Discard its caster and its code wheels. |
| 4 | **GPIO screw-terminal HAT** for the Pi 5 | [Adafruit Pi-EzConnect 2711](https://www.adafruit.com/product/2711) · [GeeekPi (Amazon)](https://www.amazon.com/GeeekPi-Screw-Terminal-Raspberry-Extension/dp/B08GKQMC72) | $10–15 | **Verify Pi 5 compatibility and clearance against the CanaKit fan case before buying** (§12.3). |
| 5 | **Eneloop AA ×4** | [Panasonic BK-3MCCA4BA](https://www.amazon.com/Eneloop-Rechargeable-Batteries-Controller-Flashlight/dp/B00JHKSN76) | $15 | 2000 mAh, 2100 cycles, low internal resistance. **Eneloop Pro** ([BK-3HCCA4BA](https://www.amazon.com/Panasonic-BK-3HCCA4BA-eneloop-Pre-Charged-Rechargeable/dp/B00JHKSL28), 2600 mAh) if the ~26% session sag still bites. |
| 6 | **M3 standoff / screw / nut assortment** | [Amazon B06Y4Y4DNW](https://www.amazon.com/Fastening-Standoff-Assortment-Installation-Raspberry/dp/B06Y4Y4DNW) | $12 | Nylon or brass. Mounts the Pi, the caster, and the sensor bracket. |
| 7 | Perfboard + 2- and 3-pos screw terminals + header strip | any electronics supplier | $8 | Driver-side hardening (§8). |
| 8 | JST-XH or XT30 pair + strain-relief / cable clamps | any electronics supplier | $6 | Pack connectorisation (§8). |
| 9 | "104" 0.1 µF ceramic disc | any electronics supplier | $1 | Replaces the substituted electrolytic (§8). |
| 10 | Rubber or foam washers, M3 | hardware store | $1 | Caster mount isolation (§5.1, §6.2). |

**Total: ~$92–107.**

> **THESIS —** Against a ~$335 project — and against the two multi-hour debugging sessions
> already spent on a loose divider leg and a pulled VM lead — this is the cheapest line item
> in the build. The v1 body was the right call for getting started and is the wrong call for
> continuing.

### 12.3 Check before you commit — two unverified fits

Neither of these is a reason to delay the order; both have clean fallbacks. But they are
**not** confirmed, and should not be treated as settled.

> **RISK —** **FIT0450 mechanical compatibility with the chassis kit is unverified.** Two
> specific unknowns: whether the encoder-equipped body seats in the kit's TT bracket (the
> encoder adds length at the rear), and whether its output shaft takes the kit's wheels
> (DFRobot describes a "copper output shaft with embedded thread" — this may not be the
> standard TT D-profile). The dimension drawing is on the DFRobot product page; check it
> against the kit before assembly.
> **FALLBACK:** the chassis kit ships with plain TT motors. If the FIT0450s do not fit,
> **build with the kit's motors** — the encoders are wired-but-unread in this rebuild
> anyway (§6.3), so nothing in Stages 1–5 is blocked. The encoder swap simply becomes its
> own later task alongside the encoder code.

> **RISK —** **GPIO terminal HAT clearance against the CanaKit active-cooler case is
> unverified.** The HAT occupies the 40-pin header, which that case encloses.
> **FALLBACK:** skip the HAT and terminate the Pi side on the same soldered perfboard as
> the driver (§8). The HAT is a convenience that removes soldering, not a requirement.

### 12.4 Order sequencing

> **DECISION —** Order in two waves. **Wave 1 — the chassis kit, the Pololu caster, the M3
> kit, and the washers.** These are what actually fix the noise (§5.1) and the geometry
> (§7), which is the entire point of the rebuild, and they have no compatibility unknowns.
> **Wave 2 — the FIT0450s and the electrical-hardening parts.** The encoders are unread
> until the encoder code lands, so they are not on the critical path and a fit problem
> (§12.3) costs nothing in schedule.

---

## 13. Open questions

> **OPEN —** Encoder GPIO pin assignment and the closed-loop control code are deferred.
> The wires will be physically present and labelled from Stage 2; nothing reads them.
> Decide the pin map when the code is written, not before.

> **OPEN —** Whether the new caster alone clears the pivot deficit, or whether pivot
> performance also needs the CG/axle placement of §7 to be tight. Stage 0's caster-lifted
> measurement should predict this; Stage 4's calibration will settle it.

> **OPEN —** Whether the §6.2 primary (large polymer ball, bearing-supported) is quiet
> enough, or whether the rubber-tread swivel fallback is needed — and if so, whether its
> reverse-flip asymmetry is acceptable pre-encoders. Decide by ear at Stage 4, against the
> Stage 0 noise triage as the baseline.

> **OPEN —** Corner sonar count and angle (`hardware.md` §4 leaves this open pending real
> chassis geometry). v2 establishes the mounting points; the decision can now finally be
> made against a real, fixed footprint.

---

## Cross-references

- `hardware.md` — the v1 design: BOM, power architecture, drivetrain honesty (§3), sensing
  limits (§4), and the pin map (§5) that v2 preserves unchanged.
- `as-built-wiring.md` — what is physically on Izzy today; the map for the Stage 1 move,
  and the doc that Stage 1 obsoletes.
- `hardware-runbook.md` — the bench procedures, including the "train chassis" warning (§7)
  and the Gate E staged bring-up that Stage 4 re-runs.
- `roadmap.md` — the gate ledger; milestone **M** is what this rebuild unblocks.
- `next-session-handoff.md` — the defect backlog §2 re-reads as a body backlog.
