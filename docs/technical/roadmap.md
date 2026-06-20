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

## 1. The build path — one canonical ladder

One sequence, ordered smallest-win-first. Every rung has a **self-certifiable
done-signal** — something you can *run or measure*, not just write — so you always
know whether a rung is green without opening another doc. The **magic moment** — the
first time the thing feels alive — lands at **C**, reachable on the bench with only
the Phase 1 hardware already in hand.

> **Progress: 0 of 16 green.** Next up: **0** (order Phase 2 parts), fired the day
> **A** (the loop contract) starts. Update this line as each done-signal goes green.

| Step | Milestone / gate | Done-signal — self-certify with exactly this | Needs | ⭐ |
|---|---|---|---|---|
| **0** | **Order Phase 2 parts now** | Phase 2 order placed (motors, driver, ball caster, HC-SR04, divider resistors, battery + holder, breadboard/wiring, 470–1000 µF + 0.1 µF caps, glue/zip-ties) the day Step A starts; order/tracking numbers recorded. Parts ship in parallel with laptop work. | Laptop / none | |
| **A** | **Loop-to-loop contract** (intent/mode schema, preemption, shared state) | An agent stub and a fake reactive stub exchange **one hand-authored Intent and one RobotState over the REAL socket**, both printed to console. Documentation alone is **NOT** done. (Run `scripts/contract_demo.py` — prints the handshake and 'STEP A OK'.) | Laptop | |
| **B** | **Hello eyes** — capture a photo | Photo captured and saved to disk; file opens and shows the scene. | Phase 1 (C270 / laptop cam) | |
| **C** | **It sees and talks** | Photo → vision model "what do you see?" → the answer prints to console. | Phase 1 | ⭐ **the magic moment** |
| **D** | **It acts** (agent loop) — three checkpoints | **D1:** model calls ONE tool and the fake backend prints the tool call. **D2:** a multi-step plan that reads RobotState back between steps. **D3:** the full agent loop runs on the laptop webcam. Done = **D3** green. | Laptop (webcam stand-in) | |
| **E** | **Laptop integration checkpoint** | The full agent loop drives the **FAKE** robot through a scripted scene end-to-end (command → Intent over the socket → fake reactive executes → RobotState updates → goal completes); the transcript prints/logs cleanly. Last all-software green before hardware. (Run `scripts/agent_demo.py` — drives the fake robot end-to-end and prints 'AGENT LOOP OK'.) | Laptop | |
| **F** | 🚦 **Gate E — Power / brownout bring-up** | PASS = **no Pi resets** AND `vcgencmd get_throttled` **stays 0x0** under a hard, stall-heavy motor drive script AND measured **motor-rail voltage stays above the driver's logic VIH**. NO-GO recovery (part of this milestone): add 470–1000 µF bulk + 0.1 µF ceramic across VM, twist/shorten motor leads, switch to NiMH cells, re-test. Motors do not move under Pi control until this passes. | Pi 5 + Phase 2 | |
| **G** | **GPIO first light** | On the real Pi 5, blink one LED / toggle one motor-driver input pin via **gpiozero**; confirm gpiozero reports the **lgpio** pin factory and that **no RPi.GPIO is anywhere in the import path** (RPi.GPIO does not work on Pi 5). Verify with a meter. | Pi 5 (+ one LED) | |
| **H** | **Hello motors** | Drive the wheels from Python through the driver: forward, turn, stop. | Pi 5 + Phase 2 + F + G | |
| **I** | **HC-SR04 resistor-divider bring-up** | Build the ECHO divider; **meter the 3.3 V tap and confirm ≤ 3.3 V BEFORE it touches any GPIO pin**; then read one sane distance. | Pi 5 + HC-SR04 + resistors | |
| **J** | **Safety reflex** (collision-stop) | A fast local loop overrides any drive command when something's too close: commanded forward drive halts within the threshold distance on the bench. In *before* any autonomous driving. | Pi 5 + Phase 2 | |
| **K** | 🚦 **Combined-load gate** (NEW) | **Reactive-tick p99 latency < 33 ms** with tracker + detector + capture + motor writes **all live simultaneously**; record the config. NO-GO recovery: drop detector cadence/resolution, move detection off the tick onto a slower thread feeding the tracker, re-measure. | Pi 5 + Phase 2 | |
| **L** | 🚦 **Gate H — Person-detector fps spike** | Measure **SUSTAINED** detector fps at the chosen detection resolution (**~320×240, downscaled before inference**), **CONCURRENT with the reactive loop + camera capture + a motor-PWM stress load — never in isolation**; record the triple **(model, resolution, runtime)** with the number. Try **ONNX Runtime or ncnn with int8**, not just OpenCV DNN. PASS: **≥ 3 Hz sustained = GO** (track-by-detection: cheap tracker re-seeded by the detector). NO-GO: **≤ 1–2 Hz** → ship the blob/color tracker as a first-class deliverable (milestone **M**, own done-signal) and defer robust follow. | Pi 5 + Phase 2 | |
| **M** | **It follows / explores** (local tracker, no cloud round-trip; collision-stop underneath) | **GO branch** (Gate H ≥ 3 Hz): track-by-detection — robot keeps a walking person centered on the bench loop. **NO-GO branch** (Gate H ≤ 1–2 Hz): the blob/color tracker as a **first-class deliverable** — robot follows a colored target, collision-stop underneath, own bench demo. A NO-GO is a *different build, not a demotion*. EXPLORE behavior per `architecture.md` / `software-spec.md`. | Pi 5 + Phase 2 + L (+ K) | |
| **N** | **WiFi-degradation test** (gated milestone) | Kill the link **mid-DRIVE_GOAL** and **mid-FOLLOW** and assert: DRIVE_GOAL **safe-stops within the bounded-goal timeout**; FOLLOW **continues locally then safe-stops on target loss**. Both asserts logged green. | Pi 5 + Phase 2 | |
| **O** | **It listens and speaks** *(separate track)* | STT + TTS bolted on **after** the text loop (0–N) works end-to-end. Allowed to lag indefinitely. | Phase 3 (mic + speaker) | |

> **DECISION —** **Item 0 is real work, not a footnote.** Ordering Phase 2 fires the
> day Step A starts, so motors, driver, HC-SR04, caps, and battery ship while the
> laptop phase (A–E) runs. There was previously no ordering step anywhere; this is it.

> **DECISION —** **Step A's done-signal is runnable, not written.** The contract is the
> single highest-risk, currently-unobservable task in the project. Forcing it to
> produce a live Intent/RobotState round-trip over the real socket converts it into the
> project's *first green check* instead of a doc nobody can falsify. See `software-spec.md`
> for the schema and `architecture.md` for the preemption model.

> **DECISION —** The magic moment is **C**, and it needs *no body*. Bench the vision
> path first; this is what keeps motivation high while the Phase 2 parts ship.

> **DECISION —** Voice (**O**) is a *separate track*, not a final step-in-sequence. It
> starts only after the full text loop (0–N) works, and it is allowed to lag
> indefinitely. The voice rabbit hole — mic quality, audio over USB, STT/TTS latency,
> wake-words — must never block the core magic.

> Legacy footnote: the original plan used an unordered 1–7 milestone list (Hello motors,
> Safety reflex, Hello eyes, It sees and talks, It acts, It follows, It listens). It is
> retired in favor of the single A–O ladder above; the seven intents survive inside it.

---

## 2. Gates, contracts & recovery paths

The raw milestone list was correct but incomplete: it hid the gates that sink hobby
robots when discovered late, and it let the riskiest task (the contract) hide behind
prose. This section gives the *why* behind the rungs above; the *pass/fail* criteria
live inline in the §1 table so you can self-certify without leaving the page.

### 2.1 The loop-to-loop contract must *run*, not just exist (Step A)

> **DECISION —** Start the loop-to-loop *contract* — the intent/mode schema, the
> safety-preemption rules, and the shared-state shape — on the laptop **immediately**,
> in parallel with B–E. It is pure software, needs zero hardware, and it dictates what
> the motor API (H) and the vision/agent API (C–D) must expose. Define the contract
> *before* building to it — and **prove it with a live socket round-trip** (Step A's
> done-signal), not a markdown file.

The contract is the artifact the whole two-loop architecture stands on. If the motor
code and the agent loop are built against two different mental models of "what is a
mode," **D** becomes a rewrite. A runnable stub-to-stub Intent/RobotState exchange
turns A, H, J, and M into implementations of an agreed, *demonstrated* interface
instead of negotiations. See `software-spec.md` and `architecture.md`.

### 2.2 Gate H — person-detector fps spike (decides M's shape)

> **DECISION —** Before committing to follow-mode (**M**), run the **person-detector
> fps spike** on the actual Pi 5 under load. Measure SUSTAINED fps at ~320×240
> (downscaled before inference), **concurrent with the reactive loop + camera capture +
> a motor-PWM stress load — never in isolation** — and record the triple (model,
> resolution, runtime). Try ONNX Runtime or ncnn with int8, not just OpenCV DNN.
> **≥ 3 Hz sustained = GO** (track-by-detection). **≤ 1–2 Hz = NO-GO.**

The Pi 5 has no NPU (the AI HAT is deliberately skipped). Whether a real *detector*
runs fast enough, or whether follow-mode must fall back, is the single biggest unknown
in the reactive layer. The NO-GO path is **not a dead end**: the blob/color tracker is
specced as a **first-class deliverable** with its own bench demo (milestone **M**,
NO-GO branch). A low number means you ship a *different build* — a colored-target
follower with collision-stop underneath — not a demoted one. See `software-spec.md`.

### 2.3 Gate E — power / brownout bring-up (blocks motors-under-Pi)

> **RISK —** Motors and a Pi 5 sharing a power story is the #1 cause of mysterious
> reboots in hobby robots. Motor inrush and flyback drag the rail down and the Pi
> brownouts mid-command.

> **DECISION —** Insert the **power bring-up gate (Gate E, milestone F)** before any
> motor moves under the Pi: separate motor and Pi supplies, common ground, decoupling
> caps, flyback protection. PASS = **no Pi resets** AND `vcgencmd get_throttled`
> **stays 0x0** under a hard, stall-heavy drive script AND **motor-rail voltage stays
> above the driver's logic VIH**. The **NO-GO recovery is part of the milestone**, not
> a fork: add 470–1000 µF bulk + 0.1 µF ceramic across VM, twist/shorten motor leads,
> switch to NiMH cells, and re-test until it passes. See `hardware.md`.

### 2.4 Combined-load gate (NEW) — the seam under real simultaneous load

> **DECISION —** Before trusting the reactive layer in motion, prove the tick holds
> under everything at once: **reactive-tick p99 latency < 33 ms with tracker +
> detector + capture + motor writes all live simultaneously** (milestone **K**). The
> NO-GO recovery is a known sub-task: drop detector cadence/resolution, move detection
> off the tick onto a slower thread that only re-seeds the tracker, and re-measure.
> This catches the failure that Gates E and H each miss in isolation — the seam jams
> only when all four consumers fight for the same core. See `software-spec.md`.

### 2.5 Pi 5 GPIO reality — first light & the HC-SR04 divider

> **DECISION —** **GPIO first light (milestone G)** comes *before* Hello motors. Blink
> one LED / toggle one pin via **gpiozero**, confirm the **lgpio** backend is the
> active pin factory, and confirm **no RPi.GPIO is in the import path** — RPi.GPIO does
> not work on Pi 5 and will fail silently or wrongly. Debug the toolchain on an LED, not
> on a spinning motor.

> **DECISION —** The **HC-SR04 resistor-divider bring-up (milestone I)** is its own
> tiny checkable milestone: HC-SR04 ECHO is 5 V and the Pi's GPIO is 3.3 V-only.
> **Meter the divided tap and confirm ≤ 3.3 V BEFORE it ever touches a GPIO pin.** A
> mis-wired divider is a quiet way to kill a Pi input; metering first makes it a
> two-minute check instead of a mystery. See `hardware.md`.

### 2.6 WiFi-degradation is a gated test, not an assumption (milestone N)

> **DECISION —** Graceful degradation on WiFi loss gets a **gated milestone with an
> observable done-signal (N)**, not a hand-wave. Kill the link mid-DRIVE_GOAL and
> mid-FOLLOW; assert DRIVE_GOAL **safe-stops within the bounded-goal timeout** and
> FOLLOW **continues locally then safe-stops on target loss**. The reactive layer is
> network-independent by design — this milestone *proves* it. See `architecture.md`
> and `software-spec.md`.

---

## 3. Current status

> **Status as of this writing:** Phase 1 (the brain) is in hand; the body and voice
> are not yet bought. **0 of 16 rungs green.**

- **Spent (~$200):** the CanaKit Raspberry Pi 5 4GB Starter Kit PRO (board, 27W PD
  wall supply, active-cooling case, 128GB OS card) plus the Logitech C270 webcam.
- **Remaining (~$135):** microSD card reader (if no laptop SD slot), Phase 2 body
  parts (~$105), Phase 3 voice (~$20). Total project ~$335.
- **Phase 2 deferred — order at milestone 0:** motors, driver, ball caster, HC-SR04,
  divider resistors, battery + holder, breadboard/wiring, bulk + ceramic caps,
  glue/zip-ties.
- **Phase 3 deferred:** USB mic + small speaker.

**What the first sessions actually are:**

1. **Fire milestone 0** — place the Phase 2 order *the day Step A starts*, so parts
   ship in parallel with the laptop work below.
2. Flash the kit's card headless (WiFi + SSH baked in), bring the Pi up over SSH.
3. **Laptop phase (A–E):** prove the loop contract with a live socket round-trip (A),
   then bench **B–C** ("it sees and talks," the magic moment), build the agent loop in
   three checkpoints (**D1–D3**), and close with the fake-robot integration run (**E**).
4. **When Phase 2 arrives:** run the power gate (**F**), then GPIO first light (**G**),
   Hello motors (**H**), the HC-SR04 divider bring-up (**I**), and the safety reflex
   (**J**) *before* any autonomous driving — then the load/fps gates (**K**, **L**),
   follow (**M**), and the WiFi-degradation test (**N**).

> **THESIS —** Develop the brain on the laptop, not the Pi. Only motor-control and
> camera-capture genuinely need the Pi. Iterating the vision/agent loop over SSH on a
> headless Pi is painfully slow — and there's no reason to debug Python *and* hardware
> *and* Linux all at once. See `software-spec.md`.

---

## 4. Risk register

Ranked by how much each will actually bite — top of the table is where to spend worry.

| # | Risk | Why it bites | Severity | Mitigation / where addressed |
|---|---|---|---|---|
| 1 | **The loop-to-loop interface is the hard part and the least specified** | Intent/mode schema, safety preemption, and shared state are the seam the whole two-loop design rests on. If motors and the agent loop are built against different mental models, **D** becomes a rewrite. | 🔴 Critical | Make Step A's done-signal a **runnable** Intent/RobotState round-trip over the real socket — the project's first green check (§2.1). `software-spec.md` |
| 2 | **Person-following physics on a Pi 5 with no NPU** | A real person *detector* may not hit usable fps without an accelerator; follow-mode could stutter and lose a moving target. | 🔴 Critical | **Gate H (milestone L)** under concurrent load decides the build; both branches — track-by-detection (GO) and the blob/color tracker (NO-GO) — are first-class deliverables (M) (§2.2). `software-spec.md` |
| 3 | **A single front ultrasonic is thin** | Narrow cone, side/rear blind spots, silent failures on glass and soft/angled surfaces. | 🟠 High | **Settled in `hardware.md`:** v1 brings up one front HC-SR04 (milestone I); whether to add corner sensors is owned there, not an open roadmap question. Post-collision behavior settled in `architecture.md`. |
| 4 | **No encoders = open-loop control** | Without wheel encoders, distance and heading are guesses; the robot drifts and "back up 30cm" is dead reckoning. | 🟠 High | Accept open-loop for v1; lean on vision feedback in follow mode; encoders are a post-v1 upgrade. `hardware.md` |
| 5 | **Power / brownout** | Motor inrush and flyback drag the shared rail down and the Pi reboots mid-command — the #1 cause of mysterious resets. | 🟠 High | **Gate E (milestone F)** with an inline pass/fail and a built-in NO-GO recovery path; the **combined-load gate (K)** catches the under-load case (§2.3–§2.4). `hardware.md` |
| 6 | **Voice is the headline goal but the most-deferred, least-specified track** | The product's pitch is "talk to it," yet voice is Phase 3 and barely specced. Far-field STT on a cheap mic, over USB, near motor noise is genuinely hard. | 🟡 Medium | Keep it a separate track (**O**) after the text loop works; spec mic/STT/TTS only when reached. `software-spec.md`, `product-spec.md` |
| 7 | **Graceful degradation on WiFi loss** | The deliberative layer needs WiFi; if it drops mid-roam the robot must not go haywire. | 🟡 Medium | Reactive layer is network-independent by design, and the **WiFi-degradation test (milestone N)** proves safe-stop on link loss with observable asserts (§2.6). `architecture.md`, `software-spec.md` |

> **RISK —** Risks 1 and 2 are the ones that can invalidate the *design*, not just
> cost time — which is exactly why §1–§2 front-load runnable work against both before
> any code commits to them. Everything below #2 is a known engineering cost, not an
> unknown.

---

## 5. Consolidated open questions

Every still-pending decision in one place. Items the sibling docs have now settled are
converted to DECISIONs and cross-linked here so the backlog reflects reality.

> **DECISION —** **Final sensor count — settled in `hardware.md`.** v1 brings up one
> front HC-SR04 first (milestone I); whether to add corner sensors for the blind spots
> in risk #3 is owned by `hardware.md`, not tracked as an open roadmap question.
> → `hardware.md`, `architecture.md`

> **DECISION —** **Follow-mode tracker choice — decided by Gate H.** The §2.2 fps spike
> decides it: ≥ 3 Hz sustained → track-by-detection; ≤ 1–2 Hz → the blob/color tracker.
> Both branches are first-class deliverables (milestones L→M), so this is no longer open
> — only the measured number is pending. → `software-spec.md`

> **DECISION —** **Post-collision behavior — settled in the sibling docs.** After a
> collision-stop fires, the response (back off / rotate to clear / halt and escalate to
> the deliberative layer) is specified in `architecture.md` with the open-loop caveat of
> risk #4. Roadmap no longer tracks it as open. → `architecture.md`, `hardware.md`

> **DECISION —** **EXPLORE mode — settled in the sibling docs.** EXPLORE behavior is
> specified in `architecture.md` / `software-spec.md` and reflected in milestone M;
> roadmap defers to those rather than holding it open. → `architecture.md`, `software-spec.md`

> **OPEN —** **Model / provider choice.** Which vision-language model and provider for
> the per-step loop vs. the escalation model, and what's the tiering threshold? Cost,
> latency, and SDK all hang on this. → `software-spec.md`, `architecture.md`

> **OPEN —** **Voice timing and approach.** When does the voice track (**O**) actually
> start, and is far-field STT in scope for v1 or punted to the kid version?
> → `software-spec.md`, `product-spec.md`

> **OPEN —** **How much should v1 anticipate the kid version?** Where do we spend now
> to avoid a rewrite later (interfaces, modes) versus deliberately throwaway cardboard
> scaffolding? → `product-spec.md`

> **OPEN —** **Roaming power budget.** Which PD power bank, and does the ~1–1.5h
> runtime estimate survive motors + Pi + camera under real load? Gates **E** and **K**
> inform this, but the bank choice is still open. → `hardware.md`

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
