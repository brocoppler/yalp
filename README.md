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

> **Coming next:** `yalp see` (capture a still → Claude "what do you see?") lands
> in the next wave, followed by `yalp agent` (the full deliberative loop driving
> the fake reactive backend). They are not implemented yet — `yalp --help` lists
> only what exists today.

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
