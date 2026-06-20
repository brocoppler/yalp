# Software Spec

> The detailed software design for yalp: the two-layer runtime, the contract between the fast on-Pi reactive loop and the slow cloud deliberative loop, person-tracking, degradation, and config. Build against this.

---

`architecture.md` makes the case for *why* yalp runs on two loops; this doc specifies *what* the two loops actually exchange and how each is built. If you only read one section, read §2 — the loop-to-loop contract is the spine of the system, and everything else hangs off it. Siblings: `architecture.md` (the framing), `hardware.md` (the body this code drives), `roadmap.md` (the build order and the gates that this doc declares).

## 1. Software Stack & Where Each Piece Runs

The single organizing principle: **only motor control and camera capture genuinely need the Pi.** Everything else — the agent loop, prompt building, the model calls — is plain Python you develop on your laptop with its webcam standing in for the robot's eyes, then marry to the body at the end. The reactive layer is the *only* code that must run on the Pi at robot-speed.

| Layer | Component | Library / Service | Runs on | Notes |
|---|---|---|---|---|
| Reactive | Motor drive (differential) | `gpiozero` (`Motor`, `Robot`) on the **lgpio/native pin factory** | **Pi only** | PWM to the DRV8833/TB6612. On Pi 5, `gpiozero` must use the lgpio/native pin factory — `RPi.GPIO` does not work on Pi 5; see `hardware.md` for pinout and the pin-factory setup. |
| Reactive | Obstacle sensing | `gpiozero` (`DistanceSensor`) | **Pi only** | HC-SR04 ultrasonic; powers collision-stop. The echo is **software-timed and jittery** — an echo timeout is "unknown" (bias to STOP), never "clear"; don't poll faster than ~15 Hz (~60 ms cycle). Reflected in the tick's `distance_known` (§2.3); detail in `hardware.md`. |
| Reactive | Lightweight tracker | `opencv-python` | Pi (prod) / laptop (dev) | Centering + follow-mode. See §4 — this is the contested piece. |
| Reactive | Camera capture | `picamera2` (Pi cam) **or** OpenCV UVC capture (USB webcam) | Pi (prod) / laptop (dev) | v1 ships the C270 USB/UVC path; its blocking `read()` runs in a dedicated capture thread (§2.5). `picamera2` is a later swap if we move to the Pi Camera Module. |
| Deliberative | Cloud VLM/LLM agent | `anthropic` SDK (primary) / `openai` SDK (alt) | Laptop (dev), Pi or laptop (later) | Tool-use / vision; tiered models — see §3. |
| Deliberative | Prompt + dispatch glue | Plain Python (`stdlib` + SDK) | Anywhere | The "brain" you iterate on the laptop. |
| Voice *(deferred)* | Speech-to-text / text-to-speech | STT/TTS lib or cloud service (TBD) | Pi + cloud | Separate track; see §7 and `roadmap.md`. |
| OS | Base image | **Raspberry Pi OS Lite (64-bit)** | Pi | Headless. Develop over SSH/WiFi; enable SSH, camera, I2C at flash time. No desktop. |

> **DECISION —** The reactive layer and the deliberative layer are **two separate OS processes**, always — never threads in one interpreter, never a shared in-process queue. They talk over a **localhost socket** (TCP `127.0.0.1` in dev; a Unix-domain socket once co-located on the Pi), framed as **JSON lines — one JSON object per line, `\n`-terminated** (see §2.2 for the wire format). The reactive process **owns GPIO and the camera** and runs whether or not the cloud is reachable; it **publishes** the latest frame and `RobotState` non-blocking. The deliberative process (laptop in dev) **never reaches into the reactive tick** — it cannot make the reactive tick block on a network read or a camera read. There is **no shared-dataclass model**: `RobotState` and `Intent` are *serialized* across the socket as JSON and reconstructed on the far side; the two processes never share a live Python object. This supersedes any earlier "shared in-process queue once co-located" framing — co-location swaps TCP for a Unix socket, nothing more.

> **THESIS —** Build the deliberative loop on the laptop first with the laptop webcam as the camera, stub the reactive layer with a fake that just prints `drive(...)`/`set_mode(...)`, and only swap in the real Pi reactive process once the agent loop is fun to use. Debugging Python, Linux, *and* hardware simultaneously is the main way hobby builds stall.

## 2. The Crux — The Loop-to-Loop Interface Contract

The model never twitches a motor. It picks **intents**, expressed as **tool calls** against a fixed ability menu. Each tool call sets (or adjusts) a **mode + goal** on the reactive layer, or asks a question the reactive layer can answer from its latest state. The reactive layer executes the mode at 10–30 Hz, keeps the robot safe, and publishes a small **state object** back up. That is the entire contract.

### 2.1 The ability menu (what the LLM can call)

| Tool | Parameters | Effect (one line) |
|---|---|---|
| `drive` | `distance_m: float` (signed; −=back), `speed: 0..1` | Enter `DRIVE_GOAL`: drive straight until the **timed** distance estimate elapses, blocked, or preempted. Reports `"completed (timed, unverified)"` — there are no encoders. |
| `turn` | `angle_deg: float` (signed; +=left/CCW), `speed: 0..1` | Enter `DRIVE_GOAL` (rotate variant): turn in place for the **timed** angle estimate. Reports `"completed (timed, unverified)"` — no encoders, no IMU. |
| `stop` | — | Enter `IDLE`: cancel current goal, wheels halt. |
| `look` / `capture` | `save: bool=false` | Grab one still from the camera; return a frame handle for the next model turn. |
| `check_distance` | — | Return the latest ultrasonic reading (meters) from shared state. |
| `describe_scene` | `detail: "quick"\|"full"` | Capture a still and (deliberative side) escalate to a model tier for a scene description. |
| `enter_follow_mode` | `target: "nearest_person"` | Enter `FOLLOW`: local tracker centers + approaches the target. No cloud in the loop. |
| `explore` | `goal_text: str` | **Deliberative sugar** (v1): the deliberative layer runs a loop of `drive`/`turn`/`describe_scene` itself. There is **no** `EXPLORE` reactive mode — see §2.2 and the EXPLORE decision below. |
| `speak` | `text: str` | Say/print `text` (TTS later; `print` for v1). |
| `set_speed_limit` | `max_speed: 0..1` | Clamp all subsequent motion (safety / "go slow"). |

> **DECISION —** This menu is the stable contract. Abilities are *mostly modes for the reactive layer*, not direct actuation — `enter_follow_mode` hands the job to the local tracker; the model does not stream steering. Adding a new ability = adding a row here + a mode/handler below. The menu is small on purpose: every tool must map to something the body can reliably do (see the in-scope envelope in `architecture.md`).

Illustrative tool schema as handed to the model (Anthropic tool-use shape; the OpenAI function-calling shape is a mechanical translation):

```json
[
  {
    "name": "drive",
    "description": "Drive straight a signed distance in meters (negative = backward). Collision-stop overrides this at all times.",
    "input_schema": {
      "type": "object",
      "properties": {
        "distance_m": { "type": "number", "description": "Signed meters; negative drives backward." },
        "speed":      { "type": "number", "minimum": 0, "maximum": 1, "default": 0.5 }
      },
      "required": ["distance_m"]
    }
  },
  {
    "name": "enter_follow_mode",
    "description": "Hand control to the on-Pi tracker to center on and approach a target. Returns immediately; the robot follows without further model calls until told to stop.",
    "input_schema": {
      "type": "object",
      "properties": {
        "target": { "type": "string", "enum": ["nearest_person"], "default": "nearest_person" }
      }
    }
  },
  {
    "name": "describe_scene",
    "description": "Capture a still and describe what is visible. Use detail='full' only when the user explicitly wants a rich description or to read text.",
    "input_schema": {
      "type": "object",
      "properties": { "detail": { "type": "string", "enum": ["quick", "full"], "default": "quick" } }
    }
  }
]
```

> **DECISION —** `explore` is **deliberative sugar** for v1, not a reactive mode. The deliberative layer implements it as a loop of `drive`/`turn`/`describe_scene` — each leg is an ordinary `Intent`, and the model re-decides after every `describe_scene`. `EXPLORE` is therefore **removed from the reactive mode enum and the reactive tick loop** (see §2.2, §2.3); the reactive layer stays dumb and carries one fewer mode. The cost is one extra round-trip per leg, which §3's latency budget already bounds (an explore step must stay under the ~10 s ceiling on real WiFi). We do **not** carry both interpretations forward.

### 2.2 The intent / mode model

The reactive layer is a small state machine. At any instant it is in exactly one **mode**, each carrying a typed **goal payload**:

| Mode | Goal payload | Reactive behavior |
|---|---|---|
| `IDLE` | — | Wheels stopped. Sensors still read. Default and safe resting state. |
| `DRIVE_GOAL` | `{kind: "straight"\|"rotate", target, speed, progress, elapsed_s}` | Execute until the **timed** target estimate elapses, blocked, or preempted. Completion is reported as `"completed (timed, unverified)"` with `elapsed_s` (see §2.3). |
| `FOLLOW` | `{target, last_seen_bbox, last_seen_ts}` | Tracker centers + approaches; coasts/searches briefly if target lost. This is the **only** mode that visually corrects distance/heading. |
| `SAFE_STOP` | `{reason, distance}` | Hard stop, motion latched off, until obstacle clears AND a new intent arrives. |

There is no `EXPLORE` mode — exploration is a deliberative loop of the modes above (see the EXPLORE decision in §2.1).

The two loops communicate only through one **state object**, owned and written by the reactive layer and *published* (serialized to JSON, §2.2 wire format) to the deliberative layer; the deliberative layer writes *intents* (never reactive state) by serializing an `Intent` back over the same socket:

```python
# Written by reactive loop every tick; serialized to the deliberative loop on demand.
@dataclass
class RobotState:
    mode: str                 # "IDLE" | "DRIVE_GOAL" | "FOLLOW" | "SAFE_STOP"  (no EXPLORE)
    goal: dict | None         # the active mode's payload (see table)
    goal_status: str          # "running" | "completed (timed, unverified)" | "blocked" | "preempted" | "none"
    goal_elapsed_s: float     # wall-clock seconds the current/last goal has run (the honest stand-in for odometry)
    distance_m: float         # latest ultrasonic reading; see staleness note below
    distance_known: bool      # False when the HC-SR04 echo timed out — treat as "unknown", bias to STOP
    obstacle: bool            # distance_known AND distance_m < SAFE_STOP_THRESHOLD
    target_visible: bool      # tracker currently sees the follow target
    target_bbox: tuple | None # (x, y, w, h) in frame px, or None
    tracker_score: float      # tracker's own confidence for the current box, 0..1
    ticks_since_last_detector_confirmation: int  # ticks since the person-detector last re-seeded/validated the box
    last_frame_id: str | None # handle to most recent decoded frame (the VLM-quality still buffer)
    speed_limit: float        # current clamp, 0..1
    ts: float                 # monotonic timestamp of this snapshot

# Written by deliberative loop; consumed by reactive loop at the top of each tick.
@dataclass
class Intent:
    mode: str                 # requested mode: "IDLE" | "DRIVE_GOAL" | "FOLLOW" | "SAFE_STOP"
    goal: dict | None         # requested goal payload
    seq: int                  # monotonically increasing; newer seq wins
```

> **DECISION —** `RobotState` and `Intent` are the *only* two shapes crossing the boundary, and they cross as **JSON, not shared objects**. The reactive layer publishes `RobotState` every tick (last-write-wins; no history). The deliberative layer pushes an `Intent` with a monotonically increasing `seq`. The reactive layer keeps a **single-slot intent mailbox, last-write-wins**: it stores only the highest `seq` it has seen and overwrites on arrival — there is **no queue and no buffer**. This makes the channel stateless and drop-tolerant: a lost `RobotState` is replaced by the next tick; a dropped `Intent` is simply re-issued by the next deliberative step (the model re-decides from the latest `RobotState` regardless).

> **DECISION —** Tracker honesty is part of the contract. `tracker_score` and `ticks_since_last_detector_confirmation` are published every tick so `FOLLOW` (and the model narrating it) can **choose to coast vs stop** instead of driving blindly on a stale box. Without these, a drifting cheap tracker will "confidently drive the robot toward the couch" after it has actually lost the person; with them, `step_follow` coasts briefly then stops and reports lost. See §4.

**The wire format (build against this today).** One TCP/Unix socket, `127.0.0.1` in dev. Each direction is a stream of **UTF-8 JSON objects, one per line, `\n`-terminated** (newline-delimited JSON — "JSON lines"). A reader accumulates bytes and splits on `\n`; each complete line is one `json.loads()`. The dataclass↔JSON mapping is field-for-field — dataclass field name → JSON key, with a `"type"` discriminator so a reader can tell the two shapes apart:

```jsonc
// reactive -> deliberative, one line, published every tick (last-write-wins)
{"type":"state","mode":"DRIVE_GOAL","goal":{"kind":"straight","target":0.5,"speed":0.5,"progress":0.4,"elapsed_s":1.2},
 "goal_status":"running","goal_elapsed_s":1.2,"distance_m":0.8,"distance_known":true,"obstacle":false,
 "target_visible":false,"target_bbox":null,"tracker_score":0.0,"ticks_since_last_detector_confirmation":0,
 "last_frame_id":"f-10231","speed_limit":1.0,"ts":1287.44}

// deliberative -> reactive, one line, on each new decision
{"type":"intent","mode":"FOLLOW","goal":{"target":"nearest_person"},"seq":42}
```

> **DECISION —** **Who polls whom:** the deliberative layer is the active party for *reads*. The reactive layer publishes a fresh `state` line every tick into a single-slot send buffer; if the deliberative reader is slow, **older unread `state` lines are dropped, newest wins** (it is a snapshot, not a log). The deliberative layer reads the latest `state` line when it builds a prompt or polls a goal (pull model — §2.4), and *writes* an `intent` line whenever the model issues one. The reactive layer's socket read is **non-blocking and off the tick's critical path** (it drains pending `intent` lines into the single-slot mailbox at the top of the tick and never waits on the socket).

> **DECISION —** **Reconnect:** the socket is treated as disposable. If it drops, the **reactive layer keeps running unchanged** (it just has no one to publish to and adopts no new intents — identical to the WiFi-loss behavior in §5). The deliberative layer **reconnects with backoff** and, on reconnect, simply resumes reading the latest `state` and issuing `intent`s with ever-increasing `seq`. Because both shapes are last-write-wins snapshots, **no replay or buffering is needed** across a reconnect — there is no missed-message recovery to implement. `seq` is monotonic across reconnects (it does not reset), so a late-arriving stale `intent` from a half-open old socket can never overwrite a newer one.

### 2.3 Preemption semantics

There is a strict priority order. Higher always wins, immediately:

1. **Collision-stop (SAFE_STOP)** — a reflex, *never* gated behind the AI. If `distance_m < SAFE_STOP_THRESHOLD`, the wheels stop this tick, full stop, regardless of mode or any in-flight intent.
2. **Newest intent** — a new `Intent` (higher `seq`) replaces whatever mode/goal is in progress. "Follow me" mid-drive abandons the drive and enters `FOLLOW`. No queueing, no "finish the current move first."
3. **Current mode execution** — only if neither of the above fired does the active mode get to advance its goal this tick.

> **DECISION —** New intents *replace*, they do not *queue* — consistent with the single-slot, last-write-wins mailbox (§2.2). The robot always reflects the operator's most recent instruction. If the user wants a sequence, the deliberative layer issues it step by step (waiting for a terminal `goal_status` — `"completed (timed, unverified)"` or `"blocked"` — between steps), rather than the reactive layer buffering a plan.

> **DECISION —** `SAFE_STOP` is sticky. Clearing the obstacle is necessary but not sufficient to resume — the robot waits in `SAFE_STOP` until the obstacle clears **and** a fresh intent arrives. This prevents a robot that's been nosed into a wall from lurching forward the instant someone moves out of the way.

> **DECISION —** **Post-collision behavior (v1): HALT and surface `BLOCKED`, no open-loop reverse.** When collision-stop fires, the reactive layer stops the wheels, enters `SAFE_STOP` with `goal_status = "blocked"`, and publishes that upward so the deliberative layer can re-plan. It does **not** auto-reverse: there is **no rear sensor**, so backing up open-loop would be driving blind into whatever is behind the robot. Recovery is the deliberative layer's job — it sees `blocked`, and may issue a *new* intent (e.g. a turn, or a short reverse the operator explicitly asks for) once it has reasoned about the scene. This resolves the open question of what to do after a collision: stop and report, never blind-reverse.

> **DECISION —** **Distances and angles are timed guesses, not measurements.** With no encoders and no IMU, `drive`/`turn` complete by elapsed time, so their goal reports `"completed (timed, unverified)"` with `goal_elapsed_s` — never a bare `"completed"`. `build_context` (§3) must tell the model that distances and angles are **approximate and uncorrected outside `FOLLOW`**, so the model treats "drove 0.5 m" as "drove roughly 0.5 m" and leans on `describe_scene`/`check_distance` rather than dead-reckoning a multi-step path.

The reactive loop tick, in full:

```python
def reactive_tick(state, intent_mailbox, motors, sensor):
    # 1. READ SENSORS (non-blocking; the HC-SR04 echo is software-timed and jittery)
    dist, known = sensor.read()     # known=False when the echo timed out
    state.distance_m, state.distance_known = dist, known
    # An echo timeout is "unknown", NOT "clear" — bias to STOP. Respect the sensor's
    # ~60 ms cycle: the capture cadence never polls faster than ~15 Hz (see hardware.md).
    state.obstacle = (not known) or (dist < SAFE_STOP_THRESHOLD)

    # 2. SAFETY OVERRIDE — beats everything, every tick
    if state.obstacle:
        motors.stop()                          # HALT — never open-loop reverse (no rear sensor)
        state.mode, state.goal_status = "SAFE_STOP", "blocked"   # surface BLOCKED upward
        state.goal = {"reason": "obstacle" if known else "echo_timeout", "distance": dist}
        publish(state)
        return                      # nothing else runs this tick

    # 3. DRAIN SOCKET INTO SINGLE-SLOT MAILBOX, then adopt (preempt in-progress mode).
    #    The mailbox stores ONLY the highest seq seen (last-write-wins) — no queue.
    intent_mailbox.drain_socket_nonblocking()  # overwrite slot with any newer-seq intent
    new = intent_mailbox.take()                # the single stored intent, or None
    if new is not None:
        # Leaving SAFE_STOP requires both: obstacle cleared (checked in step 2) AND this fresh intent.
        state.mode, state.goal = new.mode, new.goal
        state.goal_status = "running"
        state.goal_elapsed_s = 0.0

    # 4. EXECUTE CURRENT MODE  (no EXPLORE branch — exploration is a deliberative loop)
    if state.mode == "IDLE" or state.mode == "SAFE_STOP":
        motors.stop()
    elif state.mode == "DRIVE_GOAL":
        done = step_drive_goal(state, motors)          # advances progress by ELAPSED TIME (no encoders)
        state.goal_elapsed_s += DT
        if done:
            state.goal_status = "completed (timed, unverified)"   # never a bare "completed"
            state.mode = "IDLE"
        else:
            state.goal_status = "running"
    elif state.mode == "FOLLOW":
        step_follow(state, motors)                     # tracker centers + approaches; honesty signals published

    # 5. PUBLISH STATE upward (serialized to one JSON line; newest wins, no history)
    publish(state)
```

Every branch in step 4 clamps commanded speed to `state.speed_limit` before touching `motors`. The loop is designed to never block: the socket drain (step 3), sensor reads, tracker steps, and motor writes are all non-blocking, so the tick rate stays in the 10–30 Hz band even when the cloud is mid-think. The reactive tick's safety guarantee is only as good as its worst-case latency under *real* load — see the combined-load gate in §4 and `roadmap.md`.

### 2.4 How the reactive layer reports up

The deliberative layer reads `RobotState` whenever it builds a prompt or polls a goal. The fields that matter to the model:

- `goal_status` / `goal_elapsed_s` — `"completed (timed, unverified)"` lets a multi-step plan advance *while signalling the distance/angle was a timed guess*; `"blocked"` tells the model the last move hit something (or the echo went unknown) and it should re-plan rather than retry blindly; `"preempted"` explains why a goal vanished.
- `obstacle` / `distance_m` / `distance_known` — answers `check_distance`, and lets the model narrate ("something's right in front of me, I stopped"). When `distance_known` is false the model should treat distance as unknown, not clear.
- `target_visible` / `target_bbox` / `tracker_score` / `ticks_since_last_detector_confirmation` — during `FOLLOW`, lets the model know not just *whether* the tracker has a box but *how trustworthy* it is, so it can say "I think I still see you" vs "I've lost you" honestly.
- `mode` — so the model knows what the body is currently doing without re-deciding.

> **DECISION —** Reporting is **pull, not push**: the deliberative layer reads the latest `RobotState` snapshot when it needs it (every step, and when waiting on a goal). There is no event stream from reactive → deliberative for v1. Polling a last-write-wins snapshot is simpler, drop-tolerant, and fast enough given that deliberative steps are already seconds apart.

### 2.5 Camera ownership & the frame pipeline

The **reactive process owns the camera** — there is exactly one `open()` on the C270, in the reactive process. The deliberative process never touches the device; it asks the reactive process for a still over the socket. The C270's UVC/MJPEG `read()` is a **blocking** call (it waits for the next decoded frame), so it must never sit on the reactive tick.

> **DECISION —** The blocking `read()` runs in its **own capture thread** that does nothing but loop `read()` → decode → write into a **single-slot, double-buffered "latest frame"**. Both the reactive tick and the deliberative still-grab read the *newest decoded frame* out of that buffer; **neither ever calls `read()` directly**. A stale frame is fine (it's a snapshot, last-write-wins, exactly like `RobotState`); a blocked tick is not. The capture thread caps its loop at the sensor-friendly cadence and never drives the camera faster than needed.

Decouple **three** frame consumers, each at its own resolution/cadence — they are not the same image and must not be coupled:

| Consumer | Resolution / source | Cadence | Notes |
|---|---|---|---|
| **Detector** (person detection) | small, **~320×240**, downscaled *before* inference | a few Hz (whatever the Pi sustains) | Runs in a **child process** (see §2.6). Downscaling before inference is the main throughput lever on a no-NPU Pi. |
| **Tracker** (follow centering) | a **crop around the last box**, not the full frame | every reactive tick | Cheap because it only processes a region; keeps `FOLLOW` at full tick rate. |
| **VLM still** (`describe_scene`, `look`) | a **separate, higher-quality grab** | only on demand | Grabbed from the latest-frame buffer **only** when `describe_scene`/`look` fires; JPEG-compressed/downscaled before upload (§3). Never grabbed per tick. |

The tracker and detector read the same latest-frame buffer the reactive tick uses; the VLM still is a one-off higher-quality pull. None of the three blocks the others, and none calls a blocking `read()` on the reactive tick.

### 2.6 The watchdog & the process model

> **DECISION —** A **separate watchdog process** with its **own independent timer** zeroes the motor GPIO whenever the reactive-tick heartbeat is older than **100 ms**. The reactive tick stamps a shared heartbeat (e.g. a memory-mapped timestamp / named value) every loop; the watchdog reads it on its own timer and, if it's stale, drives the motor enable/PWM pins to zero directly. It is deliberately tiny and dependency-free so it cannot deadlock with whatever wedged the reactive loop.
>
> **What it saves you from:** a hung, crashed, or deadlocked reactive process leaving the motors latched on — the single worst failure for a thing with wheels. **What it cannot save you from:** anything mechanical or electrical (a shorted driver, a seized wheel, a wiring fault), a watchdog-process crash itself, or bad *but live* control (it only checks liveness, not correctness — a reactive loop that ticks happily while commanding a bad heading looks healthy to it). The collision-stop reflex in §2.3 and the combined-load gate in §4 cover the "alive but wrong / too slow" cases; the watchdog covers "not alive."

> **DECISION —** The **person-detector runs in a child *process*, not a thread.** CPython's GIL means a CPU-bound detector thread would contend with the reactive/safety loop for the interpreter and inflate tick latency; a separate process sidesteps the GIL entirely. The detector child reads frames (via the shared latest-frame buffer / shared memory) and returns person boxes over a small pipe or queue; `step_follow` consumes whatever the child last produced (§4). Net process count on the Pi: **reactive** (owns GPIO + camera + capture thread), **watchdog**, **detector child**, and — in production if co-located — **deliberative**.

## 3. The Deliberative Step Loop

One deliberative "step" is: perceive → think → act → report. **Stills, not video, for v1** — each step sends one fresh photo. The reactive layer keeps the robot safe and responsive *between* steps, so the bursty cadence is fine.

```python
def deliberative_step(user_text, history, client, reactive):
    state = reactive.read_state()                 # latest RobotState snapshot
    frame = reactive.capture_still()              # one JPEG, not a stream
    # (voice track, later) user_text = stt.transcribe(mic_audio)

    model = pick_model(user_text, history)        # tiered — see below
    msg = client.messages.create(
        model=model,
        max_tokens=1024,
        # thinking is opt-in AND only sent to models that support it — the fast
        # per-step tier (Haiku) does NOT, and sending it there 400s. Omit it by
        # default; attach {"type": "adaptive"} only on the mid/big tiers when a
        # step needs deeper reasoning.
        tools=ABILITY_MENU,                       # the §2.1 schema
        messages=history + [{
            "role": "user",
            "content": [
                {"type": "text", "text": build_context(user_text, state)},
                {"type": "image", "source": image_block(frame)},
            ],
        }],
    )

    for block in msg.content:
        if block.type == "tool_use":
            intent_or_answer = dispatch(block.name, block.input, reactive)
            # drive/turn/stop/follow -> reactive.set_intent(...)  (one Intent over the socket)
            # explore -> NOT a reactive intent: run the drive/turn/describe_scene loop here (§2.1)
            # check_distance/look/describe_scene -> read/answer, feed back next turn
        elif block.type == "text":
            speak(block.text)                     # TTS later; print for v1

    return msg                                    # append to history for the next step
```

`build_context` stitches the user's words together with a compact rendering of `RobotState` (current mode, last goal status, distance, whether a person is visible, and the tracker honesty signals) and the ability menu's intent. It must also **tell the model the ground rules of this body**: distances and angles are **approximate and uncorrected outside `FOLLOW`** (timed guesses, no encoders), a `"completed (timed, unverified)"` means "ran the move for about that long," `distance_known == false` means the ultrasonic reading is unknown (not clear), and a low `tracker_score` / high `ticks_since_last_detector_confirmation` means "I may have lost the person." The image is the still. That's the whole prompt.

**Tiered models.** Don't pay flagship latency/cost on every frame:

| Tier | When | Model |
|---|---|---|
| Cheap per-step VLM | Centering sanity, "is the path clear?", "what's roughly ahead?", routine tool-picking | `claude-haiku-4-5` |
| Mid | Ambiguous requests, multi-step `explore` reasoning | `claude-sonnet-4-6` |
| Big | "Describe this whole scene in detail", "read this sign", genuinely hard asks (often triggered by `describe_scene` detail="full") | `claude-opus-4-8` |

> **DECISION —** `pick_model()` is an **explicit, auditable routing table**, not a vibe. It **defaults to `claude-haiku-4-5`** and escalates **only** on enumerated signals. Every step **logs the chosen tier and the signal that chose it**, so routing is reviewable after the fact. Extended/adaptive thinking is enabled **ONLY on models that support it** (`thinking={"type":"adaptive"}` on the mid/big tiers) and **only when a step needs deeper reasoning**; the fast per-step tier (`claude-haiku-4-5`) runs **without** it — sending `thinking` to Haiku returns a 400 ("adaptive thinking is not supported on this model"), so it stays off by default and is omitted entirely on that tier.

```python
# Auditable routing. Default cheap; escalate only on the listed signals; log every choice.
def pick_model(user_text, intent, history, cheap_flag) -> str:
    # OPUS — only for genuinely hard vision/reading
    if intent == "describe_scene" and intent_detail == "full":
        return log_tier("claude-opus-4-8", "describe_scene(detail=full)")
    if is_read_text_request(user_text):                       # "read this sign/label/screen"
        return log_tier("claude-opus-4-8", "read-text intent")

    # SONNET — multi-step reasoning the cheap tier asked for, or an explore plan
    if is_multi_step_explore_request(user_text):              # explore is deliberative sugar (§2.1)
        return log_tier("claude-sonnet-4-6", "multi-step explore request")
    if cheap_flag == "need_more_reasoning":                   # structured flag from the cheap tier
        return log_tier("claude-sonnet-4-6", "cheap-tier need_more_reasoning")

    # HAIKU — default for routine per-step VQA / tool-picking
    return log_tier("claude-haiku-4-5", "default")
```

The escalation signals are **exactly**: `describe_scene(detail='full')` or a read-text intent → `claude-opus-4-8`; a multi-step `explore` request or a structured `need_more_reasoning` flag from the cheap tier → `claude-sonnet-4-6`; everything else stays on `claude-haiku-4-5`. Nothing else escalates.

> **DECISION —** Enforce a **hard per-session token/call budget** in the deliberative process. A WiFi retry-storm or a runaway escalation must not silently run up cost: track cumulative tokens and API calls for the session, and once the cap is hit, **stop issuing model calls** (the robot falls back to `IDLE`/local behavior exactly as in the §5 outage path) rather than retrying forever. The budget is a hard ceiling the operator sets in `config` (§6), separate from any per-call `max_tokens`.

> **RISK —** Each step is photo → upload → API call → response → new intent, so high-level decisions arrive in bursts. The often-quoted "2–3 s/step" is a **target to verify, not an assumption** — it is acceptable *because* the reactive layer fills the gaps, but only if it actually holds on real home WiFi. The smooth upgrade (streaming multimodal) is explicitly out of scope for v1 — see `architecture.md` and `roadmap.md`.

### 3.1 Latency budget & measurement plan

The deliberative loop has two latency-sensitive paths, and both must be **measured, not assumed**. Time the three contributors **separately** so it's clear which one to fix:

| Stage | What to time | Lever |
|---|---|---|
| **Capture** | C270 JPEG grab from the latest-frame buffer + encode | downscale before encode; the still is already buffered (§2.5) |
| **Upload** | the JPEG over **real home WiFi** to the API | **JPEG-compress / downscale the uploaded still** — this is the biggest controllable term |
| **Model think** | server-side time to first/last token at the chosen tier | tier routing (§3); adaptive thinking (mid/big tiers only) |

Two budgets to validate:

- **Per-step deliberative VQA** (the routine perceive→think→act step): target **"a few seconds"**. Validate that a Haiku-tier VQA step returns in that band on real WiFi with a compressed still.
- **The magic-moment loop** (capture → upload → VLM → answer for an explore step or a `describe_scene`): an explore step must stay **under the ~10 s ceiling** on real WiFi. Because `explore` is deliberative sugar (a loop of `drive`/`turn`/`describe_scene`, §2.1), each leg pays one of these round-trips, so the per-leg budget *is* the explore-step budget.

> **DECISION —** Measure capture, upload, and model-think as three separate numbers from day one and log them per step (alongside the routed tier from §3). "It feels slow" must resolve to "upload is 4 s because the still is full-resolution" or "Haiku think is 1.2 s" — not a single opaque total. JPEG-compress/downscale the uploaded still before trusting any number. Treat the 2–3 s/step figure as a hypothesis these measurements confirm or refute.

## 4. Person-Tracking — Confronting the Physics

"Lightweight OpenCV tracker" hides a real fork, and getting it wrong wrecks follow-mode:

- A **blob/color tracker** (e.g. CamShift / a KCF/CSRT box seeded once) is cheap — easily 30 Hz on the Pi 5's CPU — but **fragile**: it drifts with lighting changes, similar clothing, and occlusion, and has no notion of "person," so once it loses the target it locks onto whatever it last looked like.
- A **real person detector** (YOLO-nano, MobileNet-SSD) is **robust** — it actually knows what a person is and re-acquires after occlusion — but the Pi 5 has **no NPU**, so on CPU it may only manage a **few fps**. That collides head-on with the 10–30 Hz reactive target: you cannot run detection every tick.

> **THESIS —** Use **track-by-detection**. Run the cheap tracker every reactive tick (on a crop around the last box, §2.5) to keep follow-mode at full rate, and run the slow person detector at whatever fps the Pi sustains (say 2–5 Hz) in a **child process** (§2.6 — a process, not a thread, to dodge GIL contention with the safety loop) purely to **re-seed and validate** the tracker's box. This 2-5 Hz is the expected range on the Pi 5 CPU; Gate H (see `roadmap.md`) sets the GO threshold at >=3 Hz sustained so track-by-detection stays viable. The fast tracker provides smoothness; the slow detector provides the "it's actually a person, and here's where they really are now" correction. This is the hobby version of how real trackers are built, and it's the only design that satisfies both robustness and the reactive frame budget on a no-NPU Pi.

Concretely, inside `step_follow`:

```python
def step_follow(state, motors):
    ok, bbox, score = tracker.update(crop_around_last_box(latest_frame))   # cheap, every tick
    if detector_proc.has_fresh_result():           # from the CHILD PROCESS, a few times/sec
        det = detector_proc.take()                  # person boxes
        bbox, ok = reconcile(bbox, det)             # re-seed tracker if it drifted/lost
        tracker.reinit(latest_frame, bbox)
        state.ticks_since_last_detector_confirmation = 0
    else:
        state.ticks_since_last_detector_confirmation += 1

    # Publish honesty signals so FOLLOW can choose coast vs stop instead of driving blind.
    state.tracker_score = score if ok else 0.0

    stale = state.ticks_since_last_detector_confirmation > COAST_TICKS
    if not ok or score < TRACK_MIN_SCORE or stale:
        # Tracker is unconfirmed/low-confidence/stale: coast briefly, then STOP and report lost.
        # This is what prevents "the tracker confidently drives toward the couch."
        coast_or_stop(state, motors)
        state.target_visible = False
        return

    state.target_visible, state.target_bbox = True, bbox
    err_x = bbox_center_x(bbox) - frame_center_x   # centering error
    turn_cmd, fwd_cmd = follow_controller(err_x, bbox_area(bbox))
    motors.drive(fwd_cmd, turn_cmd)                # collision-stop still overrides upstream
```

> **OPEN —** Which detector and which tracker. Candidates: detector ∈ {YOLO-nano, MobileNet-SSD}; tracker ∈ {CSRT (accurate, slower), KCF (faster, less robust), color-histogram CamShift (cheapest)}. The choice is **data-dependent** and not decidable from a desk — it depends on the actual sustained fps on *this* Pi 5 with *this* camera.

> **OPEN / RISK —** Run an early **FPS spike** before committing to the follow-mode design: measure sustained person-detector fps on the Pi 5 (CPU only) at the camera resolution we intend to use. This is an explicit **GO/NO-GO gate**:
> - **GO (≳3–5 Hz sustained):** track-by-detection as specified — detector re-seeds, tracker fills.
> - **NO-GO (≲1–2 Hz):** fall back to a **blob/color tracker only** for v1 follow-mode (accept the fragility, constrain the demo to good lighting / distinct clothing), and push robust person-following to post-v1 — possibly behind dedicated hardware (NPU/Jetson).
> The spike de-risks the single hardest piece of software in the build. The **RISK** if skipped: building all of follow-mode around a detector cadence the Pi can't actually hit, and discovering it only when a moving person walks the robot into a wall. The spike is scheduled in `roadmap.md`.

> **DECISION —** The real safety measurement is the **combined-load gate**: reactive-tick **p99 latency < 33 ms** (i.e. ≥30 Hz held at the 99th percentile) with the **tracker + detector child + camera capture + motor writes all live at once**. The detector-fps spike above (Gate H in `roadmap.md`) only measures the detector's throughput *in isolation*; it says nothing about whether the safety loop still hits its deadline once everything is running and contending for the Pi's four cores. Measure tick latency under full load — that is the number the watchdog (§2.6) and collision-stop (§2.3) ultimately depend on. The combined-load gate is scheduled alongside Gate H in `roadmap.md`.

## 5. Graceful Degradation & Connectivity

The deliberative layer needs WiFi; the reactive layer never does. The system is designed so a network hiccup degrades capability, not safety.

```python
# Deliberative side — retry with backoff, never block the body.
def safe_step(...):
    try:
        return deliberative_step(...)
    except (APIConnectionError, RateLimitError, APITimeoutError) as e:
        backoff.sleep()                 # exponential, capped
        return None                     # no new intent issued this step
```

> **DECISION —** On WiFi/cloud loss mid-command: the **reactive layer keeps running unchanged** — it finishes any bounded in-progress goal, keeps obstacle-avoiding, keeps follow-mode (which is fully local), and safe-stops on obstacles. It just stops receiving *new* high-level intents. The **deliberative layer retries with exponential backoff** and resumes issuing intents when the cloud returns. The robot "stops taking new orders," it does not go haywire. A long deliberative outage with no active goal simply leaves the robot in `IDLE` — a safe resting state.

The SDK already retries 429/5xx/connection errors with backoff; we wrap the step so a failed call yields "no intent this step" rather than a crash, and the next step retries naturally.

## 6. Secrets & Config

> **DECISION —** Cloud API keys live in the **environment**, never in source. The deliberative process reads `ANTHROPIC_API_KEY` (and/or `OPENAI_API_KEY`) from the environment / a git-ignored `.env`; the SDK's default `anthropic.Anthropic()` constructor picks it up. No key is ever committed, logged, embedded in a prompt, or written to a memory/state file. Model selection (the per-tier IDs from §3 — `claude-haiku-4-5` / `claude-sonnet-4-6` / `claude-opus-4-8`), the **hard per-session token/call budget** (§3), speed limits, `SAFE_STOP_THRESHOLD`, tick rate, the watchdog timeout (100 ms, §2.6), the gpiozero pin factory (lgpio/native on Pi 5, §1), and the IPC socket endpoint live in one small `config.py` / `config.toml` checked into the repo — config in source, **secrets in the environment**. On the Pi, the key lives in a `0600` env file sourced by the deliberative service unit; on the laptop, in the shell environment.

## 7. Voice — Later Track (High-Level Only)

Voice is a deliberately separate track, bolted on only after the full **text** loop works end to end. The deliberative step loop already has the seams for it (`stt.transcribe(...)` before `build_context`, `tts.say(...)` in place of `print`), so wiring it in is mechanical — but the hard part is not the wiring.

> **RISK —** Far-field speech-to-text on a noisy floor, from a robot that is *also playing audio through its own speaker*, is a real project, not an afternoon bolt-on. It drags in wake-word detection, voice-activity detection, and acoustic echo cancellation (so the robot doesn't transcribe itself), plus mic quality and USB-audio latency. Treating "add a mic and a speaker" as a quick final step is the classic way to stall the whole build. Keep it isolated so its rabbit holes never block the core text-driven magic. Sequencing and the specific STT/TTS choices live in `roadmap.md`.
