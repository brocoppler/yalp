# yalp

**yalp** is a from-scratch hobby robot: a Raspberry Pi 5 brain, a USB camera, and
differential-drive wheels, driven by a two-loop design. A fast on-Pi **reactive**
layer (motors, ultrasonic, ~10–30 Hz, owns the camera) keeps the robot safe and
responsive; a slow cloud **deliberative** layer (Claude VLM/LLM) handles intent
and vision Q&A. Development is **laptop-first** — the whole brain is built and run
on a laptop against a *fake* reactive backend (your laptop webcam stands in for
the robot's camera); only the real reactive layer needs the Pi.

## Repository layout

```
yalp/
├── README.md              # this file
├── SETUP.md               # step-by-step laptop bring-up for a non-coder owner
├── pyproject.toml         # packaging (src layout, console script `yalp`)
├── requirements.txt       # runtime deps (mirror of pyproject)
├── .env.example           # copy to .env; holds ANTHROPIC_API_KEY + model ids
├── src/yalp/
│   ├── config.py          # canonical constants (model tiers, thresholds, IPC)
│   ├── camera.py          # threaded latest-frame capture (webcam/image/synthetic)
│   ├── llm.py             # thin, mockable Anthropic wrapper
│   ├── cli.py             # `yalp` entrypoint + subcommand registry
│   ├── contract/          # loop-to-loop interface (Intent/RobotState) — Wave 2
│   ├── reactive/          # fast on-Pi loop + its fake — later wave
│   └── deliberative/      # perceive→think→act→report loop — later wave
├── scripts/               # dev/ops scripts
├── tests/                 # pytest suite + tests/assets/sample.jpg
└── docs/                  # the spec hub (open docs/index.html); source of truth
```

## Quickstart

```bash
# 1. Create and activate a virtual environment (Python 3.11+)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install yalp in editable mode (with dev extras for pytest)
pip install -e ".[dev]"

# 3. Add your Anthropic API key
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=...

# 4. Sanity-check the install
yalp --help        # shows all available commands
pytest             # 134 tests, all should pass
```

## Commands

All commands run **laptop-first** against the fake reactive backend (simulated
wheels); vision uses your real webcam, auto-falling back to a synthetic test
pattern when no camera is available.

| Command | What it does |
|---|---|
| `yalp --help` | Show the command set. |
| `yalp see [question...]` | Grab a camera still and ask Claude "what do you see?" (or any follow-up question). |
| `yalp agent [words... \| --command TEXT]` | Run the full deliberative loop (Claude → intents → fake reactive robot). |
| `yalp follow` | **FOLLOW mode**: detect/track the nearest person and steer the simulated wheels toward them. |

### `yalp see`

```
yalp see [question...]          # optional free-text question to ask about the frame
         [--image PATH]         # use a file instead of the webcam
         [--speak]              # read the answer aloud via macOS `say` (TTS)
```

### `yalp agent`

```
yalp agent [words...]           # natural-language command, e.g. yalp agent follow me
           [--command TEXT]     # explicit command string (alternative to positional words)
           [--steps N]          # max deliberate-loop iterations (default: 1)
           [--synthetic]        # use synthetic camera frames instead of webcam
           [--speak]            # read each response aloud via macOS `say` (TTS)
```

Natural-language commands like `yalp agent "follow me"` route through
`enter_follow_mode` into FOLLOW.

### `yalp follow`

```
yalp follow [--detector {face,hog,person,auto}]
            [--preview]         # OpenCV overlay window (headless-safe)
            [--benchmark]       # print fps baseline vs Gate H threshold
            [--seconds N]       # auto-stop after N seconds
            [--hz HZ]           # target tick rate (default: config.FOLLOW_HZ)
            [--synthetic]       # no-camera demo with generated frames
```

FOLLOW mode (software-spec.md §4): detect/track the nearest person on the webcam
and steer the simulated wheels toward them (turn to center, drive forward until
close; clean stop when lost/stale or too dark).

`--detector` picks the person detector:

- `face` (**default**, desk-only) — OpenCV's bundled Haar face cascade; reliable at
  desk range where the webcam frames only your head + shoulders.
- `hog` — OpenCV's built-in standing-body detector (no model download).
- `person` — **ORIENTATION-AGNOSTIC** cv2.dnn MobileNet-SSD body detector: tracks a
  person from **any angle** (front, **back**, side) at room range, so follow keeps
  working when you walk **away** with your back turned. This is the **robot's**
  default (face is desk-only) and the **Gate H** detector candidate. It uses
  OpenCV's built-in `cv2.dnn` (**no new pip dependency**) and downloads a small
  model file once on first run, cached under `~/.cache/yalp/models` (override with
  `YALP_MODEL_CACHE_DIR`); offline it fails with clear instructions for dropping the
  file in by hand. Try it: `yalp follow --detector person`, then stand back and turn
  around — it should still track.
- `auto` — prefers `person` at range, falls back to `face` for close-ups.

Benchmark the Gate H candidate with `yalp follow --benchmark --detector person`. The
laptop fps is a **ceiling**, not the gate verdict — Gate H is measured on the Pi
(under concurrent load) later.

### Voice output

All commands that produce a text response support `--speak`, which pipes the output
through macOS's built-in `say` command (TTS). Voice **input** (speech-to-text) is
not yet implemented.

## What's implemented / What's next

**Done (laptop phase — works today):**
- `yalp see` — VLM vision Q&A, optional image file input, voice output
- `yalp agent` — full deliberative loop against the fake reactive backend
- `yalp follow` — FOLLOW mode with `face`, `hog`, `person` (orientation-agnostic),
  and `auto` detectors; 134 tests pass
- Voice output (TTS) via `--speak`

**Next:**
- Voice input (STT) — not yet built
- Hardware path — real reactive layer (motors, ultrasonic, camera) runs on the Pi;
  see [docs/technical/roadmap.md](docs/technical/roadmap.md) for the gate ladder and
  [docs/technical/hardware-runbook.md](docs/technical/hardware-runbook.md) for
  bring-up steps

## Viewing the spec hub

The full design — product vision, architecture, software/hardware specs, and the
build roadmap — is a single self-contained page. Open it in a browser:

```
docs/index.html
```

After editing any markdown doc under `docs/`, rebuild the embedded page with
`python3 docs/build.py`.

## Learn more

- **Setup:** [SETUP.md](SETUP.md) — laptop bring-up, getting an API key, running tests.
- **Build order & gates:** [docs/technical/roadmap.md](docs/technical/roadmap.md).
- **The loop contract:** [docs/technical/software-spec.md](docs/technical/software-spec.md).
