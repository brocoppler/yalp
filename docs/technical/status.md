# Status — the V1 goal and where we are

> The one page that says what we are building, what works today, and what is next.
> If another doc disagrees with this one, this one wins. Last updated 2026-11-18.

---

## The V1 goal

Build our own robot as cheaply as is reasonable. For V1 it must:

1. **Drive around** without hitting things.
2. **Listen to voice commands** through the camera's built-in microphone.
3. **Reply by message** (it has no speaker), on a channel that reaches a phone.

**Fallback, if the camera microphone is not usable on the robot:** skip voice,
take text commands over the same message channel, and make it follow a person.

**Either way, the next goal after that** is a self-improving loop: the robot runs
driving tests, records what happened, and an agent uses the results to improve the
code and calibration, then tests again.

---

## What works today

| Capability | State | Proven where |
|---|---|---|
| Drive straight, turn to an angle | Heading held within about ±5°; 90° turns land at +96° / −91°. Camera is the heading sensor. | Robot, 2026-09-15 |
| Stop before obstacles | SAFE_STOP at 0.30 m from the front sonar; reverse and rotate allowed as escape moves. | Robot (stop); escape moves laptop-only |
| Drive a commanded distance | Timed guess, about 1.8× off. No encoders fitted. | Robot, 2026-07-17 |
| See and describe | `yalp see` captures a frame and Claude describes it. | Robot, 2026-07-17 |
| Voice commands | Push-to-talk only: one 5 s recording per `yalp agent --listen` run, transcribed locally. | Laptop only |
| Camera microphone | Works on the robot in a quiet room (2026-11-18): `tiny.en` transcribed counting speech from 1–2 m almost perfectly. The default `tiny` model returned nothing — set `YALP_STT_MODEL=tiny.en`. Not yet tested with the motors running. | Robot, 2026-11-18 |
| Reply by message | Not built. `src/yalp/responder.py` is the plug-in point (console and speech exist). | — |
| Follow a person | Detector runs at about 26 Hz on the Pi. `yalp follow` only drives simulated wheels. | Laptop (follow); Pi (detector speed) |
| Telemetry | Flight recorder writes JSONL; `yalp telemetry` lists, summarises, exports and plots it. | Recorder on robot; reader laptop-only |

**Not yet run on the robot:** everything from 2026-09-16 — escape moves,
`hwtest --check wheels/straight`, `calibrate --heading`, `yalp session`, the
telemetry reader, and the IMU / encoder / pack-monitor drivers (no such hardware is
fitted). The camera's field-of-view constant, which scales every heading number, has
never been checked against a real 90° mark.

**Found on the robot 2026-11-18:**

- The Pi runs hot under speech-to-text: 47 °C idle, 80–84 °C and throttled during
  transcription, and no fan is visible to the OS. Check that the case fan is spinning.
- Transcription speed is set by decoding options, not heat: the same 20 s clip took
  24–31 s with library defaults and 2.7 s with `beam_size=1` and no conditioning on
  previous text, both measured hot. `voice/stt.py` now uses the fast options.
- The Pi has no clock battery. It boots with a stale date, and `git pull` fails on a
  certificate error until the clock syncs, about a minute after Wi-Fi comes up.
- `yalp`'s transcriber crashed on the Pi (PyAV / faster-whisper version mismatch);
  fixed by decoding the WAV ourselves in `voice/stt.py`.

**Hardware as built:** Raspberry Pi 5, Logitech C270, two TT motors on a DRV8833,
one front HC-SR04, 4×AA NiMH pack, two-deck acrylic chassis. Detail in
`as-built-wiring.md` §0.

**The `shopping-list-2026-09.md` parts were NOT ordered** (decided 2026-11-18: hold
until we know they are needed). Against the V1 goal:

| Part | Needed for V1? | Needed for the self-improving loop? |
|---|---|---|
| INA219 pack monitor (~$8) | No | Yes — the one part worth buying. Without it the loop cannot tell a weak battery from a bad calibration. |
| MPU-6050 IMU (~$5) | No | Not at first. The camera heading works; buy it only if follow mode or dark rooms break the camera heading. |
| Encoder motors (~$15) | No | Not at first. A sonar-against-a-wall routine can calibrate distance. Buy them if that proves too coarse. |
| Third deck + camera mount (~$20–35) | No | Only if the clipped camera shifts between runs and biases the heading. |
| Second set of AAs + charger | No | Convenience only. |

---

## Next steps, in order

1. **One session on the robot.** Follow `next-session-handoff.md`: pull the
   September code, confirm it drives, run `yalp calibrate --heading`. Then settle the
   microphone question: `yalp audio --list`, record a clip with the motors off and
   one with them running, and transcribe both.
2. **Message channel.** Add a `Responder` that sends replies to a phone, and accept
   text commands on the same channel.
3. **Continuous listening.** Replace the single 5 s recording with a loop that keeps
   listening and hands each utterance to the agent. Skip this if step 1 shows the
   microphone is unusable; do "follow on real wheels" instead.
4. **Scored self-test.** One command that drives a fixed set of trials and writes a
   JSON score (see below). This is the foundation of the self-improving loop.

Steps 2 and 3 complete V1.

---

## What the self-improving loop needs

| Need | Have | Missing |
|---|---|---|
| Reach the robot | SSH as `izzy`, `git pull`, `yalp session restart` | Robot powered and on Wi-Fi whenever the agent runs |
| Ground truth with no human | Sonar distance to a wall; camera heading | A routine that drives at a wall and compares commanded distance to the sonar change, and checks turns against the camera. `yalp calibrate` needs a person with a tape measure today. |
| A score | Telemetry recorder and reader | A fixed trial set and a JSON result an agent can compare run to run |
| Battery awareness | Driver code for an INA219 pack monitor | The INA219 itself (about $8). Speed sags about 26% over one session; without a voltage reading the loop will tune against a dying battery. |
| Safe unattended driving | SAFE_STOP, motor watchdog | A confined lane. One front sonar is the only obstacle sensor. |

---

## Parked until V1 works

These exist in code or docs and are tested, but are not on the V1 path. Leave them
alone; do not extend them.

- EXPLORE mode and explore-and-report.
- Spoken replies and the speaker purchase.
- Three-tier model routing beyond its current defaults.
- Kid-version planning, enclosure, and everything in `roadmap.md` §6.
- `chassis-v2.md` stages beyond what the self-test needs (caster, electrical
  hardening, third deck).

---

## Where to look

| For | Read |
|---|---|
| Commands to resume on the robot, key facts, open defects | `next-session-handoff.md` |
| What happened in past sessions | `session-log.md` |
| Milestone ladder, gates, risk register | `roadmap.md` |
| Wiring as it really is | `as-built-wiring.md` |
| Voice stack reference | `audio.md` |
