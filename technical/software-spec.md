# Software Spec

> The detailed software design for yalp: the two-layer runtime, the contract between the fast on-Pi reactive loop and the slow cloud deliberative loop, person-tracking, degradation, and config. Build against this.

---

`architecture.md` makes the case for *why* yalp runs on two loops; this doc specifies *what* the two loops actually exchange and how each is built. If you only read one section, read §2 — the loop-to-loop contract is the spine of the system, and everything else hangs off it. Siblings: `architecture.md` (the framing), `hardware.md` (the body this code drives), `roadmap.md` (the build order and the gates that this doc declares).

## 1. Software Stack & Where Each Piece Runs

The single organizing principle: **only motor control and camera capture genuinely need the Pi.** Everything else — the agent loop, prompt building, the model calls — is plain Python you develop on your laptop with its webcam standing in for the robot's eyes, then marry to the body at the end. The reactive layer is the *only* code that must run on the Pi at robot-speed.

| Layer | Component | Library / Service | Runs on | Notes |
|---|---|---|---|---|
| Reactive | Motor drive (differential) | `gpiozero` (`Motor`, `Robot`) | **Pi only** | PWM to the DRV8833/TB6612; see `hardware.md` for pinout. |
| Reactive | Obstacle sensing | `gpiozero` (`DistanceSensor`) | **Pi only** | HC-SR04 ultrasonic; powers collision-stop. |
| Reactive | Lightweight tracker | `opencv-python` | Pi (prod) / laptop (dev) | Centering + follow-mode. See §4 — this is the contested piece. |
| Reactive | Camera capture | `picamera2` (Pi cam) **or** OpenCV UVC capture (USB webcam) | Pi (prod) / laptop (dev) | v1 ships the C270 USB/UVC path; `picamera2` is a later swap if we move to the Pi Camera Module. |
| Deliberative | Cloud VLM/LLM agent | `anthropic` SDK (primary) / `openai` SDK (alt) | Laptop (dev), Pi or laptop (later) | Tool-use / vision; tiered models — see §3. |
| Deliberative | Prompt + dispatch glue | Plain Python (`stdlib` + SDK) | Anywhere | The "brain" you iterate on the laptop. |
| Voice *(deferred)* | Speech-to-text / text-to-speech | STT/TTS lib or cloud service (TBD) | Pi + cloud | Separate track; see §7 and `roadmap.md`. |
| OS | Base image | **Raspberry Pi OS Lite (64-bit)** | Pi | Headless. Develop over SSH/WiFi; enable SSH, camera, I2C at flash time. No desktop. |

> **DECISION —** The reactive layer is a standalone Python process on the Pi that owns GPIO and the camera, and runs whether or not the cloud is reachable. The deliberative layer is a *separate* process (laptop in dev) that talks to it over a local IPC channel (a localhost socket / small HTTP endpoint, or a shared in-process queue once co-located). The two never share Python objects across the network — they exchange the message shapes in §2 only.

> **THESIS —** Build the deliberative loop on the laptop first with the laptop webcam as the camera, stub the reactive layer with a fake that just prints `drive(...)`/`set_mode(...)`, and only swap in the real Pi reactive process once the agent loop is fun to use. Debugging Python, Linux, *and* hardware simultaneously is the main way hobby builds stall.

## 2. The Crux — The Loop-to-Loop Interface Contract

The model never twitches a motor. It picks **intents**, expressed as **tool calls** against a fixed ability menu. Each tool call sets (or adjusts) a **mode + goal** on the reactive layer, or asks a question the reactive layer can answer from its latest state. The reactive layer executes the mode at 10–30 Hz, keeps the robot safe, and publishes a small **state object** back up. That is the entire contract.

### 2.1 The ability menu (what the LLM can call)

| Tool | Parameters | Effect (one line) |
|---|---|---|
| `drive` | `distance_m: float` (signed; −=back), `speed: 0..1` | Enter `DRIVE_GOAL`: drive straight until distance covered, blocked, or preempted. |
| `turn` | `angle_deg: float` (signed; +=left/CCW), `speed: 0..1` | Enter `DRIVE_GOAL` (rotate variant): turn in place by angle. |
| `stop` | — | Enter `IDLE`: cancel current goal, wheels halt. |
| `look` / `capture` | `save: bool=false` | Grab one still from the camera; return a frame handle for the next model turn. |
| `check_distance` | — | Return the latest ultrasonic reading (meters) from shared state. |
| `describe_scene` | `detail: "quick"\|"full"` | Capture a still and (deliberative side) escalate to a model tier for a scene description. |
| `enter_follow_mode` | `target: "nearest_person"` | Enter `FOLLOW`: local tracker centers + approaches the target. No cloud in the loop. |
| `explore` | `goal_text: str` | Enter `EXPLORE`: step-wise "go look around and report" — reactive drives short legs; deliberative re-decides each step. |
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

> **OPEN —** Whether `explore` is a first-class reactive mode or just sugar the deliberative layer implements as a loop of `drive`/`turn`/`describe_scene`. Leaning toward the latter for v1 (keeps the reactive layer dumb), but it's not settled — revisit once step latency is measured.

### 2.2 The intent / mode model

The reactive layer is a small state machine. At any instant it is in exactly one **mode**, each carrying a typed **goal payload**:

| Mode | Goal payload | Reactive behavior |
|---|---|---|
| `IDLE` | — | Wheels stopped. Sensors still read. Default and safe resting state. |
| `DRIVE_GOAL` | `{kind: "straight"\|"rotate", target, speed, progress}` | Execute until target met (distance/angle), blocked, or preempted. |
| `FOLLOW` | `{target, last_seen_bbox, last_seen_ts}` | Tracker centers + approaches; coasts/searches briefly if target lost. |
| `EXPLORE` | `{goal_text, leg}` | Drive a short bounded leg, then signal "step done" so deliberative re-decides. |
| `SAFE_STOP` | `{reason, distance}` | Hard stop, motion latched off, until obstacle clears AND a new intent arrives. |

The two loops communicate only through one shared **state object**, owned and written by the reactive layer, read by both; the deliberative layer writes *intents* (never reactive state) through `set_intent`:

```python
# Written by reactive loop every tick; read by deliberative loop on demand.
@dataclass
class RobotState:
    mode: str                 # "IDLE" | "DRIVE_GOAL" | "FOLLOW" | "EXPLORE" | "SAFE_STOP"
    goal: dict | None         # the active mode's payload (see table)
    goal_status: str          # "running" | "completed" | "blocked" | "preempted" | "none"
    distance_m: float         # latest ultrasonic reading
    obstacle: bool            # distance_m < SAFE_STOP_THRESHOLD
    target_visible: bool      # tracker currently sees the follow target
    target_bbox: tuple | None # (x, y, w, h) in frame px, or None
    last_frame_id: str | None # handle to most recent capture
    speed_limit: float        # current clamp, 0..1
    ts: float                 # monotonic timestamp of this snapshot

# Written by deliberative loop; consumed by reactive loop at the top of each tick.
@dataclass
class Intent:
    mode: str                 # requested mode
    goal: dict | None         # requested goal payload
    seq: int                  # monotonically increasing; newer seq wins
```

> **DECISION —** `RobotState` and `Intent` are the *only* two shapes crossing the boundary. The reactive layer publishes `RobotState` every tick (last-write-wins; no history). The deliberative layer pushes an `Intent` with a monotonically increasing `seq`; the reactive layer adopts the highest `seq` it has seen. This makes the channel stateless and tolerant of drops — a lost `RobotState` is replaced by the next tick; a lost `Intent` is simply not adopted and the model can re-issue.

### 2.3 Preemption semantics

There is a strict priority order. Higher always wins, immediately:

1. **Collision-stop (SAFE_STOP)** — a reflex, *never* gated behind the AI. If `distance_m < SAFE_STOP_THRESHOLD`, the wheels stop this tick, full stop, regardless of mode or any in-flight intent.
2. **Newest intent** — a new `Intent` (higher `seq`) replaces whatever mode/goal is in progress. "Follow me" mid-drive abandons the drive and enters `FOLLOW`. No queueing, no "finish the current move first."
3. **Current mode execution** — only if neither of the above fired does the active mode get to advance its goal this tick.

> **DECISION —** New intents *replace*, they do not *queue*. The robot always reflects the operator's most recent instruction. If the user wants a sequence, the deliberative layer issues it step by step (waiting for `goal_status == "completed"` between steps), rather than the reactive layer buffering a plan.

> **DECISION —** `SAFE_STOP` is sticky. Clearing the obstacle is necessary but not sufficient to resume — the robot waits in `SAFE_STOP` until the obstacle clears **and** a fresh intent arrives. This prevents a robot that's been nosed into a wall from lurching forward the instant someone moves out of the way.

The reactive loop tick, in full:

```python
def reactive_tick(state, intent_inbox, motors, sensor):
    # 1. READ SENSORS
    dist = sensor.distance          # meters, blocking-free read
    state.distance_m = dist
    state.obstacle = dist < SAFE_STOP_THRESHOLD

    # 2. SAFETY OVERRIDE — beats everything, every tick
    if state.obstacle:
        motors.stop()
        state.mode, state.goal_status = "SAFE_STOP", "blocked"
        state.goal = {"reason": "obstacle", "distance": dist}
        publish(state)
        return                      # nothing else runs this tick

    # 3. ADOPT NEWEST INTENT (preempt in-progress mode)
    new = intent_inbox.take_highest_seq()      # None if nothing newer
    if new is not None:
        if state.mode == "SAFE_STOP" and not state.obstacle:
            pass                    # allowed to leave SAFE_STOP now
        state.mode, state.goal = new.mode, new.goal
        state.goal_status = "running"

    # 4. EXECUTE CURRENT MODE
    if state.mode == "IDLE" or state.mode == "SAFE_STOP":
        motors.stop()
    elif state.mode == "DRIVE_GOAL":
        done = step_drive_goal(state, motors)          # advances progress
        state.goal_status = "completed" if done else "running"
        if done: state.mode = "IDLE"
    elif state.mode == "FOLLOW":
        step_follow(state, motors)                     # tracker centers + approaches
    elif state.mode == "EXPLORE":
        leg_done = step_explore_leg(state, motors)
        state.goal_status = "completed" if leg_done else "running"

    # 5. PUBLISH STATE upward
    publish(state)
```

Every branch in step 4 clamps commanded speed to `state.speed_limit` before touching `motors`. The loop is designed to never block: sensor reads, tracker steps, and motor writes are all non-blocking, so the tick rate stays in the 10–30 Hz band even when the cloud is mid-think.

### 2.4 How the reactive layer reports up

The deliberative layer reads `RobotState` whenever it builds a prompt or polls a goal. The fields that matter to the model:

- `goal_status` — `"completed"` lets a multi-step plan advance; `"blocked"` tells the model the last move hit something; `"preempted"` explains why a goal vanished.
- `obstacle` / `distance_m` — answers `check_distance`, and lets the model narrate ("something's right in front of me, I stopped").
- `target_visible` / `target_bbox` — during `FOLLOW`, lets the model know whether the tracker still has the person, so it can say "I lost you" or re-engage.
- `mode` — so the model knows what the body is currently doing without re-deciding.

> **DECISION —** Reporting is **pull, not push**: the deliberative layer reads the latest `RobotState` snapshot when it needs it (every step, and when waiting on a goal). There is no event stream from reactive → deliberative for v1. Polling a last-write-wins snapshot is simpler, drop-tolerant, and fast enough given that deliberative steps are already seconds apart.

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
        thinking={"type": "adaptive"},            # let it reason on hard asks
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
            # drive/turn/follow/explore -> reactive.set_intent(...)
            # check_distance/look/describe_scene -> read/answer, feed back next turn
        elif block.type == "text":
            speak(block.text)                     # TTS later; print for v1

    return msg                                    # append to history for the next step
```

`build_context` stitches the user's words together with a compact rendering of `RobotState` (current mode, last goal status, distance, whether a person is visible) and the ability menu's intent. The image is the still. That's the whole prompt.

**Tiered models.** Don't pay flagship latency/cost on every frame:

| Tier | When | Model |
|---|---|---|
| Cheap per-step VLM | Centering sanity, "is the path clear?", "what's roughly ahead?", routine tool-picking | `claude-haiku-4-5` |
| Mid | Ambiguous requests, multi-step `explore` reasoning | `claude-sonnet-4-6` |
| Big | "Describe this whole scene in detail", "read this sign", genuinely hard asks (often triggered by `describe_scene` detail="full") | `claude-opus-4-8` |

> **DECISION —** Default every step to the cheap tier and **escalate only on signal** — an explicit `describe_scene(detail="full")`, a request the cheap tier flags as beyond it, or a user ask that obviously needs reading/reasoning. `pick_model` encodes this routing; it is the main cost lever. Adaptive thinking stays on so the chosen tier can spend more reasoning on the hard turns and little on the easy ones.

> **RISK —** Each step is photo → API call → response → new intent, so high-level decisions arrive in 2–3s bursts. This is inherent to a stills-in-the-loop design and is acceptable *because* the reactive layer fills the gaps. The smooth upgrade (streaming multimodal) is explicitly out of scope for v1 — see `architecture.md` and `roadmap.md`.

## 4. Person-Tracking — Confronting the Physics

"Lightweight OpenCV tracker" hides a real fork, and getting it wrong wrecks follow-mode:

- A **blob/color tracker** (e.g. CamShift / a KCF/CSRT box seeded once) is cheap — easily 30 Hz on the Pi 5's CPU — but **fragile**: it drifts with lighting changes, similar clothing, and occlusion, and has no notion of "person," so once it loses the target it locks onto whatever it last looked like.
- A **real person detector** (YOLO-nano, MobileNet-SSD) is **robust** — it actually knows what a person is and re-acquires after occlusion — but the Pi 5 has **no NPU**, so on CPU it may only manage a **few fps**. That collides head-on with the 10–30 Hz reactive target: you cannot run detection every tick.

> **THESIS —** Use **track-by-detection**. Run the cheap tracker every reactive tick to keep follow-mode at full rate, and run the slow person detector at whatever fps the Pi sustains (say 2–5 Hz, ideally on a separate thread) purely to **re-seed and validate** the tracker's box. The fast tracker provides smoothness; the slow detector provides the "it's actually a person, and here's where they really are now" correction. This is the hobby version of how real trackers are built, and it's the only design that satisfies both robustness and the reactive frame budget on a no-NPU Pi.

Concretely, inside `step_follow`:

```python
def step_follow(state, motors):
    ok, bbox = tracker.update(latest_frame)        # cheap, every tick
    if detector_thread.has_fresh_result():         # arrives a few times/sec
        det = detector_thread.take()               # person boxes
        bbox = reconcile(bbox, det)                 # re-seed tracker if it drifted/lost
        tracker.reinit(latest_frame, bbox)
    if bbox is None:
        coast_or_search(state, motors)             # brief search, then report lost
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

> **DECISION —** Cloud API keys live in the **environment**, never in source. The deliberative process reads `ANTHROPIC_API_KEY` (and/or `OPENAI_API_KEY`) from the environment / a git-ignored `.env`; the SDK's default `anthropic.Anthropic()` constructor picks it up. No key is ever committed, logged, embedded in a prompt, or written to a memory/state file. Model selection (the per-tier IDs from §3), speed limits, `SAFE_STOP_THRESHOLD`, tick rate, and the IPC endpoint live in one small `config.py` / `config.toml` checked into the repo — config in source, **secrets in the environment**. On the Pi, the key lives in a `0600` env file sourced by the deliberative service unit; on the laptop, in the shell environment.

## 7. Voice — Later Track (High-Level Only)

Voice is a deliberately separate track, bolted on only after the full **text** loop works end to end. The deliberative step loop already has the seams for it (`stt.transcribe(...)` before `build_context`, `tts.say(...)` in place of `print`), so wiring it in is mechanical — but the hard part is not the wiring.

> **RISK —** Far-field speech-to-text on a noisy floor, from a robot that is *also playing audio through its own speaker*, is a real project, not an afternoon bolt-on. It drags in wake-word detection, voice-activity detection, and acoustic echo cancellation (so the robot doesn't transcribe itself), plus mic quality and USB-audio latency. Treating "add a mic and a speaker" as a quick final step is the classic way to stall the whole build. Keep it isolated so its rabbit holes never block the core text-driven magic. Sequencing and the specific STT/TTS choices live in `roadmap.md`.
