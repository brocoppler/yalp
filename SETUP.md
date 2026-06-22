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

### Optional: voice input

`yalp agent --speak` (text-to-speech output) works out of the box on macOS via
the built-in `say` command. Voice **input** (`yalp agent --listen` — speak a
command, have it transcribed locally) needs an optional extra:

```bash
pip install -e ".[voice]"
```

This adds `sounddevice` (microphone capture) and `faster-whisper` (local
speech-to-text). You also need system libraries:

- **macOS:** nothing extra — PortAudio ships with macOS.
- **Linux / Raspberry Pi:**
  ```bash
  sudo apt-get install libportaudio2    # PortAudio runtime for sounddevice
  sudo apt-get install espeak-ng        # for --speak TTS output
  ```
- **macOS (optional, parity with Pi TTS):** `brew install espeak-ng`

The base install and the test suite (`pytest`) do **not** require the `[voice]`
extra — voice is entirely opt-in, and the tests use a fake STT backend and
file-based audio sources so no microphone or model download is needed. See
[docs/technical/audio.md](docs/technical/audio.md) for the full voice reference
and the `YALP_VOICE_*` / `YALP_STT_*` env vars.

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

All three laptop commands are ready to use now. Here's a quick tour — the laptop
phase is complete end-to-end:

**`yalp see`** — ask Claude what the webcam sees (or ask any question about a
frame):
```bash
yalp see                              # "what do you see?"
yalp see "how many fingers am I holding up?"
yalp see --image tests/assets/sample.jpg "describe this image"
yalp see "what do you see?" --speak  # reads the answer aloud via macOS `say`
```

**`yalp agent`** — run the full deliberative loop against the simulated robot:
```bash
yalp agent "look around and tell me what you see"
yalp agent "follow me" --steps 3
yalp agent --command "follow me" --synthetic  # no webcam needed
yalp agent --listen                           # speak a command (needs [voice] extra)
yalp agent --listen --speak                   # speak a command, hear the reply
```

**`yalp follow`** — FOLLOW mode: track the nearest person and steer toward them:
```bash
yalp follow                           # face detector, your webcam
yalp follow --preview                 # same + OpenCV overlay window
yalp follow --detector person         # orientation-agnostic body detector
yalp follow --detector person --benchmark  # check fps vs Gate H threshold
yalp follow --synthetic --seconds 10  # no-camera demo, auto-stops after 10 s
```

> **First `--detector person` run:** the MobileNet-SSD model (~23 MB) is
> downloaded once and cached under `~/.cache/yalp/models`. Subsequent runs are
> instant. If you're offline, it will fail with instructions for dropping the
> file in by hand.

---

## Next target — Raspberry Pi 5 bring-up

At this point the **laptop "brain" is complete** — `yalp see`, `yalp agent`, and
`yalp follow` all work end-to-end against the simulated robot, including the full
**voice → follow → voice-stop loop** (243 tests pass). The next phase moves that
proven brain onto the Pi 5 hardware; it changes very little of the brain itself.
`RealReactiveBackend` (real GPIO motors + HC-SR04) is still a stub — implementing it
so the same brain drives real wheels is the goal.

**You can start now — no battery pack needed:**

1. **Flash the Pi.** Use Raspberry Pi Imager to write **Raspberry Pi OS Lite
   (64-bit)** to the microSD card, **headless** — enable SSH and Wi-Fi at flash
   time (no monitor or keyboard needed). You develop the brain on the laptop and
   talk to the Pi over SSH.
2. **Install the GPIO stack:** Python 3.11+ plus `gpiozero` / `lgpio`.
3. **GPIO first light (milestone G):** blink an LED / toggle one pin.
4. **HC-SR04 divider (milestone I):** build and *meter* the 1k/2k voltage divider to
   ~3.3 V before it touches a GPIO pin.
5. **Wire the drivetrain + sensor with power off (§5).** No soldering — the parts are
   pre-headered.

**Waits on the inbound 4×AA NiMH battery holder** (anything where motors spin): Gate E
power/brownout (F), "hello motors" (H), the collision-stop reflex (J), and the
detector-fps gates (K/L).

**Then swap the fake backend for the real one.** The deliberative brain you built on
the laptop stays the same; you replace the *fake* reactive backend with the real on-Pi
reactive layer (motors, ultrasonic, camera). The two layers talk over the same
localhost socket contract, so the brain doesn't know the difference.

The full bring-up order is a checklist of gates in
[docs/technical/roadmap.md](docs/technical/roadmap.md), with step-by-step bench
instructions in
[docs/technical/hardware-runbook.md](docs/technical/hardware-runbook.md). Follow that
ladder; each rung has a concrete "done" signal you can verify.
