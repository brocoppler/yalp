# yalp — Technical Architecture

> The conceptual backbone of yalp: how a cloud "brain" and an on-board "spinal cord" split the work so the robot stays smart, responsive, and safe at the same time.

---

This document defines the **concepts and boundaries** of yalp's control system. It is deliberately schema-free: the exact tool list, message formats, and intent payloads live in `software-spec.md`. Read this first to understand *why* the system is shaped the way it is, then read `software-spec.md` for the concrete contract. Hardware choices that this design leans on are in `hardware.md`; build sequencing is in `roadmap.md`; the scope envelope is in `product-spec.md`.

## 1. The Core Idea: Two Loops, Not One

yalp runs on **two control loops with different clocks**, not one. They are different because the two halves of the job have fundamentally different time budgets.

- **The fast reactive layer** lives entirely on the Pi, runs at **~10–30 Hz**, and never touches the network. It is reflex and habit: stop before you hit something, keep the person centered, keep driving until a condition flips. It is dumb on purpose, and it is always running.
- **The slow deliberative layer** lives in the **cloud** and runs at **~seconds per step**. It is thought: turn speech and a photo into a high-level intent, pick a mode/goal, describe what the camera sees, and talk back. It is smart, expensive, and laggy.

The deliberative layer decides *what to do*. The reactive layer decides *how to keep doing it safely right now*.

> **DECISION —** yalp is built as two loops: a local reactive layer (~10–30 Hz, no network) and a cloud deliberative layer (~seconds/step). The reactive layer owns the body and safety in real time; the deliberative layer sets high-level intent. This split is the single most important architectural decision in v1 and everything else follows from it.

### Why the split is non-negotiable

A single cloud-routed loop has a fatal flaw: every decision takes a 2–3 second round trip (capture → upload → model → response). During those seconds the robot would be **blind and uncontrolled** — coasting on its last command with nothing watching the ultrasonic. That is precisely how hobby bots drive into walls. Splitting the loops means the body is *never* waiting on the cloud to avoid a collision or to keep tracking a moving target. The thinking happens in bursts; the body keeps moving and stays safe between bursts.

```
                       ┌─────────────────────────────────────────────┐
   voice / text  ─────▶│   SLOW DELIBERATIVE LAYER   (cloud, ~s/step) │
                       │                                             │
                       │   speech-to-text                            │
                       │      └▶ LLM / VLM  (words + ONE still photo) │
                       │           └▶ high-level INTENT + mode/goal   │
                       │           └▶ scene description               │
                       │      ◀┘ text-to-speech ───────▶ spoken reply │
                       └───────────────┬─────────────────────────────┘
                            set goal /  │   ▲  report state
                            mode  ▼     │   │  (progress, blocked,
                       ┌────────────────┴───┴──────────────────────────┐
                       │   FAST REACTIVE LAYER   (Pi, ~10–30 Hz, local)│
                       │                                              │
                       │   camera frames + ultrasonic ─▶ reflexes ─▶ motors
                       │     • collision-stop      (OVERRIDES ALL)    │
                       │     • person-track / center  (follow mode)   │
                       │     • "keep doing X until something changes" │
                       └──────────────────────────────────────────────┘

   The cloud sets intent.  The Pi executes it and keeps the robot safe.
```

## 2. The Agent / Tool-Use Model

yalp does not hand-code a keyword table ("if user says 'forward' then drive"). Instead it uses **AI agents with tool use**: the language model is handed a **menu of the robot's abilities** plus the user's request, and it *chains* the right calls itself. Conceptually the menu looks like:

| Ability (concept) | What it does | Who actually executes it |
|---|---|---|
| `drive` / `turn` | Move a bounded amount or until a condition | reactive layer, under collision-stop |
| `look` | Point the camera / reorient to see | reactive layer |
| `enter-follow-mode` | Lock onto and track a person | reactive layer (local tracker) |
| `check-distance` | Read the ultrasonic | reactive layer |
| `describe-scene` | Caption the current still | deliberative layer (bigger model) |
| `speak` | Say something out loud | deliberative layer (TTS) |

The crucial point: **most abilities are modes for the reactive layer, not direct motor twitches.** The model says *"follow that person"*; it does not micro-steer. It sets the goal, and the local tracker does the second-by-second following. The model conducts; the fast layer plays the instrument.

> **THESIS —** The LLM sets high-level INTENT; the reactive layer EXECUTES and ENFORCES SAFETY. The agent's job is to choose and sequence goals, never to drive the motors directly. This is what makes "do whatever I tell it" tractable on cheap hardware — the AI's freedom is bounded by a small, safe toolbox.

The **concrete tool names, parameters, return shapes, and the intent JSON schema are defined in `software-spec.md`**, not here. This doc only fixes the *idea*: a bounded menu, chained by the model, executed by the reactive layer.

## 3. The Contract Between the Loops

This is the crux of the whole design — the seam where cloud meets body. Three things flow across it.

### Goal down

The deliberative layer hands the reactive layer a **goal or mode**, not a motor command. Examples of the *kind* of thing it sets (exact schema → `software-spec.md`):

- a **mode**: `FOLLOW`, `EXPLORE`, `IDLE`.
- a **bounded action**: "drive forward up to 1 m or until blocked," "turn right ~90°."
- a **terminating condition**: keep going *until* X (an obstacle, a timeout, a target lost).

A goal is something the reactive layer can pursue **autonomously for seconds** without further cloud contact. That property is what lets the cloud be slow.

### State up

The reactive layer continuously reports a small **status** back up so the next deliberative step has fresh ground truth: current mode, whether the last goal **completed / is in-progress / was blocked**, last distance reading, and whether a target is still locked. The deliberative layer reads this (plus a fresh still) before it decides the next step. It does **not** stream raw frames up — see §6.

### Preemption and priority

A new command must be able to **interrupt** one already in flight (you say "stop, come here" while it's mid-explore). The model is the arbiter of *deliberative* intent, but the reactive layer enforces a strict **priority order**, highest first:

```
   1.  COLLISION-STOP        ← reflex; overrides everything, always
   2.  NEW USER COMMAND      ← preempts the current goal
   3.  CURRENT GOAL / MODE   ← the in-progress deliberative intent
   4.  IDLE / hold position  ← default when nothing is active
```

When a new command arrives, the current goal is abandoned and replaced — the reactive layer does not queue goals deep; the freshest human intent wins. Collision-stop sits above even that: no command, new or old, can drive the robot into something the ultrasonic sees.

The hard question is **what happens after a collision-stop fires** mid-goal:

> **DECISION —** After a collision-stop interrupts an active goal, the reactive layer halts and holds, marks the goal `BLOCKED`, and **surfaces that state up to the deliberative layer rather than silently resuming or silently aborting.** The robot does not blindly retry into the same obstacle, and it does not freeze forever — it kicks the decision back to the brain, which can re-plan ("there's a wall, turn and go around?") or ask the user. For v1, auto-resume is explicitly *off*; resuming requires a fresh deliberative decision.

> **OPEN —** Should a *blocked* state auto-prompt the user ("I'm stuck — what now?") via voice, or wait silently for the next command? Leaning toward a short spoken nudge once voice exists, but the trigger threshold (how long blocked before speaking) is unsettled. Revisit when Phase 3 voice lands.

The **precise wire format** of goals, status, and preemption signals — field names, enums, timeouts — is `software-spec.md`'s job. This section fixes only the *semantics*: goals down, status up, freshest-human-intent-wins, collision-stop on top.

## 4. Safety Override Design

Collision-stop is the one behavior that must work even when everything else is broken. Three models were on the table:

| Model | What it is | Verdict for v1 |
|---|---|---|
| **Software priority in the loop** | The reactive loop checks distance every tick and zeroes the motors if too close | **Chosen** — simple, fast enough at 10–30 Hz, no extra parts |
| Watchdog timer | A separate timer cuts motors if the loop stops ticking | Good *complement* later; defends against a hung loop |
| Hardware kill path | Sensor wired to physically cut motor power | Overkill for a cardboard v1; revisit for the kid version |

> **DECISION —** Collision-stop is a **software priority at the top of the reactive loop** for v1: every tick reads the ultrasonic, and a too-close reading zeroes the motor outputs before any drive command is applied. It is structurally impossible for a deliberative command to bypass it because the check runs *after* commands are accepted and *before* they reach the motors. A hardware kill path and a watchdog are deferred to the sturdier kid-version.

Because the safety net is software running on the same Pi as the motor driver, the reactive loop must stay lean and never block — anything that could stall the tick (a synchronous network call, a heavy CV op) is banned from the reactive path.

> **RISK —** A single forward-facing ultrasonic has a **narrow cone and large blind spots** — to the sides, below the beam, and for sound-absorbing or angled surfaces. Collision-stop protects against head-on obstacles only; yalp can still clip a chair leg or a wall it approaches at an angle. This is a known v1 limitation accepted for cost/simplicity. Sensor placement, cone geometry, and a possible multi-sensor or bumper upgrade are detailed in `hardware.md`.

## 5. Graceful Degradation When the Cloud Drops

WiFi will hiccup, and the deliberative layer *is* the cloud. The design requirement is that a dropped connection mid-command must **never** leave the robot driving blind or frozen in a dangerous state.

Because the reactive layer is fully local, it keeps running when the network dies. What changes is only that **no new high-level goals arrive**. The defined behavior:

- The reactive layer **keeps avoiding obstacles** — collision-stop is local and unaffected.
- A bounded in-flight goal is allowed to **finish or safely time out**, then the robot **holds position** (safe-stop) rather than coasting.
- Follow-mode may continue locally (the tracker is on-device) but **safe-stops if it loses the target** and can't get a new goal.
- When connectivity returns, the deliberative layer resumes from fresh state (§3) — it re-reads status and a new still, it does not replay stale commands.

> **DECISION —** On cloud/WiFi loss mid-command, yalp **degrades to a safe local-only mode**: keep avoiding obstacles, complete or time-out the current bounded goal, then hold position. It must **safe-stop, never freeze-blind and never coast indefinitely.** New high-level commands simply pause until the network returns; the body stays under reactive control the whole time.

## 6. Key Simplifying Decisions

Two deliberate simplifications keep v1 cheap, buildable, and within a hobby budget.

### Stills, not video

The deliberative layer sends a **single still photo** per step, not a video stream. Models handle stills fine, stills are far cheaper, and streaming a live feed off a Pi over home WiFi *while* driving motors is a heavy, pricey integration.

> **DECISION —** v1 uses **stills, not video.** One photo per deliberative step. Continuous streaming multimodal — the "smooth mode" that would make the robot narrate and decide continuously instead of in halting bursts — is a **stretch goal, explicitly out of v1**.

### Tiered models

Not every glance deserves a flagship model. The per-step loop ("is the path clear?", "roughly what's ahead?", centering hints) runs on a **fast/cheap VLM**; only genuinely hard requests ("describe this whole scene in detail," "read this sign") escalate to a **bigger model**.

> **DECISION —** The deliberative layer is **tiered**: a fast/cheap vision model for the routine per-step loop, a larger model only for hard scene-understanding requests. This keeps per-step latency and cost low without giving up capability where it matters. Specific model picks and the escalation trigger belong in `software-spec.md`.

## 7. Development Architecture: Build the Brain on the Laptop

Only **two things genuinely require the Pi**: driving the motors and capturing from the robot's camera. Everything else — the agent loop, tool dispatch, the VLM calls, the tracker, the goal/status contract — is plain Python that runs anywhere.

So the brain is built and iterated **on the laptop first**, using the laptop's webcam as a stand-in camera. This decouples software iteration from hardware in a way that matters enormously:

- The laptop edit-test cycle is **instant**; iterating over SSH on a headless Pi is painfully slow.
- You debug **one thing at a time** — Python logic on the laptop, *then* hardware/Linux gremlins on the Pi — instead of all three at once.
- The whole deliberative path and most of the contract can be exercised **before any motors arrive** (which matters given the body parts ship after the brain).
- The reactive layer is designed against a **hardware-abstraction seam**: a fake motor/sensor backend on the laptop, the real `gpiozero`/ultrasonic backend on the Pi, same interface. The marry-to-body step swaps the backend, not the logic.

> **THESIS —** Treat the Pi as an **I/O endpoint, not a development environment.** Build the brain on the laptop behind a hardware-abstraction seam; only motor-control and camera-capture cross over to the Pi at the end. This is the cheapest way to keep iteration fast and isolate hardware bugs from logic bugs.

Milestone sequencing — which capability comes online when, and what hardware each step needs — is owned by `roadmap.md`. The architectural point here is only that the seam *exists* and the brain lives above it.

## 8. Prior Art to Study (and the Trap)

Two open-source robots already solve big chunks of this. Read them before writing the conversation loop and the tracker from scratch.

- **LeKiwi** (Hugging Face's LeRobot) — a wheeled mobile base that installs and runs directly on a Raspberry Pi. The closest existing reference for yalp's chassis-and-Pi setup; study how they structure the **Pi-side motor/control code** and their reactive loop.
- **Reachy Mini** (Hugging Face) — a Pi-based robot with camera, mics, and speaker, plus an SDK that integrates with LLMs. It ships a **Conversation App** and a **real-time tracker** — basically the talking/seeing half of yalp's eventual kid version, already built and open. It doesn't roam, so it won't help the explore-the-room half.

The value is in **reading the patterns**, not adopting the stacks wholesale.

> **RISK —** There is a strong temptation to **over-adopt a heavy framework** (the full LeRobot stack) for what is a cardboard v1. A large framework brings dependencies, abstractions, and an opinionated structure that can outweigh the whole build — and it buries the learning that v1 exists to produce. **Steal the patterns (motor control, the tracker, the conversation loop); do not inherit the framework.** Keep yalp's own code small enough to understand end to end.

---

> **OPEN —** This doc fixes concepts and boundaries; the concrete interface contract — tool/intent schema, goal & status wire formats, model picks, timeouts — is deferred to `software-spec.md`. Where the two disagree, `software-spec.md` wins on *mechanism* and this doc wins on *intent*.
