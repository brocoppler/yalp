# Next-session handoff — resume here

**Last updated:** 2026-07-16, late evening / overnight (floor-graze diagnosed,
overnight tooling landed).
**One-line status:** Root cause fixed, floor-graze at parking spot diagnosed,
overnight tooling committed (hwtest soak+matrix+save, `yalp drive` subcommand,
`startup_blind` latch semantics). Morning sequence: charge pack, pull repo, run
channel matrix, soak sensor, then fire the one-command victory lap.

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
  the pack before any motor work next session**, then run the channel matrix
  before trusting any maneuver.

- **Physical state at power-down (tape-measured):** izzy cleanly shut down,
  parked **118 cm** square-ish to the quilt-covered door. Quilt stays up
  (specular door without it). The ~112 cm sonar mode at shutdown matched the
  tape — quilt hangs a few cm proud of the door. A transient ~88 cm mode
  during final captures was the operator walking through the beam; expect and
  ignore human transients in future captures.

- **Floor-graze diagnosis (~75% confidence):** the dominant ~65 cm sonar mode
  at the parking spot is most likely a **floor graze** — the flat-remounted
  module leans forward a few degrees with the cardboard wall, putting the
  lower beam skirt on the hardwood ~0.65 m out. Evidence: persists across
  scene changes; 4× wider jitter than hard-surface modes; geometry closes for
  a 0.12–0.18 m mount height. **Recommended permanent fix:** shim the
  module's BOTTOM edge up 1–2 degrees (pushes any graze past 3 m).
  **Quick discriminator:** lay a cloth flat on the floor 0.4–0.8 m ahead —
  a graze mode dies, an object mode survives. **The lap is GO-able even
  unshimmed:** the graze reads a constant safe ~65 cm until the door is truly
  nearer, then tracks it down to the 0.30 m trip.

- **Everything in `izzy:/tmp` died with the shutdown.** The validated gpiod
  driver is now productized in the repo — no `/tmp` scripts need recreating.

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
  - `2504b0c` — **`startup_blind` latch semantics:** cold-boot SAFE_STOP when
    no valid read has ever landed is now labeled `reason="startup_blind"` (vs
    `"echo_timeout"` for mid-run dropout). `yalp drive` pre-flight drives
    through both (adoption lifts the latch); only a genuine close `"obstacle"`
    causes refusal (exit 2, no intent sent).
  - `71829b4` — **`yalp hwtest` upgrades:** `--seconds S`/`--hz H` ultrasonic
    soak (prints count, valid%, miss%, min/median/max/stdev, longest-miss
    streak); `--matrix` 6-step per-channel motor triage (LEFT-FWD/REV,
    RIGHT-FWD/REV, PIVOT-LEFT/RIGHT); `--save PATH` camera frame save.
  - `1925ad1` — **`yalp drive` subcommand** (first-class IPC drive, replaces
    all hand-rolled `/tmp` scripts). See morning sequence below.
  - Earlier (already on main): `YALP_ULTRASONIC_MAX_POLL_HZ` env knob.
  - **izzy is at `0ce8a26` (main).** Pull after repo is pushed:
    `git pull --ff-only`, then `.venv/bin/pip install -e . -q`.

## MORNING SEQUENCE

**Root cause fixed. Overnight tooling committed. Steps below are the complete
morning run-card in order.**

### Step 1 — charged pack in, power on

Nothing motor-powered until the pack is fresh. Left-FORWARD and right-REVERSE
were dead or marginal at session end (battery sag). **Charge or swap before
continuing.**

---

### Step 2 — push repo, pull on izzy

**PUSH REQUIRED.** The repo's `main` is many commits ahead of origin (gpiod
driver, docs, overnight tooling). The operator must authorize the push, then
on izzy:
```
cd ~/yalp && git pull --ff-only && .venv/bin/pip install -e . -q
```

---

### Step 3 — channel matrix (wheels up)

New `--matrix` flag runs a 6-step per-channel triage (LEFT-FWD/REV,
RIGHT-FWD/REV, PIVOT-LEFT/RIGHT). **Put izzy on the stand — wheels off the
ground** before running:
```
yalp hwtest --check motors --matrix
```
Confirm all channels alive and note any weak ones before trusting any maneuver.

---

### Step 4 — ultrasonic soak

New `--seconds`/`--hz` flags run a timed soak with a summary (valid%, miss%,
min/median/max, longest-miss streak). Place izzy facing the quilt-covered
door at ~1.14 m (tape mark). Run:
```
yalp hwtest --check ultrasonic --seconds 20 --hz 10
```
**Expect:** primary mode at **~1.12–1.14 m** (quilt a few cm proud of door
tape); a benign secondary **~65 cm floor-graze mode** (see floor-graze note
above — harmless unless shimmed). Also verify: a rigid box at 30 cm must
read **~29–31 cm**. If readings are still ~2–4× true, the gpiozero fallback
is in use; check `python3-libgpiod` v2 on izzy.

---

### Step 5 — THE VICTORY LAP (one command)

Stock config (no relaxed grace). Remove the test box from the lane first.
```
yalp drive
```
Default flags: `--target 1.6` (m), `--speed 0.3`. The command:
1. Connects to the running reactive process via IPC.
2. Pre-flights safety: refuses a genuine close `"obstacle"` (exit 2, no
   intent sent); drives through a stale cold-boot latch (`"startup_blind"` or
   `"echo_timeout"`, distance unknown) — adoption lifts it automatically.
3. Sends `DRIVE_GOAL straight 1.6 @ 0.3`, polls at 5 Hz, prints a live
   timeline row each poll (elapsed, distance, mode, status, progress/reason).
4. **Expect:** decreasing distance track from quilt door, `SAFE_STOP` at a
   true ≤ 0.30 m, reason `"obstacle"`, stop verdict printed, **exit 0**.
   On timeout (45 s default): sends IDLE halt, exit 1.
   Ctrl-C: IDLE halt, exit 130.

**Note on `startup_blind`:** if the reactive process just booted and the first
poll shows `reason="startup_blind"`, that is the cold-boot latch (no valid
read yet). The CLI drives through it — it is safe and expected; adoption
lifts it once the sensor delivers a valid reading. A genuine `"obstacle"`
refusal prints why and exits 2 before any intent is sent.

---

### Step 6 — on success: mark floor-drive DONE

Update `roadmap.md` — mirror the milestone-J format, mark the floor-drive
rung DONE (note: gpiod driver, `yalp drive` command, `startup_blind`
semantics, date 2026-07-17). Then the road opens:
**`yalp see` → person-following → voice.**

## Key facts for whoever resumes

- **Connection:** `ssh izzy` (host `izzy.local`). Old static IP is dead. Pack
  switch = motor master cutoff; sensor-only work is safe with it off.

- **Sensor ops procedure (new, hard-won):** after ANY mount adjustment verify
  aim against a known frontal target before trusting any reading. Confident
  stable readings can be a wall behind the robot's shoulder.

- **izzy's `~/yalp` is at `0ce8a26` (main).** Overnight commits (gpiod
  driver, hwtest upgrades, `yalp drive`, `startup_blind`) are ahead of origin
  and not yet on izzy. Pull after the push: `git pull --ff-only`, then
  `.venv/bin/pip install -e . -q`.

- **`/tmp` artifacts on izzy died with the shutdown.** No `/tmp` scripts
  need recreating — everything is now in the repo (`yalp drive`, `yalp hwtest
  --matrix`, etc.). Persistent telemetry lives at
  `~/.local/state/yalp/telemetry/telemetry.jsonl`.

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

Push → pull on izzy → charge pack → `yalp hwtest --check motors --matrix`
(channel triage, wheels up) → `yalp hwtest --check ultrasonic --seconds 20 --hz 10`
(soak, confirm door + floor-graze) → `yalp drive` (victory lap, floor
collision-stop proven) → mark floor-drive rung DONE in `roadmap.md` →
`yalp see` (vision Q&A) → person-following → voice.
