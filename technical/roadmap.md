# Roadmap

> Build sequencing, the risk register, and the consolidated open questions for yalp v1 — the one place where every DECISION, RISK, and OPEN call lives.

---

This doc is the project's spine. It orders the work smallest-win-first, inserts the
go/no-go gates the raw milestone list hides, ranks what will actually bite, and
collects every pending decision so nothing rots in a side conversation. For *what*
each layer does see `architecture.md`; for *how* the code is shaped see
`software-spec.md`; for the parts and power see `hardware.md`; for who it's for see
`product-spec.md`.

> **THESIS —** The hard part of this robot is not the motors, the camera, or the
> cloud model — each of those is a solved, well-trodden hobby problem. The hard part
> is the *seam* between the fast local loop and the slow cloud loop. The sequencing
> below is built to de-risk that seam first and earliest, on the laptop, before any
> hardware can hide it.

---

## 1. The build path (smallest win first)

The milestones, ordered so each step stands alone and you can stop and play at any
point. The **magic moment** — the first time the thing feels alive — lands at step 4,
which is reachable on the bench with only the Phase 1 hardware already in hand.

| # | Milestone | What "done" looks like | Needs hardware | Magic? |
|---|---|---|---|---|
| 1 | **Hello motors** | Drive the wheels from Python: forward, turn, stop | Phase 2 (motors, driver, battery) | |
| 2 | **Safety reflex** | Ultrasonic collision-stop — a fast local loop that overrides any drive command when something's too close | Phase 2 (HC-SR04) | |
| 3 | **Hello eyes** | Capture a photo and save it | Phase 1 only (C270) | |
| 4 | **It sees and talks** | Photo → vision model "what do you see?" → print the answer | Phase 1 only | ⭐ **the magic moment** |
| 5 | **It acts** | Agent loop: command + ability menu → model picks high-level intent → sets a goal for the reactive layer | Phase 1 to build, Phase 2 to drive | |
| 6 | **It follows / explores** | Person-following as a **local** tracker (no cloud round-trip), obstacle-stop still underneath | Phase 2 + a passing fps gate | |
| 7 | **It listens and speaks** *(separate track)* | STT + TTS bolted on **after** the text loop works end-to-end | Phase 3 (mic + speaker) | |

> **DECISION —** Voice (step 7) is a *separate track*, not step 7-of-7-in-sequence.
> It starts only after the full text loop (steps 1–6) works, and it is allowed to lag
> indefinitely. The voice rabbit hole — mic quality, audio over USB, STT/TTS latency,
> wake-words — must never block the core magic.

> **DECISION —** The magic moment is step 4, and step 4 needs *no body*. Bench the
> vision path first. This is what keeps motivation high while the Phase 2 parts ship.

---

## 2. Sequencing refinements

The raw milestone list is correct but incomplete: it hides three things that will
sink the project if discovered late. Two are *gates* (go/no-go checks) and one is a
*parallel track* that costs nothing to start now.

### 2.1 Design the loop-to-loop contract in parallel (steps 1–4)

> **DECISION —** Start the loop-to-loop *contract* — the intent/mode schema, the
> safety-preemption rules, and the shared-state shape — on the laptop **immediately**,
> in parallel with steps 1–4. It is pure software, needs zero hardware, and it
> dictates what the motor API (step 1) and the vision/agent API (steps 4–5) must
> expose. Define the contract *before* building to it, not after.

The contract is the artifact that the whole two-loop architecture stands on. If the
motor code and the agent loop are built against two different mental models of "what
is a mode," step 5 becomes a rewrite. Writing it first turns steps 1, 2, 5, and 6
into implementations of an agreed interface instead of negotiations. See
`software-spec.md` for the schema and `architecture.md` for the preemption model.

### 2.2 Person-detector fps spike — a GO/NO-GO gate before step 6

> **DECISION —** Before committing to follow-mode (step 6) as imagined, run an early
> **person-detector fps spike** on the actual Pi 5: how many frames per second can it
> sustain detecting/tracking a person with no NPU? This is a gate. If the number is
> too low for a moving target, follow-mode forks to a cheaper blob/color tracker or a
> track-by-detection scheme *before* any follow code is written — not after it stutters.

The Pi 5 has no NPU (the AI HAT is deliberately skipped). Whether a real person
*detector* runs fast enough, or whether follow-mode must fall back to a lightweight
blob tracker, is the single biggest unknown in the reactive layer. Find out cheaply,
early, on real hardware. See `software-spec.md`.

### 2.3 Power / brownout bring-up — a gate before motors + Pi run together

> **RISK —** Motors and a Pi 5 sharing a power story is the #1 cause of mysterious
> reboots in hobby robots. Motor inrush and flyback drag the rail down and the Pi
> brownouts mid-command.

> **DECISION —** Insert a **power bring-up gate** before steps 1–2 ever run with the
> Pi attached: separate motor and Pi supplies, common ground, decoupling caps, flyback
> protection, and a deliberate "drive the motors hard while watching for under-voltage
> warnings" smoke test. Motors do not move under the Pi's control until this passes.
> See `hardware.md`.

### 2.4 The refined sequence

Original milestones with the gates and the parallel track woven in:

| Order | Item | Type | Where |
|---|---|---|---|
| A | **Loop-to-loop contract** (intent/mode schema, preemption, shared state) | Parallel track — starts now, laptop-only | `software-spec.md`, `architecture.md` |
| B | Step 3 — Hello eyes (capture a photo) | Milestone — bench, Phase 1 | `software-spec.md` |
| C | Step 4 — **It sees and talks** ⭐ | Milestone — bench, Phase 1 | `software-spec.md` |
| D | Step 5 — It acts (agent loop) | Milestone — built on laptop with webcam stand-in | `software-spec.md` |
| E | **Power / brownout bring-up** | 🚦 Gate — blocks motors-under-Pi | `hardware.md` |
| F | Step 1 — Hello motors | Milestone — needs Phase 2 + gate E | `hardware.md` |
| G | Step 2 — Safety reflex (collision-stop) | Milestone — in *before* autonomous driving | `architecture.md`, `hardware.md` |
| H | **Person-detector fps spike** | 🚦 Gate — decides step 6's shape | `software-spec.md` |
| I | Step 6 — It follows / explores | Milestone — gated by H | `software-spec.md` |
| J | Step 7 — It listens and speaks | Separate track — after I | `software-spec.md`, `product-spec.md` |

Note the order: the contract (A) and the vision path (B–D) run on the laptop while
Phase 2 parts ship; the body steps (F–G) only begin once parts arrive *and* the power
gate (E) passes; follow-mode (I) waits behind its fps gate (H).

---

## 3. Current status

> **Status as of this writing:** Phase 1 (the brain) is in hand; the body and voice
> are not yet bought.

- **Spent (~$200):** the CanaKit Raspberry Pi 5 4GB Starter Kit PRO (board, 27W PD
  wall supply, active-cooling case, 128GB OS card) plus the Logitech C270 webcam.
- **Remaining (~$135):** microSD card reader (if no laptop SD slot), Phase 2 body
  parts (~$105), Phase 3 voice (~$20). Total project ~$335.
- **Phase 2 deferred:** motors, driver, ball caster, HC-SR04, battery + holder,
  breadboard/wiring, glue/zip-ties — not yet ordered.
- **Phase 3 deferred:** USB mic + small speaker.

**What the first sessions actually are:**

1. Flash the kit's card headless (WiFi + SSH baked in), bring the Pi up over SSH.
2. **Bench steps 3–4** — capture a photo → "it sees and talks." This is reachable
   today with only Phase 1 hardware, and it's the magic moment.
3. **In parallel on the laptop:** design the loop-to-loop contract (§2.1) and build
   the agent loop (step 5) using the laptop webcam as a stand-in camera.
4. **When Phase 2 arrives:** run the power gate (§2.3), then steps 1–2 (motors, then
   the safety reflex *before* any autonomous driving).

> **THESIS —** Develop the brain on the laptop, not the Pi. Only motor-control and
> camera-capture genuinely need the Pi. Iterating the vision/agent loop over SSH on a
> headless Pi is painfully slow — and there's no reason to debug Python *and* hardware
> *and* Linux all at once. See `software-spec.md`.

---

## 4. Risk register

Ranked by how much each will actually bite — top of the table is where to spend worry.

| # | Risk | Why it bites | Severity | Mitigation / where addressed |
|---|---|---|---|---|
| 1 | **The loop-to-loop interface is the hard part and the least specified** | Intent/mode schema, safety preemption, and shared state are the seam the whole two-loop design rests on. If motors and the agent loop are built against different mental models, step 5 becomes a rewrite. | 🔴 Critical | Design the contract first, in parallel, laptop-only (§2.1). `software-spec.md` |
| 2 | **Person-following physics on a Pi 5 with no NPU** | A real person *detector* may not hit usable fps without an accelerator; follow-mode could stutter and lose a moving target. Forces a blob-vs-detector fork and track-by-detection. | 🔴 Critical | Early fps spike as a go/no-go gate before writing follow code (§2.2). `software-spec.md` |
| 3 | **A single front ultrasonic is thin** | Narrow cone, side/rear blind spots, and silent failures on glass and soft/angled surfaces. One sensor can't see the obstacle it's about to clip. | 🟠 High | Define collision-stop priority + post-collision behavior; budget 2–3 sensors. `hardware.md`, `architecture.md` |
| 4 | **No encoders = open-loop control** | Without wheel encoders, distance and heading are guesses; the robot drifts and "back up 30cm" is dead reckoning. Vision closes the loop only in follow mode. | 🟠 High | Accept open-loop for v1; lean on vision feedback in follow mode; encoders are a post-v1 upgrade. `hardware.md` |
| 5 | **Power / brownout** | Motor inrush and flyback drag the shared rail down and the Pi reboots mid-command — the #1 cause of mysterious resets. | 🟠 High | Separate supplies + common ground, decoupling caps, flyback protection, and a bring-up gate before motors run under the Pi (§2.3). `hardware.md` |
| 6 | **Voice is the headline goal but the most-deferred, least-specified track** | The product's whole pitch is "talk to it," yet voice is Phase 3 and barely specced. Far-field STT on a cheap mic, over USB, near motor noise is genuinely hard. | 🟡 Medium | Keep it a separate track after the text loop works; spec mic/STT/TTS only when reached. `software-spec.md`, `product-spec.md` |
| 7 | **Graceful degradation on WiFi loss** | The deliberative layer needs WiFi; if it drops mid-roam the robot must not go haywire. The reactive layer has to safe-stop and keep collision-stop alive on its own. | 🟡 Medium | Reactive layer is network-independent by design; on cloud loss, stop taking new high-level commands and hold/safe-stop. `architecture.md`, `software-spec.md` |

> **RISK —** Risks 1 and 2 are the ones that can invalidate the *design*, not just
> cost time — which is exactly why §2 front-loads work against both before any code
> commits to them. Everything below #2 is a known engineering cost, not an unknown.

---

## 5. Consolidated open questions

Every pending decision, pulled into one place so the team can see the whole backlog
at once rather than rediscovering it scattered across the sibling docs.

> **OPEN —** **Final sensor count.** One front HC-SR04 for v1, or budget 2–3 from the
> start (front + corners) to cover the blind spots in risk #3? Affects the Phase 2
> order and the collision-stop logic. → `hardware.md`, `architecture.md`

> **OPEN —** **Follow-mode tracker choice.** Pending the §2.2 fps spike: full person
> detector, lightweight blob/color tracker, or track-by-detection hybrid? Drives the
> whole shape of step 6. → `software-spec.md`

> **OPEN —** **Model / provider choice.** Which vision-language model and provider for
> the per-step loop vs. the escalation model, and what's the tiering threshold? Cost,
> latency, and SDK all hang on this. → `software-spec.md`, `architecture.md`

> **OPEN —** **Voice timing and approach.** When does the voice track actually start,
> and is far-field STT in scope for v1 or punted to the kid version? → `software-spec.md`,
> `product-spec.md`

> **OPEN —** **How much should v1 anticipate the kid version?** Where do we spend now
> to avoid a rewrite later (interfaces, modes) versus deliberately throwaway cardboard
> scaffolding? → `product-spec.md`

> **OPEN —** **Post-collision behavior.** After a collision-stop fires, what then —
> back off, rotate to find clear space, halt and ask the deliberative layer? Open-loop
> control (risk #4) makes "back up" a guess. → `architecture.md`, `hardware.md`

> **OPEN —** **Roaming power budget.** Which PD power bank, and does the ~1–1.5h
> runtime estimate survive motors + Pi + camera under real load? → `hardware.md`

---

## 6. Future / stretch (post-v1)

Explicitly out of scope for v1, recorded so good ideas don't get re-litigated as if
they were on the critical path.

- **Kid-friendly enclosure** — cute, durable, safe; small parts and lithium handled
  responsibly. This is the v1 → kid-version transition. See `product-spec.md`.
- **Smooth mode** — real-time streaming multimodal so the deliberative layer narrates
  and decides continuously instead of in photo-by-photo bursts. Heavier and pricier
  over home WiFi while driving motors. See `architecture.md`.
- **Better navigation** — depth camera or LiDAR + SLAM for reliable "go to the
  kitchen." This is what unlocks named-place navigation, which v1 explicitly punts.
- **A stronger on-device tracker** — smoother person-following and the beginnings of
  room-mapping, beyond what the v1 blob/detector fork can do.
- **Onboard AI (e.g. Jetson Orin Nano)** — move inference on-device to cut latency and
  the cloud dependency. Directly attacks risks #1, #2, and #7.
- **The dream: balance-on-a-ball.** A ballbot that actually balances — a hard control
  problem deliberately saved for last, after the wheeled base is boring and reliable.

> **THESIS —** Every stretch item maps to a v1 limitation we chose on purpose. v1
> exists to make the two-loop architecture real and boring; the stretch list is what
> gets exciting *only once that foundation holds*.
