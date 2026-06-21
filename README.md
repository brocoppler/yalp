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
yalp --help        # shows the (currently minimal) command set
pytest             # runs the smoke test
```

## Commands

Run `yalp --help` for the live list. Today:

| Command | What it does |
|---|---|
| `yalp see [question]` | Grab a webcam still (or `--image PATH`) and ask a Claude vision model "what do you see?". |
| `yalp agent "<command>"` | Run the deliberative agent loop against the fake reactive backend (real eyes, simulated wheels). It turns plain language into tool-call intents — including **`yalp agent "follow me"`**, which routes through `enter_follow_mode` into the FOLLOW behavior below. |
| `yalp follow` | Run **FOLLOW mode** (track-by-detection) against the real webcam: the simulated wheels steer toward a real person, printing a per-tick steering decision. `--seconds N` auto-stops; `--preview` shows a bbox/steering overlay when a display is available (headless-safe); `--synthetic` forces the test-pattern; `--benchmark` prints the laptop detector/tracker/FOLLOW-tick fps baseline vs the Gate H threshold. |

`yalp follow` realizes software-spec.md §4's track-by-detection thesis: OpenCV's
built-in HOG people detector (no model download) re-seeds a cheap box tracker, and
the FOLLOW loop turns toward the person (horizontal bbox error) and drives forward
until they're close enough (bbox size), degrading to a clean stop ("I lost you")
when the target is lost or the scene is too dark. The detector is pluggable — on
the Pi we'd swap in MobileNet-SSD / YOLO-nano behind the same interface; Gate H
decides.

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
