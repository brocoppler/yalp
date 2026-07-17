# Next-session handoff — resume here

**Last updated:** 2026-07-16, late evening (updated after root-cause closure).
**One-line status:** Root cause of two-day ultrasonic saga confirmed and fixed in
code — gpiozero Python-side timing on Pi 5 manufactures 2x/4x range inflation;
`GpiodUltrasonicSensor` now auto-selected. Battery sag still blocks motor work —
pull latest onto izzy, charge/swap the pack, validate the door reading, then run
the victory lap.

---

## Where we are

- **2026-07-16 supervised floor drive: DONE.** 1.2 m on hardwood via
  `DRIVE_GOAL` straight 1.5 @ 0.3 through `RealReactiveBackend`. Ran under a
  relaxed-grace env config (`YALP_ULTRASONIC_GRACE_MS=20000`,
  `MAX_MISSES=100000`) — supervised-only safety net, never a default. Completed
  on the goal timer; motors ran clean; zero reverse commands. The
  distance-based `SAFE_STOP` stayed armed the whole run but was never reached
  because the sonar never saw the door (reason: it was pointing at the wrong
  wall — see below).

- **ROOT CAUSE IDENTIFIED AND FIXED: gpiozero Python-side echo timing on Pi 5
  manufactures 2x/4x range inflation.** Simultaneous capture proved it:
  `libgpiod` v2 kernel-timestamped edges read 29.0–29.6 cm on a 30 cm box;
  gpiozero read 116–118 cm on the same scene. The earlier "sonar boresight
  ~35-45° left" finding was this artifact — the pivot-sweep "left wall track"
  was 2× a shoe/kettlebell cluster dead ahead; the quilt/towel falsification
  test re-reads as the door at 2.08× true range. The HC-SR04 module and
  bracket are aimed correctly. **The `GpiodUltrasonicSensor` driver is now
  auto-selected on Pi 5 when `python3-libgpiod` v2 is present.** Full closure
  notes in `hardware.md` 2026-07-16 late-evening subsection.

- **Victory lap correctly ABORTED (2026-07-16 evening):** autonomous attempt
  (drive at the quilt-covered door, expect `SAFE_STOP` at 0.30 m) was
  aborted before any drive — battery sag had killed left-FORWARD, and with the
  gpiozero defect still in place the safety geometry was untrustworthy. No
  contact; izzy parked clean ~1.2-1.4 m square-facing the door. The code fix
  is now in; the battery and the pull are the remaining blockers.

- **Actuation degradation at session end (prime suspect: battery sag after a
  full day):** left-motor-FORWARD dead, right-motor-REVERSE marginal-to-dead
  at duty 0.3; left-rotation channels (left-reverse + right-forward) still
  worked. Channels failed progressively in order of load. **Charge or swap
  the pack before any motor work next session**, then run a quick channel
  matrix (fwd/rev per wheel, pivot both ways) before trusting any maneuver.

- **Physical state at power-down:** izzy parked ~1.2-1.4 m square-facing the
  quilt-covered door; quilt still hung on the door; black fabric storage cube
  (sonar-invisible absorber) moved out of the drive lane; shoes + kettlebell
  near the left wall by the original lane — **clear them before pivoting**
  (they jammed right-pivots once already).

- **Environment visually confirmed:** the target door is real, closed. The
  quilt/towel stays on the glossy door for all sonar work (specular
  otherwise). Black woven-fabric storage cube is an ultrasound ABSORBER
  (invisible to sonar at any aim) — keep it out of drive lanes.

- **Code on laptop (pull required on izzy):**
  - `992e3f0` — **`GpiodUltrasonicSensor` driver** (libgpiod v2, kernel-
    timestamped): auto-selected by `make_ultrasonic_sensor()` when
    `python3-libgpiod` v2 is importable. Fixes the gpiozero 2x/4x
    range-inflation safety defect. Force with `YALP_ULTRASONIC_BACKEND=gpiod`;
    chip discovery via `YALP_GPIOCHIP`.
  - `b1e1f34` — SIGINT shutdown fix (real-stack, bounded joins, motors zeroed
    first in teardown).
  - `fde1587` — Ultrasonic observability counters (`total_reads / valid /
    raw_misses / coasted / unknown_served` in state/telemetry).
  - Earlier (already on main): `YALP_ULTRASONIC_MAX_POLL_HZ` env knob.
  - **izzy is at `0ce8a26` (main).** Pull after repo is pushed:
    `git pull --ff-only`, then `.venv/bin/pip install -e . -q`.

## THE NEXT TASK

**Root cause is fixed in code. The gpiod driver auto-selects on Pi 5 when
`python3-libgpiod` v2 is present. The remaining steps are operational.**

### Step 0 — mandatory before motor work

1. **Charge or swap the battery pack.** Left-FORWARD and right-REVERSE were
   dead or marginal at session end. Nothing motor-powered until this is done.

2. **Channel matrix:** fwd/rev per wheel, pivot both ways at duty 0.3. Confirm
   all four channels alive before any maneuver. Note any that are still weak.

3. **Clear the corridor:** shoes + kettlebell by the left wall — move them;
   they jammed right-pivots once already. Storage cube already out of lane.

---

### Step 1 — pull latest onto izzy (requires the repo to be pushed first)

Push the laptop branch to origin first, then on izzy:
```
git pull --ff-only
.venv/bin/pip install -e . -q
```
The gpiod driver (`GpiodUltrasonicSensor`) will auto-select on the next
`RealReactiveBackend` start. Confirm with `yalp hwtest --check ultrasonic` —
you should see honest distances. If readings are still ~2–4× true, the gpiozero
fallback is in use; check `python3-libgpiod` install on izzy.

---

### Step 2 — remove the 30 cm test box; validate the door reading

Remove the test box from the drive lane. Place izzy ~1.2-1.4 m square-facing
the quilt-covered door (it's still hung; leave it — specular door otherwise).
Run a live ultrasonic stream and expect STEADY ~1.2–1.4 m. That number is now
honest with the gpiod driver. **Do not drive until the door reads true.**

---

### Step 3 — victory lap (marks floor-drive rung DONE)

Stock config (NO relaxed grace). `DRIVE_GOAL straight 1.6 @ 0.3`.
Expect: valid decreasing distance track from the door, `SAFE_STOP` at ≤ 0.30 m
TRUE, goal status 'blocked', no reverse, sticky latch. **That run marks the
floor-drive rung DONE — update `roadmap.md`** (mirror the milestone-J entry
format, note gpiod driver in use).

## Key facts for whoever resumes

- **Connection:** `ssh izzy` (host `izzy.local`). Old static IP is dead. Pack
  switch = motor master cutoff; sensor-only work is safe with it off.

- **Sensor ops procedure (new, hard-won):** after ANY mount adjustment verify
  aim against a known frontal target before trusting any reading. Confident
  stable readings can be a wall behind the robot's shoulder.

- **izzy's `~/yalp` is at `0ce8a26` (main).** The two code commits from this
  session are not yet merged. Pull after merge: `git pull --ff-only`, then
  `.venv/bin/pip install -e . -q`.

- **`/tmp` artifacts on izzy** (`ultra_char.py`, drive scripts, logs) are wiped
  on reboot — recreate from repo/session artifacts if needed. Persistent
  telemetry lives at `~/.local/state/yalp/telemetry/telemetry.jsonl`.

- **Helper scripts for pivots/arcs** were staged in `izzy:/tmp` this session
  (wiped on reboot; trivial to recreate from `GpiozeroMotorDriver` — pivot
  bursts at duty 0.3 with `try/finally` stop).

- **Jam signature:** commanded pivot with zero scene change in camera frames =
  wheel obstruction or dead channel, not a software bug. Check the channel
  matrix and clear physical obstacles first.

- **Calibration and wiring — do NOT re-diagnose:** `~/.config/yalp/calibration.json`
  has `left_invert=true, right_invert=true`; DRV8833 IN/IN driver fix confirmed;
  camera `/dev/video0`; `.env` holds the rotated `ANTHROPIC_API_KEY` (mode 600,
  gitignored). `venv` ownership fixed this session (`pip install -e .` works).

- **Documented field findings (startup-blind latch semantics, timed-goal
  ~20% short calibration, stiction asymmetry forward vs backward/right-pivot):**
  see `hardware.md` 2026-07-16 subsection.

- **Settled facts unchanged:** wiring, driver fix, motor calibration, milestone H
  and J DONE (bench), grace fix verified at 15 Hz, cosmetic warnings expected
  (`cv2.CascadeClassifier` fallback, `gpiozero PWMSoftwareFallback`).

- **Safety ritual (unchanged):** wheels up on stand for motor tests; pack switch
  is the motor master cutoff; dress Pin 1 (3V3) and Pin 2 (5V) wires apart.

## The road after

Push → pull on izzy → charge pack → channel matrix → validate door reading
(honest with gpiod driver) → victory lap (floor collision-stop proven, floor-drive
rung DONE in `roadmap.md`) → `yalp see` (vision Q&A) → person-following → voice.
