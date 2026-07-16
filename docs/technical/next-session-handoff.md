# Next-session handoff — resume here

**Last updated:** 2026-07-16, end of the on-hardware session.
**One-line status:** First supervised floor drive COMPLETED (timed goal, human
backstop); collision-stop reflex on the floor still UNPROVEN — sonar discovered
pointing ~90-140° CCW off the nose. Physical quarter-turn clockwise bracket fix
+ verification is THE next task, then the "victory lap" drive where she stops
herself at the door.

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

- **Sonar saga resolved: mount is ~90-140° CCW off the nose (viewed from
  above).** After a hand "leveling" of the mount an autonomous camera+sonar
  diagnosis (pivot sweeps cross-referenced with video frames) proved the
  transducers point into the left-rear quadrant. The persistent 2.3-2.7 m
  "background band" everyone kept seeing was the left-rear wall, not a
  frontal target. Full field findings — including the visual cross-reference
  method — are in `hardware.md` 2026-07-16 subsection.

- **Environment visually confirmed:** the target door is real, closed, ~1 m
  ahead. A black woven-fabric storage cube in the corridor is an ultrasound
  ABSORBER (invisible to sonar at any aim angle) and must be moved out of
  drive lanes before the victory lap.

- **Code landed this session — 2 commits ahead of `origin/main`, not yet
  merged:**
  - `b1e1f34` — SIGINT shutdown fix: the real-stack previously survived
    SIGINT during timeout storms (needed SIGTERM); motors now zeroed first in
    teardown, bounded joins, regression tests added.
  - `fde1587` — Ultrasonic observability counters: state/telemetry now carry
    `total_reads / valid / raw_misses / coasted / unknown_served`. Eliminates
    the lower-bound-only field analysis problem.
  - Earlier (already on main): `YALP_ULTRASONIC_MAX_POLL_HZ` env knob and
    inert-grace construction warning.
  - **izzy is at `0ce8a26` (main).** Pull needed after these commits merge:
    `git pull --ff-only`, then `.venv/bin/pip install -e . -q`.

## THE NEXT TASK

**In order — nothing drives until step 2 reads true.**

1. **Physical fix:** rotate the sensor bracket ~quarter turn CLOCKWISE (viewed
   from above), pitch level, until the transducers visibly face the same
   direction as the camera lens. Pack switch OFF for sensor-only work.

2. **Verify BOTH axes:** with izzy ~1 m square to the closed door, run
   `/tmp/ultra_char.py --hz 15 --seconds 8` (recreate from repo/session
   artifacts if `/tmp` was wiped). Expect a STEADY ~95-105 cm. If reading ~2 m,
   iterate yaw in 10-15° steps, using the steady-pass as the feedback signal.
   **Do not drive until this reads true.**

3. **Clear the corridor:** move the fabric storage cube out of all drive lanes
   (it is an ultrasound absorber — sonar-invisible regardless of aim). Re-square
   izzy at ~1 m standoff from the door.

4. **VICTORY LAP:** stock config (NO relaxed grace),
   `DRIVE_GOAL` straight ~1.5 @ 0.3. Expect: valid decreasing distance track
   from the door, a distance-triggered `SAFE_STOP` at 0.30 m (≈ true range
   now), `BLOCKED`, no reverse, sticky latch. That run marks the floor-drive
   rung DONE — update `roadmap.md` (mirror the milestone-J entry format).

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

Sonar bracket fix → victory lap (floor collision-stop proven, floor-drive rung
DONE) → `yalp see` (vision Q&A; the camera+vision loop already proved itself
diagnosing the sonar) → person-following → voice.
