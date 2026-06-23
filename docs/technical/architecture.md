# yalp — Technical Architecture

> The conceptual backbone of yalp: how a cloud "brain" and an on-board "spinal cord" split the work so the robot stays smart, responsive, and safe at the same time. The two-loop split, the agent/tool model, and the contract between thinking and reflexes. Interface schemas live in `software-spec.md`.

---

This document defines the **concepts and boundaries** of yalp's control system. It is deliberately schema-free: the exact tool list, message formats, and intent payloads live in `software-spec.md`. Read this first to understand *why* the system is shaped the way it is, then read `software-spec.md` for the concrete contract. Hardware choices that this design leans on are in `hardware.md`; build sequencing is in `roadmap.md`; the scope envelope is in `product-spec.md`.

> **STATUS —** The laptop "brain" (see/agent/follow, including the pluggable-detector follow loop and voice OUTPUT) is built and tested; the on-Pi reactive body is the remaining work. This doc fixes *concepts*; the canonical, up-to-date build status and gate ledger live in **`roadmap.md`** — treat it as the single status home.

## 1. The Core Idea: Two Loops, Not One

yalp runs on **two control loops with different clocks**, not a single pipeline. They are different because the two halves of the job have fundamentally different time budgets.

- **The fast reactive layer** lives entirely on the Pi, runs at **~10–30 Hz**, and never touches the network. It is reflex and habit: stop before you hit something, keep the person centered, keep driving until a condition flips. It is dumb on purpose, and it is always running.
- **The slow deliberative layer** lives in the **cloud** and runs at **~seconds per step**. It calls a cloud vision-language model to turn speech and a photo into a high-level intent, pick a mode/goal, describe what the camera sees, and talk back. It is smart, expensive, and laggy.

The deliberative layer decides *what to do*. The reactive layer decides *how to keep doing it safely right now*. The cloud sets the goal; the local loop drives the body and refuses to hurt it.

> **DECISION —** Split control into a local fast reactive layer (~10–30 Hz, no network) and a cloud slow deliberative layer (~seconds/step). The reactive layer owns the motors and safety in real time; the deliberative layer only sets goals and modes. This split is the single most important architectural decision in v1 and everything else follows from it.

### Why the split is non-negotiable

A single cloud-routed loop has a fatal flaw: every decision takes a 2–3 second round trip (capture → upload → model → response). During those seconds the robot would be **blind and uncontrolled** — coasting on its last command with nothing watching the ultrasonic. The photo was taken before the robot moved, the answer arrives after it has already coasted forward, and there is no reflex in between. That is precisely how hobby bots drive into walls. Keeping reflexes local closes that window: the body is *never* waiting on the cloud to avoid a collision or to keep tracking a moving target. The thinking happens in bursts; the body keeps moving and stays safe *between* the bursts.

```
  ┌──────────────────────────────────────────────────────────────┐
  │  SLOW DELIBERATIVE LAYER — cloud, ~seconds per step            │
  │                                                                │
  │   voice/text ─► speech-to-text                                 │
  │              ─► LLM/VLM  (my words + ONE still photo)          │
  │              ─► picks intent, sets a MODE/GOAL  ───────────┐   │
  │              ─► describes scene ─► text-to-speech ─► reply │   │
  └───────────────────────────────────────────────────────────┼───┘
              ▲ state reports (mode, progress, distance)       │ goal / mode
              │                                                 ▼
  ┌───────────┴────────────────────────────────────────────────────┐
  │  FAST REACTIVE LAYER — local, on the Pi, ~10–30 Hz, no cloud     │
  │                                                                  │
  │   camera frames + ultrasonic ─► reflexes ─► motors               │
  │     • collision-stop          (overrides EVERYTHING)             │
  │     • person-tracking / centering   (in follow mode)             │
  │     • "keep doing X until a condition flips"                     │
  └──────────────────────────────────────────────────────────────────┘

      The LLM sets intent. The reactive layer executes it and keeps it safe.
```

![Annotated desk scene of the yalp rover labelling the two control loops — "Claude - deliberative loop" in the cloud, "vision / follow" as the reactive layer on the Pi, and "collision-stop 0.30 m" as the safety threshold](../images/yalp-annotated-build.png)

*Annotated build — the two-loop split made physical: deliberative (Claude, cloud) above the line; reactive (vision/follow, collision-stop) on the Pi below it.*

The two layers map cleanly onto the project's "what can it really do" buckets: snappy/reliable things (follow, stop, avoid) are **local reflexes**; clumsy things (explore-and-describe, visual Q&A) are **cloud thinking** that tolerates latency because they are not time-critical.

---

## 2. The Agent / Tool-Use Model

yalp does not hand-code a keyword table ("if user says 'forward' then drive"). Instead the deliberative layer is an **LLM agent given a menu of robot abilities** plus the user's request, and it *chains* those abilities to satisfy intent. A representative menu:

| Ability | Kind | Who actually executes it |
|---|---|---|
| `drive(distance)` / `turn(angle)` | one-shot motion | reactive layer, guarded by collision-stop |
| `look(direction)` | orient camera/body | reactive layer |
| `check_distance()` | read ultrasonic | reactive layer (returns now) |
| `enter_follow_mode(target)` | **mode** | reactive tracker (track-by-detection, **pluggable detector**) runs until told to stop |
| `describe_scene()` | capture + reason | deliberative layer (bigger model) |
| `speak(text)` | text-to-speech out (**OUTPUT shipped** — macOS `say`, opt-in `--speak`) | deliberative layer |

The crucial point: **most abilities are modes for the reactive layer, not direct motor twitches.** The model says *"follow that person"*; it does not micro-steer. It sets the goal, and the local tracker does the second-by-second following. The model conducts; the fast layer plays the instrument.

> **THESIS —** The LLM sets high-level INTENT; the reactive layer EXECUTES and ENFORCES SAFETY. Most abilities the model calls are **modes for the reactive layer**, not direct actuator commands. The agent's job is to choose and sequence goals, never to drive the motors directly. I build the toolbox, the AI sequences it, the fast layer plays it — and the AI's freedom is bounded by a small, safe toolbox, which is what makes "do whatever I tell it" tractable on cheap hardware.

This keeps the cloud out of any tight control loop: even a multi-step plan like *"go around the corner and tell me what's there"* decomposes into a few goal-setting tool calls, each handed down to the reactive layer to carry out safely while the model waits for the next still.

> The **concrete tool names, parameters, return shapes, and the intent JSON schema** — the exact ability list — are defined in `software-spec.md`, not here. This doc only fixes the *idea*: a bounded menu, chained by the model, executed by the reactive layer.

---

## 3. The Contract Between the Loops (the crux)

This is the crux of the whole design — the seam where a slow planner meets a fast executor. Three concerns define it: **goal hand-down**, **state report-up**, and **preemption**.

### Goal hand-down (down the seam)

The deliberative layer never streams motor commands. It hands the reactive layer a single **active goal/mode**, not a motor command. Examples of the *kind* of thing it sets (exact schema → `software-spec.md`):

- a **mode**: `FOLLOW`, `IDLE` (exploration is a deliberative loop, not a reactive mode — see `software-spec.md`).
- a **bounded action**: `drive_until(clear=false)`, "drive forward up to 1 m or until blocked," "turn right ~90°."
- a **terminating condition**: keep going *until* X (an obstacle, a timeout, a target lost).

A goal is a *standing intention with a stop condition*, not a one-shot pulse — something the reactive layer can pursue **autonomously for seconds** without further cloud contact. That property is what lets the cloud be slow, and it keeps the robot behaving sensibly during the seconds before the next cloud reply.

### State report-up (back up the seam)

The reactive layer continuously publishes a small **status snapshot** back up so the next deliberative step has fresh ground truth: current mode, whether the last goal **completed / is in-progress / was blocked**, latest ultrasonic distance, and whether a target is still locked / a collision-stop fired. The deliberative layer reads this (plus a fresh still) before it decides the next step. This is how the model learns *"I tried to drive forward but stopped 20 cm from a wall"* and can re-plan or narrate honestly instead of hallucinating success. It does **not** stream raw frames up — see §6.

### Preemption and priority

A new command must be able to **interrupt** one already in flight (you say "stop, come here" while it's mid-explore), and safety must override both. The model is the arbiter of *deliberative* intent, but the reactive layer resolves every tick by a fixed **priority order**, highest first:

```
  PRIORITY  (highest wins, evaluated every reactive tick)
  ┌─────────────────────────────────────────────────────────┐
  │ 1. COLLISION-STOP   reflex — halts wheels, ungated        │
  │ 2. NEW GOAL         latest deliberative goal preempts old │
  │ 3. ACTIVE GOAL      keep executing current mode           │
  │ 4. IDLE / SAFE-STOP  default when no goal is set          │
  └─────────────────────────────────────────────────────────┘
```

A fresh goal from the cloud atomically replaces the current one (last-write wins) — the reactive layer does not queue goals deep; there is no queue in v1, and the user's freshest instruction is always the truth. Collision-stop sits above all of it and cannot be out-voted by any command, new or old: nothing can drive the robot into something the ultrasonic sees.

The hard question is **what happens after a collision-stop fires** mid-goal:

> **DECISION —** After a collision-stop interrupts an active motion goal, the reactive layer halts and holds, marks the goal `BLOCKED`, and **surfaces that state up to the deliberative layer rather than silently resuming or silently aborting.** The robot does not blindly retry into the same obstacle, and it does not freeze forever — it kicks the decision back to the brain, which can re-plan ("there's a wall, turn and go around?"), turn away, or ask the user. For v1, auto-resume is explicitly *off*; resuming requires a fresh deliberative decision, because auto-resume risks grinding into the obstacle the instant the path looks clear. Concretely, **`SAFE_STOP` is sticky**: clearing the obstacle is necessary but not sufficient — the robot stays stopped until the obstacle is gone **and** a fresh intent arrives (see `software-spec.md` §2.3).

> **OPEN —** Whether a *follow* goal should auto-resume after the obstacle clears (likely yes — the human stepped between robot and target) while a *drive/explore* goal should not. Per-mode resume policy is unresolved and deferred to `software-spec.md`.

> **OPEN —** Should a *blocked* state auto-prompt the user ("I'm stuck — what now?") via voice, or wait silently for the next command? Leaning toward a short spoken nudge once voice exists, but the trigger threshold (how long blocked before speaking) is unsettled. Revisit when Phase 3 voice lands.

> The **precise wire format** of goals, status, and preemption signals — field names, enums, timeouts — is `software-spec.md`'s job. This section fixes only the *semantics*: goals down, status up, freshest-human-intent-wins, collision-stop on top.

---

## 4. Safety Override Design

Collision-stop is the one behavior that must work even when everything else is broken — it may never be gated behind the AI or the network. Three implementation models were considered:

| Model | What it is | Verdict for v1 |
|---|---|---|
| **Software priority in the reactive loop** | top-priority distance check each tick; zeroes motors if too close | **Chosen** — simple, fast enough at 10–30 Hz, lives where motors are commanded, no extra parts |
| Watchdog timer / process | independent timer/process safe-stops motors on a stale heartbeat | Adopted as a **lightweight v1 safeguard** against a hung loop |
| Hardware kill path | sensor wired to driver enable / physically cut motor power, no CPU | Overkill for a cardboard v1; revisit for the kid version |

> **DECISION —** Collision-stop is a **software priority at the top of the reactive loop** for v1: every tick reads the ultrasonic, and a too-close reading zeroes the motor outputs before any drive command is applied. It is structurally impossible for a deliberative command to bypass it because the check runs *after* commands are accepted and *before* they reach the motors. Add a lightweight **software watchdog** that safe-stops if the reactive loop itself stalls. Defer any hardware cutoff path to the sturdier kid-version.

Because the safety net is software running on the same Pi as the motor driver, the reactive loop must stay lean and never block — anything that could stall the tick (a synchronous network call, a heavy CV op) is banned from the reactive path.

> **RISK —** A single forward-facing HC-SR04 ultrasonic has a **narrow cone and large blind spots** — side obstacles, low objects below the beam, table edges, and sound-absorbing or angled surfaces can go unseen. Collision-stop protects against head-on obstacles only and is only as good as what the sensor sees; yalp can still clip a chair leg or a wall it approaches at an angle. This is a known v1 limitation accepted for cost/simplicity. Mitigations — sensor placement, cone geometry, a possible second sensor or bumper, speed caps — are detailed in `hardware.md`.

---

## 5. Graceful Degradation When the Cloud Drops

WiFi will hiccup mid-command, and the deliberative layer *is* the cloud. The design requirement is that a dropped connection mid-command must **never** leave the robot driving blind or frozen in a dangerous state. Because motors and safety live locally, a dropped connection degrades *capability*, not *control*: the robot stops accepting new high-level commands but keeps avoiding obstacles and finishing or safely ending its current low-level behavior. The defined behavior:

- The reactive layer **keeps avoiding obstacles** — collision-stop is local and unaffected.
- A bounded in-flight goal is allowed to **finish or safely time out**, then the robot **holds position** (safe-stop) rather than coasting.
- Follow-mode may continue locally (the tracker is on-device) but **safe-stops if it loses the target** and can't get a new goal.
- When connectivity returns, the deliberative layer resumes from fresh state (§3) — it re-reads status and a new still, it does not replay stale commands.

> **DECISION —** On cloud/WiFi loss mid-command, yalp **degrades to a safe local-only mode**: collision-stop and any active local mode (e.g. follow) keep running, but a standing *motion* goal that depends on cloud re-planning **winds down to a safe-stop** within a short timeout rather than driving open-loop forever. It must **safe-stop, never freeze-blind and never coast indefinitely.** New high-level commands simply pause until the network returns; the body stays under reactive control the whole time.

Worked example: mid-way through *"drive forward and tell me what's there,"* WiFi drops. The current `drive_until_clear` goal continues under collision-stop until its stop condition or the cloud-dependency timeout; with no new goal arriving, the robot safe-stops, holds position, and resumes taking commands when the link is back. No wall, no runaway.

---

## 6. Key Simplifying Decisions

Two deliberate simplifications keep v1 cheap, buildable, and within a hobby budget.

### Stills, not video

The deliberative layer sends a **single still photo** per step, not a video stream. Models handle stills fine, stills are far cheaper, and streaming a live feed off a Pi over home WiFi *while* driving motors is a heavy, pricey integration.

> **DECISION —** v1 uses **stills, not video.** One photo per deliberative step. Continuous streaming multimodal — the "smooth mode" that would make the robot narrate and decide continuously instead of in halting bursts — is a **stretch goal, explicitly out of v1**.

### Tiered models

Not every glance deserves a flagship model. The per-step loop ("is the path clear?", "roughly what's ahead?", centering hints) runs on a **fast/cheap VLM**; only genuinely hard requests ("describe this whole scene in detail," "read this sign") escalate to a **bigger model**.

> **DECISION —** The deliberative layer is **tiered across three rungs**, routed by an explicit, auditable table (not a vibe): a fast/cheap model (Haiku) is the **default** for routine per-step VQA / tool-picking; a mid model (Sonnet) handles multi-step `explore` reasoning or a `need_more_reasoning` flag from the cheap tier; a big model (Opus) handles only genuinely hard vision/reading (`describe_scene(detail=full)`, read-text). Paying flagship latency and cost on every step is wasteful and would make the bursts even slower. **Extended/adaptive thinking is capability-gated**: it is attached only on the mid/big tiers that support it — the fast Haiku tier does **not** support it (sending it 400s), so the per-step loop runs without it. Specific (env-overridable) model IDs and the exact escalation triggers belong in `software-spec.md`.

These two together keep the deliberative layer **cheap and fast enough to be fun**: small payloads, small models, and only occasional escalation.

---

## 7. Development Architecture: Build the Brain on the Laptop First

Only **two things genuinely require the Pi**: driving the motors and capturing from the robot's camera. Everything else — the agent loop, the tool menu, prompt design, the VLM calls, the tracker, the goal/status contract, scene description — is plain Python that runs anywhere.

So the brain is built and iterated **on the laptop first**, using the laptop's webcam as a stand-in camera. This decouples software iteration from hardware in a way that matters enormously:

- The laptop edit-test cycle is **instant**; iterating over SSH on a headless Pi is painfully slow.
- You debug **one thing at a time** — Python logic on the laptop, *then* hardware/wiring/Linux gremlins on the Pi — instead of all three at once.
- The whole deliberative path and most of the contract can be exercised **before any motors arrive** (which matters given the body parts ship after the brain).
- The cloud calls and agent logic are identical on laptop and Pi, so almost nothing is throwaway. The reactive layer is designed against a **hardware-abstraction seam**: a fake motor/sensor backend on the laptop, the real `gpiozero`/ultrasonic backend on the Pi, same interface. The marry-to-body step swaps the thin motor/camera adapters, not the logic.

> **DECISION —** Build and iterate the entire vision/agent loop **on the laptop** using the laptop webcam as a stand-in camera. Marry it to the Pi body only at the end. The C270 doubling as a laptop cam makes the stand-in faithful.

```
  LAPTOP (fast iteration)              PI (hardware truth)
  ┌────────────────────────┐          ┌────────────────────────┐
  │ agent loop + tool menu  │  same    │ same agent loop         │
  │ cloud VLM calls         │  code    │ cloud VLM calls         │
  │ webcam (stand-in)       │ ───────► │ C270 capture (real)     │
  │ FAKE motor adapter      │          │ gpiozero motors + HC-SR04│
  └────────────────────────┘          └────────────────────────┘
```

> **THESIS —** Treat the Pi as an **I/O endpoint, not a development environment.** Build the brain on the laptop behind a hardware-abstraction seam; only motor-control and camera-capture cross over to the Pi at the end. This is the cheapest way to keep iteration fast and isolate hardware bugs from logic bugs.

Milestone sequencing — which capability comes online when, and what hardware each step needs relative to part arrivals — is owned by `roadmap.md`. The architectural point here is only that the seam *exists* and the brain lives above it.

---

## 8. Prior Art to Study (and the Trap)

Two open-source robots already solve big chunks of this and are worth a day of reading before writing the conversation loop and the tracker from scratch.

- **LeKiwi** (Hugging Face's LeRobot) — a wheeled mobile base that installs and runs directly on a Raspberry Pi. The closest existing reference for yalp's chassis-and-Pi setup; study how they structure the **Pi-side motor/control code** and their reactive loop.
- **Reachy Mini** (Hugging Face) — a Pi-based robot with camera, mics, and speaker, plus an SDK that integrates with LLMs. It ships a **Conversation App** and a **real-time tracker** — basically the talking/seeing half of yalp's eventual kid version, already built and open. It doesn't roam, so it won't help the explore-the-room half.

The value is in **reading the patterns**, not adopting the stacks wholesale.

> **RISK —** There is a strong temptation to **over-adopt a heavy framework** (the full LeRobot stack) for what is a cardboard v1. A large framework brings dependencies, abstractions, and hardware assumptions that can outweigh the whole build — and it buries the learning that v1 exists to produce. **Steal the patterns (motor control, the follow-me tracker, the conversation loop); do not inherit the framework.** Keep yalp's own code small enough to understand end to end. Re-evaluate adopting a framework only at the sturdier kid version.

---

## Where the rest is specified

- Concrete tool/intent schema, goal and status message shapes → `software-spec.md`
- Sensor placement, blind-spot mitigation, power and wiring → `hardware.md`
- Milestone sequencing and part-arrival ordering → `roadmap.md`
- User-facing behaviors and scope of "do whatever I tell it" → `product-spec.md`

> **OPEN —** This doc fixes concepts and boundaries; the concrete interface contract — tool/intent schema, goal & status wire formats, model picks, timeouts — is deferred to `software-spec.md`. Where the two disagree, `software-spec.md` wins on *mechanism* and this doc wins on *intent*.
