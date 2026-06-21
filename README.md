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

All commands run **laptop-first** against the fake reactive backend (simulated
wheels); vision uses your real webcam, auto-falling back to a synthetic test
pattern when no camera is available.

| Command | What it does |
|---|---|
| `yalp --help` | Show the command set. |
| `yalp see [question]` | Grab a camera still (or `--image PATH`) and ask Claude "what do you see?". |
| `yalp agent [command]` | Run the full deliberative loop (Claude → intents → fake reactive robot). Natural-language commands like `yalp agent "follow me"` route through `enter_follow_mode` into FOLLOW. |
| `yalp follow` | **FOLLOW mode** (track-by-detection, software-spec.md §4): detect/track the nearest person on the real webcam and steer the simulated wheels toward them (turn to center, drive forward until close; clean stop when lost/stale or too dark). |

`yalp follow` flags: `--seconds N` (auto-stop), `--preview` (OpenCV overlay window
if a display is available; headless-safe), `--synthetic` (no-camera demo), and
`--benchmark` — print the laptop detector / tracker / FOLLOW-tick fps baseline and
compare it to the **Gate H** GO threshold (`config.GATE_H_GO_HZ`). The laptop uses
OpenCV's built-in HOG people detector (no model download); on the Pi we'd swap in a
faster detector behind the same interface.

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
