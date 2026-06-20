# Setup — laptop bring-up

This guide gets yalp running **on your laptop**, step by step. No robot hardware
is needed yet: the laptop's own webcam stands in for the robot's camera, and a
fake reactive backend stands in for the wheels. You don't need to be a
programmer — follow the steps in order and copy the commands exactly.

Everything here happens in a **terminal** (the Terminal app on macOS/Linux).

---

## 1. Install Python 3.11 or newer

Check what you have:

```bash
python3 --version
```

If it prints `Python 3.11.x` (or higher), you're set. Otherwise install a newer
Python:

- **macOS:** install from [python.org/downloads](https://www.python.org/downloads/),
  or with Homebrew: `brew install python@3.12`.
- **Linux (Debian/Ubuntu):** `sudo apt update && sudo apt install python3 python3-venv python3-pip`.

---

## 2. Get the code and open it in a terminal

Download or clone this repository, then `cd` into its folder:

```bash
cd path/to/yalp
```

You should see `README.md`, `pyproject.toml`, and a `src/` folder when you run `ls`.

---

## 3. Create and activate a virtual environment

A "venv" is an isolated sandbox for yalp's Python packages, so they don't clash
with anything else on your machine.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

After activating, your prompt shows `(.venv)`. Re-run the `source` line in any new
terminal window before working on yalp. To leave the venv later, run `deactivate`.

---

## 4. Install yalp

```bash
pip install -e ".[dev]"
```

This installs yalp plus its dependencies (the Anthropic SDK, OpenCV, Pillow,
NumPy) and the `pytest` test tool. The `-e` means "editable": code changes take
effect without reinstalling.

---

## 5. Get an Anthropic API key and add it

yalp's "thinking" runs on Anthropic's Claude models, which need an API key.

1. Sign in at [console.anthropic.com](https://console.anthropic.com/).
2. Create an API key (it looks like `sk-ant-...`).
3. Copy the example env file and paste your key into it:

   ```bash
   cp .env.example .env
   ```

   Open `.env` in any text editor and set:

   ```
   ANTHROPIC_API_KEY=sk-ant-your-key-here
   ```

`.env` is **git-ignored** — your key never gets committed or shared. Never paste
your key into source files or share it.

---

## 6. (Optional) Plug in the webcam

For laptop development your built-in webcam works out of the box. If you have the
**Logitech C270**, just plug it into a USB port — yalp can use it as the camera
source. With no camera at all, yalp falls back to generated test frames, so
nothing breaks.

---

## 7. Run the tests

```bash
pytest
```

A row of dots and `passed` at the bottom means everything is wired up correctly.

You can also check the command-line tool works:

```bash
yalp --help
```

> The interesting commands (`yalp see`, then `yalp agent`) arrive in the next
> wave. For now `--help` and the test suite confirm your setup is healthy.

---

## When the Raspberry Pi arrives

Everything above is laptop-only. The real robot body comes later and changes very
little of the brain:

1. **Flash the Pi.** Use Raspberry Pi Imager to write **Raspberry Pi OS Lite
   (64-bit)** to the microSD card, **headless** — enable SSH and Wi-Fi at flash
   time (no monitor or keyboard needed). You develop the brain on the laptop and
   talk to the Pi over SSH.
2. **Swap the fake backend for the real one.** The deliberative brain you built on
   the laptop stays the same; you replace the *fake* reactive backend with the
   real on-Pi reactive layer (motors, ultrasonic, camera). The two layers talk
   over the same localhost socket contract, so the brain doesn't know the
   difference.

The order in which to bring up the body — power, GPIO, motors, the ultrasonic
sensor, the safety reflex, then follow-mode — is laid out as a checklist of gates
in [docs/technical/roadmap.md](docs/technical/roadmap.md). Follow that ladder;
each rung has a concrete "done" signal you can verify.
