# Next-session handoff — resume here

**Last updated:** 2026-11-18 (trimmed; history moved to `session-log.md`).
**Goal and current state:** `status.md`.
**One-line status:** Izzy drives straight and turns to a commanded angle using the
camera as her heading sensor. She was powered down on 2026-09-15 and has NOT run the
2026-09-16 code.

---

## First thing next session

```
ssh izzy
cd ~/yalp && git pull --ff-only && .venv/bin/pip install -e . -q && .venv/bin/pip install smbus2 -q
bash scripts/install_reactive_service.sh        # optional: systemd user unit + ~/.config/yalp/reactive.env
.venv/bin/yalp session start                    # or: systemctl --user start yalp-reactive
.venv/bin/yalp session wait --camera            # READY when the port answers AND the camera is warm
.venv/bin/yalp drive --target 0.6               # the timeline shows hdg=[camera]
.venv/bin/yalp drive --turn 90                  # closed-loop turn; allowed inside the 0.30 m latch (escape move)
.venv/bin/yalp hwtest --check wheels            # per-wheel yaw-rate matrix
.venv/bin/yalp hwtest --check straight --seconds 3 --reverse-first 2 --save /tmp/s.csv
.venv/bin/yalp calibrate --heading              # ground-truth the camera HFOV against a floor mark (do this once)
.venv/bin/yalp telemetry summary                # per-goal summary of the newest flight-recorder file
```

Then the microphone check (decides the V1 path — see `status.md`):

```
.venv/bin/pip install -e ".[voice]" -q && sudo apt-get install -y libportaudio2
.venv/bin/yalp audio --list                     # is the C270 mic listed?
```

Record one clip with the motors off and one with them running, and transcribe both.

If sensors from `shopping-list-2026-09.md` are fitted: `yalp hwtest --check imu`,
`--check power`, `--check encoders`. Everything auto-detects at server start
(`YALP_IMU` / `YALP_POWER_MONITOR` / `YALP_ENCODERS` = auto|1|0).

---

## Open questions

- Heading-hold gains (0.01 / 0.004) were tuned on one floor in about 10 legs. Re-tune
  with `yalp telemetry plot`.
- The FIT0450 16-pulse figure (x4 = 7680 ticks/rev) is from the datasheet; verify by
  hand-spinning once encoders are fitted.
- `ENCODER_TRACK_WIDTH_M` (0.14) must be measured.
- `camera_hfov_deg` has never been ground-truthed.

---

## Defect backlog

1. **Open-loop timed distance over-reports about 1.8×** (about 0.9 m actual on a
   1.6 m goal). OPEN. Fix is encoders or a sonar-based speed calibration.
   Side-effect: the goal timer can fire before the reflex on long approaches.
2. **Veer under way.** CLOSED 2026-09-15 by the slow-decay driver mode and the visual
   heading hold (`as-built-wiring.md` §3.6).
3. **Floor-graze at about 0.6 m.** OPEN. The sonar module leans forward a few
   degrees. Shim its bottom edge up 1–2°. Benign meanwhile: it can only stop her early.
4. **Battery sag.** OPEN. Speed droops about 26% over about 28 runs in one session
   (0.155 → 0.114 m/s at duty 0.45). Recharge between long sessions; an INA219 would
   make it measurable.
5. **Rotation model.** CLOSED 2026-09-15 by closed-loop rotate goals. The open-loop
   `turn_rate_dps` figure is only a fallback now.
6. **Retinue-side (not this repo):** tasks that depend on archived task IDs stall.
   Workaround: clear `depends_on` on the stalled task.

---

## Key facts for whoever resumes

- **Connection:** `ssh izzy` / `izzy.local`. The old static IP is dead. Pack switch =
  motor master cutoff; sensor-only work is safe with it off.

- **Motor power path — do not re-diagnose:**
  `red = VM = breadboard J1`, `black = J3`, caps' + legs on the VM column.
  See `as-built-wiring.md` §3.3 before touching anything in J1.

- **Calibration state (2026-09-15):** `~/.config/yalp/calibration.json` on the Pi has
  `left_invert=false, right_invert=true, left_trim=1.0, right_trim=1.0,
  max_speed_mps=0.29, turn_rate_dps=120` plus the LEARNED `straight_bias_fwd` /
  `straight_bias_rev` (≈ +0.01; rewritten by the reactive layer after straight
  drives). Backups: `calibration.json.bak-pretune` (July) and
  `calibration.json.bak-2026-09-15`. The July `right_trim 0.90` was tuned for the old
  mixed decay mode and is WRONG under slow decay. The `duty_deadband` key is not in
  the live file; `from_dict` defaults a missing key to 0.0, which preserves the
  measured linear behavior.

- **Drive settings:** motors stall at duty 0.30; 0.45 is reliably above stiction and
  is the `yalp drive` default. `config.MOTOR_DECAY_MODE="slow"`.

- **Camera warm-up:** the C270 publishes black frames for about 1 s after open. Give
  the server about 4 s after its port comes up before the first drive
  (`yalp session wait --camera` does this).

- **Settled facts — do NOT re-diagnose:** DRV8833 IN/IN driver fix confirmed; camera
  is `/dev/video0`; `.env` in `~/yalp` holds `ANTHROPIC_API_KEY` (mode 600,
  gitignored); `GpiodUltrasonicSensor` is auto-selected when `python3-libgpiod` v2 is
  present (force with `YALP_ULTRASONIC_BACKEND=gpiod`; chip via `YALP_GPIOCHIP`).

- **Motion is proven by frames, not exit codes.** One visible pivot burst in camera
  frames before trusting any motor run. A commanded pivot with zero scene change means
  a wheel obstruction, a dead channel, or the pack switched off — not a software bug.
  Run `yalp hwtest --check motors --matrix`.

- **Cosmetic warnings expected:** `cv2.CascadeClassifier` fallback and
  `gpiozero PWMSoftwareFallback`. Both harmless.

- **Safety ritual:** wheels up on a stand for motor tests; pack switch is the motor
  master cutoff; dress Pin 1 (3V3) and Pin 2 (5V) wires apart.
