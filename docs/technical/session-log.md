# Session log — what happened, newest first

> History only. For the current state and the V1 goal read `status.md`; for the
> commands to resume on the robot read `next-session-handoff.md`. Entries below are
> kept as written at the time, so some facts in older entries were later superseded
> (each entry says when).

---

## 2026-09-16 overnight build — what landed (all on `main`, tests 1008+ passing)

- **Escape moves (spec §2.3 amendment):** a rotate or a NEGATIVE straight goal is
  adopted and runs while a KNOWN obstacle is latched; a forward intent stays refused;
  a blind sensor still latches everything. `yalp drive --turn` / negative `--target`
  pass the pre-flight as an ESCAPE. She no longer strands herself at 0.25 m.
- **`yalp hwtest --check wheels` / `--check straight`:** the camera-measured per-wheel
  yaw-rate matrix (verdicts: weak/dead/wrong-sign wheel, the sign-flipping-gap
  decay-mode signature) and the straight-drive recorder (veer character, CSV).
- **`yalp calibrate --heading`:** closed-loop 90° spins L and R read against a floor
  mark → `camera_hfov_deg` in the calibration file. Every heading number scales
  with it; it has never been ground-truthed — do it.
- **`yalp session`:** start/wait/status/stop/restart/logs for the reactive server;
  systemd user unit + installer. Server output is line-buffered now.
- **Sensors (drivers + fakes + tests, hardware not yet fitted):** MPU-6050 IMU is
  the primary heading source when present (camera = fallback); FIT0450 quadrature
  encoders close the distance loop (goal `closure: "odometry"`); INA219 pack monitor
  publishes `pack_voltage_v` / `pack_state` in the new `RobotState.sensors` sub-map.
  Pin plan in `hardware.md` §5; wiring notes in the module docstrings.
- **`yalp telemetry list|summary|export|plot`:** the flight-recorder reader —
  per-goal heading/distance/motor traces as an SVG for tuning the hold.
- **Docs:** `shopping-list-2026-09.md`, `as-built-wiring.md` §0 (the two-deck body)
  and §3.6, `hardware.md` §5 additions, spec §2.3 amendment.

**Open questions (2026-09-16):** heading-hold gains (0.01 / 0.004) were tuned on one
floor in ~10 legs — re-tune with `yalp telemetry plot` once the IMU is in; the
FIT0450 16-pulse figure (x4 = 7680 ticks/rev) is from the datasheet, verify by
hand-spinning; `ENCODER_TRACK_WIDTH_M` (0.14) must be measured.

---

## 2026-09-15 session — what happened, in order

1. **Motor pack was switched off** at first (no motion, exit 0 — the drive CLI's
   open-loop "complete" proves nothing; the sonar not moving did). Switched on.
2. **She spun left instead of driving straight.** The left TT motor's leads at
   H4/H5 are swapped relative to July. Fixed in the calibration file:
   `left_invert=false` (backup `calibration.json.bak-2026-09-15`).
3. **Still veered, and the veer flipped sign with duty** (+14° left at 0.45,
   −42° right at 0.60, ~100° left in reverse). Per-wheel camera measurement
   (`as-built-wiring.md` §3.6 table) showed the DRV8833 "mixed" PWM dialect put the
   two wheels in different decay modes once only one was inverted. **Fix:**
   `config.MOTOR_DECAY_MODE="slow"` — uniform slow decay on both channels/both
   directions (DIR pins are software-PWMed too). No re-wiring.
4. **Visual heading hold** (`yalp/reactive/visual_odometry.py` + `ReactiveTickCore`):
   frame-to-frame yaw from phase correlation (left/right halves averaged to
   cancel looming), P+D correction on the straight-drive duty split, learned
   feed-forward bias persisted to the calibration file after every straight drive
   (also on SAFE_STOP / preemption). Closed-loop rotate goals with a predictive
   stop lead. `yalp drive --turn DEG` added; the timeline prints `hdg=`.
   Config knobs: `HEADING_HOLD_*`, `ROTATE_*`, `TRIM_LEARNING_*`, `CAMERA_HFOV_DEG`.
5. **Camera warm-up** (`camera.WARMUP_MAX_SECONDS` 1.5 → 3 s): the C270 publishes
   black frames for ~1 s after open; the heading estimate is blind until then.
   Give the server ~4 s after its port comes up before the first drive.

**Open design decision — SAFE_STOP has no escape.** Once the reflex latches at
<0.30 m, the tick halts *every* goal, and the drive CLI pre-flight refuses
reverse and rotate too. On a small floor she strands herself after every forward
leg and after any pivot that sweeps the sonar across furniture (happened twice
this session). Recommendation: allow `rotate` goals and NEGATIVE straight drives
while the obstacle latch is active (neither moves the nose forward; the reflex
still halts any forward motion), and have the pre-flight pass them. This changes
software-spec.md §2.3 — operator's call, not made unilaterally.

**Measured this session (slow decay, single-wheel |yaw rate| deg/s, no trims):**
duty 0.45 → L fwd 104 / R fwd 66 / L rev 111 / R rev 76; duty 0.60 → 121 / 89 /
81 / 102. Left is ~20–35% stronger forward; the heading hold + learned bias absorb it.

---

## 2026-07-17 session — first floor drive, drivetrain tuning, first `yalp see`

- **Floor-drive milestone: EARNED.** Four drives (A–D) across the session
  closed the rung:
  - **Drive A:** Timed-goal completion on a long lane — wall track confirmed
    true-range descent from 1.94 m to 1.08 m.
  - **Drive B:** Leftward-veer run (defect filed — see backlog item 2 below).
  - **Drive C:** Goal timer expired 2 cm above the SAFE_STOP threshold — no
    contact, clean exit.
  - **Drive D (earning run):** SAFE_STOP triggered at served 0.30 m, reason
    `"obstacle"`. Motors zeroed in one tick; retry refused by the sticky
    latch. This is the run that earned the milestone.

- **Sonar quality across all drives:** ~5,000 reads, 100% valid, zero misses.
  `GpiodUltrasonicSensor` (gpiod backend) confirmed in-stack — the kernel irq
  thread is visible in the process table. No gpiozero fallback, no inflation
  artifact.

- **Frame-verified motion gate is now standard practice:** one visible pivot
  burst in camera frames before trusting any motor run. Exit codes are never
  proof of motion — this gate is.

- **SIGINT shutdown verified graceful** at session end (motors zeroed, bounded
  joins, clean exit).

- **Immobility root cause (that morning):** the motor pack's red VM lead had
  been pulled out during charging. Reseated at breadboard J1 per
  `as-built-wiring.md` §3.3. The session then proceeded without further power
  issues.

- **Proven session playbook (now first-class tooling):**
  - `yalp hwtest --check motors --matrix` — 6-step per-channel triage
    (LEFT-FWD/REV, RIGHT-FWD/REV, PIVOT-LEFT/RIGHT).
  - `yalp hwtest --check ultrasonic --seconds N --hz H` — timed soak with
    valid%, miss%, min/median/max/stdev, longest-miss streak.
  - `yalp drive` — gated one-command drive (pre-flights safety, sends
    `DRIVE_GOAL`, polls live timeline, refuses genuine obstacles before
    sending intent).
  - Frame-verified motion gate before trusting any motor run.

- **Drivetrain tuned (2026-07-17 afternoon).** Motor stall confirmed at duty
  0.30 — zero displacement over a 5.4 s commanded drive (frame- and
  sonar-verified). Duty 0.45 reliably above stiction. Speed sag measured:
  0.155 m/s fresh → 0.114 m/s after ~28 runs (~26% across one session —
  recharge between long sessions). Pivots stiction-dominated at duty 0.45:
  commanded 30–60° delivers 2–15°. Reverse is slower than forward and
  pulls the nose right. Deadband code landed in commit `ebbd727` (see Key
  facts). Calibration written to `~/.config/yalp/calibration.json` (backup
  at `calibration.json.bak-pretune`): `max_speed_mps 0.29`,
  `right_trim 0.90`. Post-tune verification: commanded 1.0 m → 0.94 m
  sonar-true, veer ~7 cm/m, SAFE_STOP re-verified at served 0.28 m.

- **First `yalp see` on-robot run (2026-07-17 afternoon).** `yalp see` ran
  from `~/yalp` with the API key loaded via `load_dotenv` from `.env`; the
  C270 webcam captured a frame; the model returned an accurate scene
  description of the wall corner, baseboard, and floor Izzy faced —
  verified against the frame by an independent reviewer. Milestone C
  done-signal satisfied; PASSED 2026-07-17 in the roadmap.
