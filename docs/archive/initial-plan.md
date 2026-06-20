# AI Companion Robot — Project Spec & Goals

## One-line summary

A small, from-scratch robot with a camera and a "brain" that I can talk to in plain language — it interprets what I say, moves and looks around, and reports back what it sees. Long-term it becomes a fun toy for my son; **v1 is a personal build-and-learn project where looks and durability don't matter (cardboard is fine).**

---

## Vision & goals

- **Talk to it naturally.** I say something like *"follow me,"* *"go check around the corner and tell me what's there,"* or *"what do you see?"* and it understands and acts — not a fixed list of keyword commands.
- **It sees and reports.** It can look at a scene and describe it back to me out loud.
- **It moves through the world** under its own power and reacts to what's around it.
- **Not locked into a kit.** I want full freedom over the form and the behavior. Building from scratch on purpose — no pre-set dog/car shape, no closed repertoire.
- **v1 is a learning project.** Aesthetics, durability, and polish are explicitly *out of scope* for the first iteration. Get the core loop working first; make it nice later.
- **Eventual purpose:** a delightful, kid-friendly robot pet for my son (age 5–8). At that age he's the audience/co-pilot, not the operator — so "it just works reliably" matters more for *that* version than for v1.

---

## Guiding principle: what "do whatever I tell it" realistically means

The AI can *understand* almost any request. The robot can only *do* what its body and my code allow. So here's the realistic envelope for a small wheeled/rolling bot — and notice the buckets map cleanly onto the two-layer architecture below: the snappy, reliable stuff is fast *local* reflexes; the clumsy stuff is the slow *cloud* "thinking."

**In scope (reliable and satisfying)**
- *Reactive movement — runs locally on the fast layer, so it feels snappy and alive:* follow me, come here, back up, spin, dance, go forward/left/right, stop; react to faces and gestures; basic obstacle avoidance.
- *Talk and visual Q&A — runs through the cloud LLM, but it isn't time-critical so the latency doesn't hurt:* answer any spoken question, tell stories, play games (it's an LLM with a speaker); "what do you see?", "what color is this?", "read this sign," "is that a cat or a dog?"

**In scope but clumsy (good enough for v1) — the slow deliberative layer working step by step**
- "Go around the corner and report" / explore-and-describe a room — works via a vision-model-in-the-loop, but it moves haltingly (a few seconds of "thinking" per step).

**Out of scope for v1 (or for cheap rolling hardware in general)**
- Reliable navigation to *named* places ("go to the kitchen") — needs real mapping/SLAM + better sensors.
- Manipulation — "pick up that toy," "bring me the remote" — needs an arm/gripper and much more.
- Stairs, rough terrain, going outside far from WiFi.

**Mental model:** a clever talking pet that *sees, chats, and moves toward things* — not a butler that fetches.

---

## How it works (architecture)

**Core idea:** the robot runs on *two loops, not one.* A **fast reactive layer** lives entirely on the Pi and keeps the robot responsive and safe in real time. A **slow deliberative layer** calls a cloud vision-language model to understand what I want and decide high-level intent. The LLM sets the goal; the reactive layer executes it and stops the robot from hurting itself. This is the cheap, hobby version of how capable robots are actually built today — a slow model that reasons about the scene paired with a fast loop that drives the body.

Splitting it this way is the single most important design decision in v1. If everything routed through the cloud loop, the robot would be **blind and uncontrolled** during the 2–3 seconds it spends "thinking" each step — which is exactly how hobby bots drive into walls. Keeping reflexes local fixes that.

**Fast reactive layer** (on the Pi, ~10–30 Hz, no network):
- **Collision-stop** — overrides every other command. If the ultrasonic sees something too close, the wheels stop, full stop. A reflex, never gated behind the AI.
- **Person-tracking / centering** — when in "follow" mode, a lightweight on-device tracker keeps the target centered and drives toward it. No cloud in this path, so it actually keeps up with a moving person.
- **"Keep doing X until something changes"** — drive straight / turn until a condition flips.

**Slow deliberative layer** (cloud, seconds per step):
- Speech-to-text → LLM/VLM gets my words plus a still photo → picks a high-level intent and **sets a mode/goal for the reactive layer** → describes what it sees → text-to-speech.

**The "do anything" mechanism = AI agents / tool use.** Instead of hand-coding every command, I give the language model a *menu* of the robot's abilities (drive, turn, look, speak, enter-follow-mode, check-distance, describe-scene) plus my request, and the model chains the right actions itself. Crucially, the abilities it calls are mostly *modes for the reactive layer*, not direct motor twitches — the model says "follow that person," and the local tracker does the following. I build the toolbox; the AI conducts; the fast layer plays.

```
FAST REACTIVE LAYER — local, on the Pi, ~10–30 Hz, no cloud
   camera frames + ultrasonic  →  reflexes  →  motors
     • collision-stop  (overrides everything)
     • person-tracking / centering  (when in follow mode)
     • "keep doing X until something changes"

SLOW DELIBERATIVE LAYER — cloud, ~seconds per step
   voice  →  speech-to-text
          →  LLM/VLM  (my words + a camera photo)
          →  picks high-level intent, sets a mode/goal for the reactive layer
          →  describes what it sees  →  text-to-speech  →  spoken answer

   The LLM sets intent.  The reactive layer executes it and keeps the robot safe.
```

**Stills, not video (for v1).** The deliberative layer sends *still photos*, not a video stream — cheaper, simpler, and the model handles stills fine. The upgrade that makes this feel *smooth* instead of bursty is real-time streaming multimodal (continuous narration/decisions), but streaming a live feed from a Pi over home WiFi *while* driving motors is a heavier, pricier integration — so it's a stretch goal, not v1. (See caveats.)

**Tiered models.** Use a fast/cheap vision model for the per-step loop (centering, "is the path clear?", a rough "what's ahead?") and only escalate to a bigger model for genuinely hard requests ("describe this whole scene in detail," "read this sign"). No reason to pay flagship latency and cost on every frame.

---

## Prior art to steal from (don't reinvent the brain)

Two open-source robots already implement big chunks of what I'm building. I don't have to adopt their stacks, but reading them first will save real time — the conversation loop and the follow-me tracker are solved problems with published code.

- **LeKiwi** (Hugging Face's LeRobot) — a wheeled mobile base that installs and runs directly on a Raspberry Pi. It's the closest existing reference to my chassis-and-Pi setup; worth it for how they structure the Pi-side motor/control code.
- **Reachy Mini** (Hugging Face) — an open-source robot on a Pi with a camera, four mics, and a speaker, plus an SDK that integrates with LLMs. It ships a "Conversation App" for natural talking and a real-time tracker. It doesn't roam (head/body/antenna only), so it won't help with the explore-the-room half — but the talking-seeing-LLM half is basically my eventual kid version, already built and open.

**Plan:** spend a day reading both *before* writing my own conversation loop and tracker.

---

## Hardware spec

| Part | Pick | Approx. $ | Notes |
|---|---|---|---|
| **Brain** | Raspberry Pi 5 (4GB) | ~$110 | In v1 the Pi is mostly doing **I/O** — camera, motors, WiFi, audio — since the heavy AI runs in the cloud. So the Pi 5 isn't about raw compute here; it's about RAM headroom and **multiple real USB ports** (camera + mic + speaker without hub-juggling), which means far fewer gremlins on a first build. A Pi 4 (4GB) already on hand is fine. A Pi Zero 2 W (~$15) works but its single USB port and tight RAM trade money for debugging time — a false economy for build #1. **Skip the AI HAT / NPU** — running local models on it is a known hobbyist trap; CPU + cloud is the move for v1. **✓ Ordered** via the CanaKit Pi 5 4GB Starter Kit PRO (board + 27W PD wall supply + case/fan + 128GB OS card). *(The standalone board is ~$110 in the 2026 memory crunch vs $60 at launch.)* |
| microSD | 128GB card (came in the kit) | ✓ have | OS + code; pre-loaded with Raspberry Pi OS — I'll re-flash with the Lite image + WiFi/SSH for headless. |
| Pi power | 27W USB-C **PD** wall supply ✓ (in kit) + PD power bank (roaming — to buy) | ✓ + ~$30 | Wall PSU came with the kit (great for the bench). A PD power bank is still needed for roaming (Phase 2). Pi 5 wants a proper PD source, not any phone charger. |
| **Eyes** | Logitech C270 USB webcam | ~$22 ✓ | **Decided & ordered.** UVC plug-and-play; doubles as my laptop cam while building the brain. Pi Camera Module 3 stays a possible later swap (needs a Pi-5 camera cable) if I want autofocus. |
| **Motors** | 2× TT DC gear motors + wheels | ~$15 (4-pack) | Differential drive |
| Balance | 1–2× ball caster | ~$3 ea | Passive roller; holds up the chassis |
| Motor driver | DRV8833 *or* TB6612FNG | ~$5 | Cheaper, cooler, and more efficient than the classic L298N (which drops ~2V and runs hot, wasting battery). Use an L298N only if it's already in the parts bin. |
| Motor power | 4×AA holder *or* 18650 pack | ~$2–12 | **Separate** from Pi power; share a common ground. |
| **Obstacle sensor** | HC-SR04 ultrasonic | ~$3 | **Core, not optional.** This is the collision-stop reflex — in place from the moment the wheels first move. Cheapest insurance on the whole bot. |
| Wiring | Jumper wires + mini breadboard | ~$10 | |
| Chassis | Cardboard / hot glue / zip ties | — | v1 — whatever's on hand |
| *Deferred* | USB mic + small speaker | ~$15 | Voice in/out. Deliberately a **separate, later track** — get the whole loop working text-first. |

**Brain + camera are now ordered (~$200 — the CanaKit kit + C270).** Remaining to finish the robot: ~$135 (card reader + Phase 2 body + Phase 3 voice), for a total project of ~$335 — still low-hundreds. Full breakdown in the shopping list below.

> **Locomotion is the leading approach, not a commitment.** Two driven wheels + a ball caster (cheap, robust, textbook). *Not* attempting a balance-on-a-ball "ballbot" or BB-8 sphere yet — those are hard control problems saved for later.

---

## Shopping list — what's ordered and what's left

**Phase 1 (the brain) is ordered.** The all-in-one kit plus the camera together cover the entire brain — the Pi, its power supply, cooling, the OS card, and the eyes. Everything left to buy is the **body** (Phase 2) and **voice** (Phase 3), and those parts weren't hit by the price crisis, so the rest is small and predictable.

> **Heads-up on pricing (mid-2026):** a global DRAM/NAND shortage — driven by AI-datacenter demand — pushed Raspberry Pi and memory-card prices well above MSRP (the Pi 5 4GB is ~$110 now versus its $60 launch). That's already absorbed in the kit I bought. The body parts below (motors, driver, sensor, wiring, glue) are unaffected and stable.

### ✅ Already ordered (Phase 1 — the brain) — ~$200
| Item | What it covers | ~$ |
|---|---|---|
| **CanaKit Raspberry Pi 5 (4GB) Starter Kit PRO** | Pi 5 4GB board · UL-listed 27W USB-C PD **wall** supply · case with **active-cooling fan** · 128GB microSD **pre-loaded with Raspberry Pi OS** | ~180 |
| **Logitech C270 USB webcam** | the "eyes" — UVC plug-and-play; works on the Pi and doubles as my laptop cam while I build the brain | ~22 |

With just these, I can reach **build step 4 ("it sees and talks")** on the bench. Two things to know about the kit:
- Its PSU is a **wall** supply — perfect for development, but a roaming **power bank** is still on the buy list for when the robot moves.
- Its case fan handles cooling, so **no separate Active Cooler is needed**. The case is optional for a cardboard bot: I can leave the Pi in it (for the fan) and glue/zip-tie the whole cased unit to the chassis.

### 🛒 Still to buy

**First, to set up headless:**
| Item | Why | ~$ |
|---|---|---|
| USB microSD card reader — *skip if my laptop has an SD slot* | to re-flash the kit's card with WiFi + SSH preset (Raspberry Pi Imager) so it boots with no monitor | 8 |

> The card is pre-loaded, but for headless I'll re-flash it with my WiFi + SSH baked in. (Check the kit box — some CanaKit bundles already include a reader.)

**Phase 2 — the body (~$105):** adds movement, the safety reflex, and follow-mode.
| Item | Notes | ~$ |
|---|---|---|
| USB-C PD power bank (≥20W, ~10,000mAh) | roaming power — the kit's supply is wall-only | 30 |
| TT DC gear motors + wheels, 4-pack | need 2; spares are good | 15 |
| Motor driver: DRV8833 **or** TB6612FNG breakout | *get the pre-soldered-header version* — no soldering; cheaper and cooler than an L298N | 7 |
| Ball caster (passive roller) | holds up the chassis | 6 |
| HC-SR04 ultrasonic sensor | the collision-stop reflex — **core, not optional**; pins pre-attached | 8 |
| 4×AA holder + AA batteries | motor power, kept **separate** from the Pi (common ground); simple and safe — no lithium yet | 10 |
| Breadboard + jumper wire kit | get M-M, M-F, **and** F-F wires | 12 |
| Hot glue gun + zip ties + foam mounting tape | to build and mount the cardboard chassis | 19 |

> **Zero soldering required for v1** — every module above can be bought with pre-attached header pins or screw terminals. A soldering iron is a nice-to-have, not a must (~$20 if I'd rather solder).

### Phase 3 — voice (~$20)
| Item | Notes | ~$ |
|---|---|---|
| USB mini microphone + small USB speaker | voice in/out; bolt on once the text loop works. A ~$5 USB audio adapter gives cleaner sound and frees a USB port if onboard audio is weak. | 15–20 |

### Deliberately *not* buying
- **Monitor / keyboard / mouse** — headless; everything's over SSH from my laptop.
- **Separate Active Cooler / heatsink** — the kit's case fan already covers it.
- **AI HAT / NPU accelerator** — the heavy AI runs in the cloud for v1; local models on a Pi accelerator are a known hobbyist trap.
- **Depth camera / LiDAR** — that's the post-v1 navigation upgrade.

### Where the money's gone
- **Spent so far (the brain): ~$200** — CanaKit kit + C270 webcam.
- **Left to finish the whole robot: ~$135** — card reader + Phase 2 body + Phase 3 voice.
- **Total project: ~$335.** Still low-hundreds, not thousands.

---

## Software stack

- **OS:** Raspberry Pi OS Lite (headless — no monitor; develop over SSH/WiFi from my laptop).
- **Enable:** SSH, camera, I2C.
- **Reactive layer (local, on the Pi):** `gpiozero` for motors and the ultrasonic; `opencv` for the lightweight person/blob tracker that powers follow-mode and centering. This loop runs on-device with no network call.
- **Deliberative layer (cloud):** OpenAI / Anthropic SDK (or plain HTTP) to call a vision-language model. **Tiered** — a fast/cheap model for the per-step loop, a bigger one only for hard scene descriptions.
- **Camera:** `picamera2` (Pi cam) or standard capture (USB webcam).
- **Voice (separate, later track):** a speech-to-text + text-to-speech library or service, bolted on only *after* the text loop works end-to-end.
- **Develop the brain on my laptop, not the Pi.** Only motor-control and camera-capture genuinely need the Pi. The whole vision/agent loop gets built and iterated on my laptop (using the laptop webcam as a stand-in camera), where the edit-test cycle is instant — then married to the body at the end. Iterating that loop over SSH on a headless Pi is painfully slow, and there's no reason to debug Python *and* hardware *and* Linux all at once.

---

## Build path (milestones — smallest win first)

1. **Hello motors** — drive the wheels from Python (forward, turn, stop). *(From here on, develop the brain on the laptop — only motors + camera capture need the Pi.)*
2. **Safety reflex** — wire up the ultrasonic and build the collision-stop: a fast local loop that overrides any drive command when something's too close. This goes in *before* the robot ever drives on its own — it's the foundation of the reactive layer.
3. **Hello eyes** — capture a photo and save it.
4. **It sees and talks** — send the photo to a vision model ("what do you see?"), print the answer. ← *the magic moment, and it's only step 4.*
5. **It acts** — wire up the agent loop: command + action menu → the model picks high-level intent and sets a goal for the reactive layer. Now *"go forward and tell me what's there"* works, with the collision-stop running underneath the whole time.
6. **It follows / explores** — person-following as a **local tracker** (not a cloud round-trip, so it keeps up with a moving target), with obstacle-stop still underneath.
7. **(separate track) It listens and speaks** — add speech-to-text and text-to-speech once the full text-based loop works. Kept deliberately separate so the voice rabbit hole (mic quality, audio over USB, STT/TTS latency, wake-words) doesn't block the core magic.

Each step stands alone — I can stop and play at any point.

**Where I am now:** the brain (kit + webcam) is in hand but the body parts aren't yet, so my first sessions are the *vision path* — set up the Pi headless, then run steps **3–4** (capture a photo → "it sees and talks") on the bench. Steps **1–2** (motors, safety reflex) begin when the Phase 2 order arrives; meanwhile I can build the agent loop (step 5) on my laptop with the webcam standing in.

---

## Known constraints & honest caveats

- **Latency (and why the robot still feels alive):** each *deliberative* step is photo → API call → response → new goal, so high-level decisions arrive in halting bursts (a few seconds each). But the **reactive layer runs locally at full speed** — collision-stop, following, and centering don't wait on the cloud — so the robot stays responsive and safe *between* those bursts instead of freezing. The bursts are the thinking; the body keeps moving.
- **Smooth mode (the streaming upgrade):** real-time streaming multimodal (continuous narration/decisions instead of photo-by-photo) is what would make the deliberative layer feel smooth. Over home WiFi while driving motors it's a heavier, pricier integration — parked as a stretch goal, not v1.
- **Cost:** cloud API calls are pennies-to-dollars for hobby use, and tiering models keeps it cheap. A local model avoids fees but needs beefier hardware and is less capable.
- **Connectivity:** the deliberative layer needs WiFi wherever it roams (fine indoors). The reactive layer keeps working even if the network hiccups — the robot just stops taking new high-level commands, it doesn't go haywire.
- **Battery:** expect ~1–1.5 hours of runtime per charge.
- **Reliability:** this is a flaky hobby build, not a polished product — totally fine for v1, but it's why the *kid* version is a later, sturdier iteration.
- **Learning curve:** ~4–8 hours for a basic from-scratch build, plus debugging (loose jumpers, wrong pins, Linux/Python gremlins) — which is where most of the learning happens. Developing the brain on the laptop cuts a good chunk of this.

---

## Future / stretch (post-v1)

- Kid-friendly enclosure (cute, durable, safe — small parts & lithium battery handled responsibly).
- **Smooth mode:** real-time streaming multimodal so the deliberative layer narrates and decides continuously instead of in bursts.
- Better navigation: depth camera or LiDAR + SLAM for reliable "go to X."
- A stronger on-device tracker for smoother person-following and room-mapping.
- Onboard AI (e.g. a Jetson Orin Nano) to cut latency and the cloud dependency.
- The dream: a robot that actually balances on a ball.

---

## Decisions — now settled

- **Brain** — Raspberry Pi 5 4GB, via the CanaKit Starter Kit PRO. **Ordered.**
- **Camera** — Logitech C270 USB webcam. **Ordered.** (Pi Camera Module 3 stays a possible later swap — it'd need a Pi-5 camera cable — if I want autofocus.)
- **Locomotion** — differential drive: two TT gear motors + a ball caster. Approach locked; parts are in the Phase 2 order, not yet bought.
- **Voice** — a separate, later track; text-first.
