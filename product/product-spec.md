# yalp — Product Spec

> What yalp does from the user's seat: the commands it understands, how it behaves, how fast it feels, and where it honestly falls short in v1. Behavior, not implementation.

---

## 1. Interaction Model

yalp is a talking, seeing, rolling robot you drive with plain language. The loop is always the same three beats:

1. **You say something** — in v1 you *type* it from a laptop over the network.
2. **It works out what you mean** — not a fixed keyword list; it interprets intent ("follow me," "what's around the corner?," "read this sign").
3. **It acts and/or reports back** — it moves, looks, answers, or some combination, then waits for the next thing.

There is no app to learn and no command syntax to memorize. The mental model for the user is a **clever talking pet that sees, chats, and moves toward things** — not a butler that fetches named objects from named rooms. See `product-vision.md` for why that framing is the whole point.

### A turn, end to end

Interaction is turn-based, like a chat: you send one thing, yalp does it and replies, then waits. A turn can be pure talk (*"tell me a joke"*), pure motion (*"spin"*), or **both at once** — *"go forward and tell me what you see"* mixes a Tier-1 move with a Tier-2 report in a single command, and yalp handles the blend without you having to split it up. There is no always-listening mode in v1; nothing happens until you send a command, and `stop` ends whatever is in flight.

> **DECISION —** v1 is **text-first**. You type commands from your laptop and read its replies as text. Voice — speech-to-text in, text-to-speech out — is a **deferred, separate track**, bolted on only after the full typed loop works end to end. Treating voice as its own project (mic quality, audio over USB, STT/TTS latency, wake-words) keeps the voice rabbit hole from blocking the core magic. See `roadmap.md`.

> **THESIS —** yalp has **two speeds**, and the user feels both. Reflexes (stop, steer, come-here, don't-hit-things) are **instant** because they run locally; thinking (understanding a request, describing a scene) takes **a beat** because it goes to the cloud. Every expectation in this doc flows from that split. One caveat the reflex framing hides: vision-driven moves can only react as often as the camera spots a person, and on v1 hardware that cadence is slow — which is exactly why continuous follow is a stretch and come-here is the safe bet. The mechanism lives in `architecture.md`; here we only describe how it *feels*.

---

## 2. Capability Catalog

Capabilities come in three tiers, ordered by how snappy and reliable they feel. Tier 1 is the fast local layer; Tier 2 is cloud conversation; Tier 3 is cloud deliberation driving the body, which is real but clumsy in v1.

For every capability: the **user-facing behavior**, the **latency feel**, and the **v1 quality bar** (what "good enough" means for the first build).

### Tier 1 — Reactive movement (fast, local, feels alive)

These respond immediately and keep responding without waiting on the network.

| Capability | User-facing behavior | Latency feel | v1 quality bar |
|---|---|---|---|
| **Stop** | Halts the wheels at once, mid-command if needed. | Instant — top priority. | Always works, every time, even while doing something else. |
| **Forward / left / right** | Drives or steers the named way until told to stop or something blocks it. | Instant. | Direct and responsive; collision-stop always underneath. |
| **Back up** | Reverses a short, timed distance — best-effort, not a measured one. | Instant. | Short and controlled; open-loop and un-odometered, so distance is approximate; not guarded by a rear sensor (v1 senses forward only). |
| **Spin / turn in place** | Rotates left or right on the spot. | Instant. | Turns reliably; angle is approximate and open-loop, not degree-accurate. |
| **Dance** | Runs a short, canned playful motion. | Instant. | A fixed fun routine — purely for delight. |
| **Come here** | Drives toward the person it sees until close, then stops — a one-shot approach, not a continuous lock. | Instant start; runs to completion, then stops. | Reaches you across a clear room in good light; needs line of sight, not a homing beacon. The easiest, most repeatable people-seeking behavior in v1 — the demo centerpiece. |
| **Follow me** | Locks onto the nearest person and drives to keep them centered and at distance. | Instant to react, but the person-detector runs at a low frame rate, so fast motion outpaces it. | Tracks a person walking at a slow, steady pace in good light; stops and reports ("I lost you") if the person moves fast, turns away, or the scene is cluttered. A v1 stretch — see §3 and the come-here decision below. |
| **React to faces / gestures** | Notices a face or simple gesture and responds (orients, acknowledges). | Near-instant. | Detects faces in good light; gesture vocabulary is small and best-effort. |
| **Basic obstacle avoidance** | Won't drive into what's directly ahead; stops or refuses. | Instant reflex. | Reliably stops for obstacles in the forward cone; **not** 360° aware. |

> **DECISION —** **"Come here" is the v1 demo centerpiece, not "follow me."** A one-shot approach-then-stop only has to find a person once and drive until close — far easier and far more repeatable than continuous tracking, which depends on a low-frame-rate person detector keeping a moving lock. Continuous **"follow me" is a stretch goal within v1**: nice if it lands, never the thing the demo rests on. Build, demo, and judge v1 on come-here; treat follow as a bonus. The engineering reasons live in `architecture.md` and `software-spec.md`.

> **DECISION —** **"Good light" means roughly 200–300 lux** — an ordinary, well-lit indoor room. This is the floor for any vision-driven motion (come-here, follow, face/gesture reactions). **Below that floor, come-here and follow degrade gracefully to a hard STOP plus an "I lost you" report** — a spec'd behavior, not an anecdotal caveat. yalp never gropes around in the dark; if it can't see well enough to be sure, it stops and says so. The exposure/detection mechanics are in `software-spec.md` and `hardware.md`.

> **RISK —** **Motion is approximate and open-loop.** With no wheel encoders, "back up," "spin," and "turn" run on timed power, not measured distance or angle — so the same command lands a little differently each time, and exact distances and angles are best-effort, not guaranteed. Set this expectation with the user up front so it reads as honest, not broken. The safety upside: after a near-collision the robot **halts and reports** rather than blindly reversing into something it can't see (v1 senses forward only — see §5). Details in `software-spec.md` and `hardware.md`.

### Tier 2 — Talk & visual Q&A (cloud, latency is fine)

These take a beat but the wait doesn't hurt — you're asking, not steering.

| Capability | User-facing behavior | Latency feel | v1 quality bar |
|---|---|---|---|
| **Answer questions** | You ask anything; it answers conversationally. | A beat (one cloud round-trip). | It's a language model with a speaker — answers are as good as the model. |
| **Tell stories / play games** | Open-ended chat, storytelling, simple word games. | A beat per turn. | Engaging back-and-forth; no guarantee of memory across separate sessions in v1. |
| **"What do you see?"** | Snaps a photo and describes the scene back. | A beat. | A solid one-to-two-sentence description of what's actually in frame. |
| **"What color is this?" / "is that a cat or a dog?"** | Answers a specific visual question about what's in front of it. | A beat. | Reliable on clear, well-lit subjects close to the camera. |
| **"Read this sign"** | Reads legible text it can see. | A beat (may escalate to a stronger model). | Reads clear, printed text held steady in frame; struggles with small, skewed, or faint text. |

### Tier 3 — Explore-and-report (cloud-in-the-loop, works but halting)

| Capability | User-facing behavior | Latency feel | v1 quality bar |
|---|---|---|---|
| **"Go around the corner and tell me what's there"** | Drives a short exploration, looks as it goes, then reports what it found. | **Stop-go** — moves a step, thinks a few seconds, moves again. | It works and returns a real description. It is *halting, not smooth*, and it does **not** promise to reach a named place. |

> **THESIS —** Tier 3 is the headline demo *and* the honest weak spot. It proves "go look and tell me" is possible on cheap hardware — and it visibly stutters while doing it. Sell it as "watch it think," not "watch it glide."

---

## 3. Acceptance Scenarios

Concrete "when I do X, it does Y / says Z" cases that define success. Each one pins down a capability.

- **Stop wins.** When I type `stop` while it's mid-drive, the wheels halt within a fraction of a second. *(Tier 1: Stop.)*
- **It comes to me — the centerpiece.** When I type `come here` from across a clear room in good light (~200–300 lux), it turns toward me and drives over until it's close, then stops on its own. This is the one-shot approach the v1 demo rests on. *(Tier 1: Come here.)*
- **It follows me at a walk.** When I type `follow me` and walk at a slow, steady pace across a well-lit room (~200–300 lux), it pivots and drives to stay pointed at me at a roughly constant distance; when I stand still, it stops. If I move fast, turn away, or the scene is cluttered, it stops and reports *"I lost you"* rather than chasing the wrong thing. *(Tier 1: Follow me — a v1 stretch.)*
- **It stops in the dark.** When I run `come here` or `follow me` in a dim room (below ~200 lux), it does not grope toward me — it stops and reports *"I lost you."* Failing to see is a clean STOP, not a guess. *(Tier 1: low-light degradation.)*
- **It refuses to crash.** When I type `go forward` while it faces a wall, it drives, then stops on its own before contact and tells me it's blocked — it never pushes into the wall. After such a halt it reports rather than blindly reversing. *(Tier 1: Obstacle avoidance / safety.)*
- **It describes the room.** When I type `what do you see?`, within a few seconds it replies with a sentence or two naming the main things in front of it. *(Tier 2: VQA — the magic moment.)*
- **It reads a sign.** When I hold a hand-printed sign reading `HELLO` in front of it and type `read this sign`, it replies `HELLO`. *(Tier 2: Read text.)*
- **It tells a story.** When I type `tell me a story about a robot`, it tells one. *(Tier 2: Talk.)*
- **It handles a combined command.** When I type `go forward and tell me what you see`, it drives forward and, a beat later, reports what's now in front of it — one command, both a move and a description. *(Tier 1 + Tier 2.)*
- **It explores, haltingly.** When I type `go around the corner and tell me what's there`, it drives in halting steps, pauses to think, and finally reports something like *"I see a couch and a doorway"* — taking maybe a minute, not a smooth glide. *(Tier 3: Explore-and-report.)*

If all of these pass, v1 has hit its product bar. See `roadmap.md` for the build order that gets here.

---

## 4. UX & Latency Expectations

yalp deliberately feels like two different things at once, and that's a feature, not a bug.

| Tier | What you're doing | How it should feel | If it feels wrong |
|---|---|---|---|
| **1 — Reactive** | Steering, stopping, come-here | Immediate, like a toy | Any lag here is a bug — Tier 1 must never wait on the cloud. Continuous follow is the exception: its *control* is instant, but it can only re-aim as often as it spots you, so it tracks a slow walk and stops on fast motion (§2). |
| **2 — Talk & VQA** | Asking, chatting, "what's this?" | A short, unremarkable pause | A multi-second wait is expected and fine — you're conversing, not driving. |
| **3 — Explore** | "Go look and report" | Visibly stop-go | Smooth motion would be *nice* but isn't promised in v1 (see below). |

### Why some things feel instant

Reflexes — stop, follow, steer, don't-hit-things — run **on the robot itself**, with no network in the path. They respond as fast as you'd expect a toy to. This is what keeps yalp feeling *alive* rather than laggy. The "how" is in `architecture.md`; from the user's side, just expect Tier 1 to be immediate.

### Why some things take a beat

Anything that requires *understanding* — a question, a scene description, a high-level command — makes a round trip to a cloud model. That's a few seconds. For asking and answering, the wait is unremarkable. The trade is intentional: it buys real intelligence on cheap hardware.

### Why explore-and-report stutters

Tier 3 chains many "think" steps together while the robot moves, so the delays stack up into visible stop-go motion.

> **RISK —** Explore-and-report is **stop-go by nature** in v1: drive a little, freeze a few seconds to think, drive a little more. A user expecting a smooth roving robot will be underwhelmed. Set expectations up front — *"it's thinking out loud with its feet"* — or the feature reads as broken. The smooth, continuously-narrating version is a deferred upgrade; see `roadmap.md`.

> **RISK —** Between thinking bursts, the robot is steering on local reflexes alone, not fresh cloud judgment. That's safe (the collision-stop reflex never sleeps — see §5), but it means yalp can pause facing the "wrong" way or describe a scene a beat after it's changed. Acceptable for v1; worth saying out loud so it doesn't feel like a bug.

---

## 5. Safety — from the user's point of view

The one promise yalp makes about its body: **it should never drive into things.**

- **Collision-stop overrides commands.** If something is too close ahead, the wheels stop — even if you just told it to go forward. A movement command is a *request*; the don't-crash reflex is *not* negotiable and is never gated behind the thinking layer.
- **It fails safe, not wild.** If the network drops or the cloud is slow, yalp simply stops taking new high-level commands. It does not lurch, spin, or keep driving blind — the reflex layer just holds it still.
- **You can always stop it.** `stop` is the highest-priority command and works at any time.

The user only needs to trust those three things. *How* they're guaranteed — the always-on local reflex loop and the forward distance sensor — lives in `architecture.md` and `hardware.md`.

> **RISK —** v1 senses obstacles in a **forward cone only**. It can still back into or clip something to its side or rear, because there's no sensor there yet. Don't oversell "it won't hit anything" — the honest claim is "it won't drive *forward* into things." Wider awareness is a post-v1 sensor upgrade in `roadmap.md`.

---

## 6. Out of Scope for v1 — and Open Questions

The capability tiers cut both ways: the same line that says what's *in* says what's *out*. v1 deliberately does **not** promise:

| Not in v1 | Why | Where it lives |
|---|---|---|
| Navigation to *named* places — "go to the kitchen" | Needs real mapping/SLAM and better sensors than cheap rolling hardware has. | `roadmap.md` |
| Manipulation — "pick up that toy," "bring me the remote" | Needs an arm and gripper; yalp has wheels and a camera, nothing to grasp with. | out of project scope for v1 |
| Stairs, rough terrain, roaming far from WiFi | Differential-drive-on-a-flat-floor robot; the thinking layer needs the network. | `roadmap.md` |
| Smooth, continuously-narrating exploration | Real-time streaming multimodal is a heavier, pricier integration. | `roadmap.md` |
| Degree-accurate or distance-accurate motion | v1 motion is approximate and un-odometered by design. | `architecture.md` |
| 360° / rear / side obstacle awareness | One forward sensor in v1 (see §5). | `hardware.md` |
| Voice in and out | Deferred separate track; v1 is text-first (§1). | `roadmap.md` |

Promising any of these would set the user up to be disappointed. The honest pitch is the Tier 1–3 catalog above and nothing beyond it.

### Open questions

> **OPEN —** How kid-friendly should the v1 *command set* be? v1's operator is the builder (me), so terse, literal commands are fine — but the long-term audience is a 5–8-year-old who's a co-pilot, not an operator. Do we shape the v1 vocabulary toward that now, or keep v1 developer-facing and design the kid command set fresh later? Leaning toward the latter; revisit in `product-vision.md`.

> **OPEN —** Far-field voice on a noisy floor is genuinely hard — a kid talking across a room with a TV on is a tough audio problem, not a quick add-on. v1 sidesteps it by being text-first, but the kid version can't. Defer the detail (mic array vs. single mic, wake-word, noise handling) to `roadmap.md` and `software-spec.md` rather than pretending it's solved.

> **OPEN —** Should yalp remember anything between sessions — names, preferences, past games — or start fresh each time? v1 assumes stateless. Persistence is a personality question worth settling before the kid version; see `product-vision.md`.
