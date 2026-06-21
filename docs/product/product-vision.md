# Product Vision — yalp

> Why yalp exists, who it's for, and what is in and out of scope. The product-level north star; hardware, architecture, and code live in the sibling specs.

---

## 1. What yalp is

yalp is a from-scratch hobby robot — a Raspberry Pi 5 "brain" with a USB camera and two
differential-drive wheels — that you talk to in plain language. It interprets your intent,
moves and looks around under its own power, and tells you out loud what it sees.

The mental model that keeps scope honest:

> **THESIS —** yalp is *a clever talking pet that sees, chats, and moves toward things* —
> not a butler that fetches. Every scoping decision in this doc flows from that one image.
> If a request fits "a pet that notices and reacts," it's probably in scope. If it needs
> hands, a map, or stairs, it isn't.

This is a deliberate build-from-scratch project. There is no kit, no fixed dog/car/sphere
shape, and no closed list of keyword commands — full creative freedom over both the body and
the behavior is the point, not an accident of being cheap.

For the parts, wiring, and control loops behind this summary, see `hardware.md`,
`architecture.md`, and `software-spec.md`. For the concrete feature list and behaviors, see
`product-spec.md`. For sequencing and what ships when, see `roadmap.md`.

---

## 2. Vision & goals

The dream, stated plainly:

- **Talk to it naturally.** You say *"follow me,"* *"go check around the corner and tell me
  what's there,"* or *"what do you see?"* — and it understands and acts. Intent, not a fixed
  vocabulary of keyword commands.
- **It sees and reports back.** It looks at a scene and describes it to you in spoken language.
- **It moves through the world** under its own power and reacts to what's around it.
- **It's not locked into a kit.** Full freedom over form and behavior is a first-class goal.
  Building from scratch is the project, not a constraint to be apologized for.
- **v1 is a learning project.** Aesthetics, durability, and polish are explicitly *out of
  scope* for the first iteration. Get the core loop working; make it nice later.
- **Long-term it becomes a robot pet** — a delightful, kid-friendly companion for the
  builder's son (age 5–8). At that age he is the audience and co-pilot, not the operator.

> **DECISION —** The "dream" is voice-first — talking to a robot and having it talk back is
> the whole magic. But v1 is built **text-first**: the full see-think-move loop is proven over
> typed commands. The two halves of voice ship separately, though. Voice **output** (text-to-
> speech) is already here as an optional flag — `yalp see --speak` and `yalp agent --speak`
> read the robot's answers aloud via macOS `say` — so it can already talk back when asked.
> Voice **input** (speech-to-text) is the part still deliberately deferred to a later track,
> so the audio rabbit hole (mic quality, STT latency, wake-words) never blocks the core loop.
> See `roadmap.md` for the full DONE-vs-NEXT and `product-spec.md`.

> **THESIS —** The thing worth being proud of in v1 is the *interaction*, not the hardware:
> a robot you address in plain English that genuinely sees and moves. A cardboard chassis that
> nails that beats a beautiful chassis that only does keyword tricks.

---

## 3. Audience & the two horizons

yalp has two distinct users separated in time. Conflating them is the fastest way to over-build
v1, so they are scoped separately and explicitly.

| | **v1 — the learning build** | **Long-term — the robot pet** |
|---|---|---|
| **Primary user** | The builder (an adult, hands-on) | The son, age 5–8 |
| **Their role** | Operator *and* developer | Audience / co-pilot — never the mechanic |
| **What matters most** | The core loop works; learning happens | "It just works," safely, every time |
| **Looks** | Out of scope — cardboard is fine | In scope — cute and characterful |
| **Durability** | Out of scope — flaky is acceptable | In scope — survives a child's handling |
| **Safety** | Adult-supervised bench/floor use | First-class: no small parts, safe battery, no pinch points |
| **Reliability bar** | "Works when I babysit it" | "Works when a 6-year-old is alone with it" |
| **Status** | **This is what we're building now** | **Post-v1, a later sturdier iteration** |

> **DECISION —** v1 optimizes for the *builder's* learning, not the *son's* delight. Looks,
> durability, and child-safety are explicit non-goals for v1 (see §5) and become first-class
> only in the post-v1 pet. This is a scoping choice, not an oversight.

> **RISK —** The two horizons can quietly merge — "since I'm building it anyway, let me make
> it cute and kid-proof now." That doubles the v1 surface area and stalls the core loop. Resist.
> The kid version is a *second* build that reuses v1's brain, not a polished v1.

---

## 4. The guiding principle — what "do whatever I tell it" really means

The AI can *understand* almost any request. The robot can only *do* what its body and the code
allow. The honest envelope for a small wheeled bot sorts every request into three product-level
tiers — framed here by how the experience *feels*, not by how it's implemented (the fast-local /
slow-cloud split that delivers these tiers is `architecture.md`'s job).

| Tier | What it feels like | Example requests | v1 verdict |
|---|---|---|---|
| **In scope & satisfying** | Snappy, alive, reliable | "follow me," "come here," "back up," "spin," "stop," react to a face; *and* "what do you see?", "what color is this?", "read this sign," "is that a cat or a dog?", tell a story, play a game | **Ship it** |
| **In scope but clumsy** | Works, but halting — thinks for a few seconds per step | "go around the corner and tell me what's there," "explore this room and describe it" | **Ship it, good-enough** |
| **Out of scope for v1** | Needs sensors, hands, or terrain yalp doesn't have | "go to the kitchen" (named-place navigation), "pick up that toy / bring me the remote" (manipulation), stairs / rough terrain / outdoors far from WiFi | **Explicitly cut** |

### 4.1 In scope & satisfying

Two flavors, both of which feel good in v1:

- **Reactive movement** — following, coming, backing up, spinning, stopping, reacting to faces
  and gestures, not driving into walls. This feels *snappy and alive* because it doesn't wait on
  the cloud to think.
- **Talk & visual Q&A** — answering typed questions (spoken questions are the deferred
  speech-to-text track), describing a scene, reading a sign, identifying an object, telling
  stories, playing games. Answers can already be **spoken back aloud** via the optional
  `--speak` flag (text-to-speech is shipped). It's a camera and an LLM with a voice; this
  isn't time-critical, so a little latency doesn't hurt.

> **DECISION —** These two are the heart of v1. If yalp does *only* reactive movement plus talk
> and visual Q&A — and nothing in the lower tiers — v1 is already a success.

### 4.2 In scope but clumsy (good enough for v1)

"Go around the corner and report" / "explore this room and describe it." This genuinely works,
but it moves haltingly — a few seconds of "thinking" between each small step.

> **DECISION —** Explore-and-report ships in v1 in its *clumsy* form. Halting, bursty motion is
> an accepted v1 trade-off, not a bug to fix before launch. Making it *smooth* (continuous
> narration and decisions) is a named post-v1 stretch goal — see `roadmap.md`.

### 4.3 Out of scope for v1

> **DECISION —** Three capabilities are cut from v1 because cheap rolling hardware can't do them
> well, and faking them produces a frustrating toy:
> - **Reliable named-place navigation** ("go to the kitchen") — needs real mapping/SLAM and
>   better sensors than a single ultrasonic. Out.
> - **Manipulation / grasping** ("pick up that toy," "bring me the remote") — needs an arm and
>   gripper and far more control. Out.
> - **Stairs, rough terrain, and going outside far from WiFi** — wrong drivetrain, wrong
>   connectivity assumptions. Out.

> **RISK —** Because the AI *talks* as if it can do anything, users (especially a child later)
> will ask for out-of-scope things and believe yalp simply failed. The product must **decline
> gracefully and honestly** — "I can't pick things up, but I can go look" — rather than try and
> flail. Graceful refusal is a product requirement, not a nicety. (Behavior detailed in
> `product-spec.md`.)

---

## 5. Success criteria for v1

v1 is "done enough to be proud of" when, on the bench and the living-room floor, yalp can:

1. **See and tell.** Point it at a scene, ask "what do you see?", and get a correct, spoken-style
   description back. *(This is the magic moment — and it arrives early.)*
2. **Take plain-language commands.** Give it an intent in natural language and watch it pick the
   right high-level action — no fixed keyword list.
3. **Move and react safely.** Drive, turn, and follow a person, with a collision-stop reflex that
   never lets it drive into a wall — even mid-"thought."
4. **Explore and report.** Send it "around the corner" and have it come back with a description of
   what was there, clumsy-but-working.
5. **Decline gracefully.** When asked for something out of scope, say so plainly instead of
   flailing.

> **THESIS —** Hitting items 1–3 alone already justifies the build as a learning project. Items
> 4–5 are what make it feel like *yalp* and not a demo.

> **STATUS —** These criteria span two phases, and only the second is gated on parts arriving.
> The **LAPTOP phase is done**: the brain — `see`, `agent`, and `follow` (including an
> orientation-agnostic person detector) — is built and laptop-tested against a fake robot
> backend, and answers can already be spoken aloud via `--speak`. The **HARDWARE phase** —
> proving these same behaviors on the real chassis once the Raspberry Pi and motors arrive — is
> what remains. Read the success criteria above against that split: the *intelligence* is real
> today; the *embodiment* is the next milestone. See `roadmap.md` for the full DONE-vs-NEXT.

### 5.1 Explicit non-goals for v1

To protect the core loop, v1 deliberately does **not** aim for:

- **A nice-looking or durable body.** Cardboard, hot glue, and zip ties are fine.
- **Child-readiness.** No child-safety hardening, no small-parts/battery childproofing yet — v1 is
  adult-supervised. (These become first-class in the post-v1 pet — §3.)
- **Voice *input* in v1's critical path.** Text-first for commands; speech-to-text (the mic
  side) is a separate later track. Voice *output* (text-to-speech) already ships as an optional
  `--speak` flag, so this non-goal is about listening, not talking back.
- **Smooth, continuous motion.** Halting explore-and-report is accepted (§4.2).
- **Named-place navigation, manipulation, or off-road/outdoor operation.** Cut (§4.3).
- **On-device AI / zero cloud dependency.** v1 leans on cloud vision-language models; local
  inference is a post-v1 consideration. (Rationale in `architecture.md`.)
- **Long battery life or unattended operation.** ~1–1.5 hours per charge, supervised, is fine.

---

## 6. Open product questions

> **OPEN —** How much should the v1 learning build *anticipate* the eventual kid version? Picking
> reusable abstractions (the brain, the command vocabulary) is cheap foresight; building cute or
> child-proof hardware now is the over-build §3 warns against. Where exactly is the line? Leaning
> toward: design the *software/brain* to carry forward, treat the *body* as fully throwaway.

> **OPEN —** Voice is deferred but the *dream* is voice-first. Do we frame and message yalp as a
> voice product from day one (text is just scaffolding), or as a text product that later grows a
> voice? This shapes how the command surface and persona are designed even before audio exists.
> See `roadmap.md` and `product-spec.md`.

> **OPEN —** What is yalp's *personality*? "A clever talking pet" implies a character — name,
> voice, how it refers to itself, how it reacts. Deferred until the loop works, but the eventual
> kid version lives or dies on it, and early choices (how it phrases descriptions and refusals)
> quietly set the tone. Owned here once §5's core loop is proven.

> **OPEN —** What's the single demo that proves v1 to *the builder* — and later, the moment that
> makes *the son* light up? They may be different (a clean "explore-and-report" run vs. "it
> followed me and said hello"). Naming both sharpens what each horizon is really optimizing for.
