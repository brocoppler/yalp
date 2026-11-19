# Audio — yalp's voice stack

> The consolidated technical reference for yalp's voice **output** (text-to-speech) and voice **input** (speech-to-text). Backends, the record→transcribe→agent pipeline, the dev/CI abstractions that make it run without a mic or a model download, and the full environment-variable surface.

---

## 1. Overview

yalp has two independent voice halves:

- **Voice OUTPUT (TTS)** — `--speak` reads a text reply aloud. Ships in software, needs no extra hardware on the laptop, and is a silent no-op when no TTS binary is present.
- **Voice INPUT (STT)** — `--listen` records a short clip from the microphone, transcribes it locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and feeds the transcript through the agent loop. The **Logitech C270 webcam has a built-in mono mic** — no separate USB mic is needed on the Pi.

Both are opt-in flags on the CLI; neither is required for the base install or the test suite.

---

## 2. Voice OUTPUT (TTS)

`yalp agent --speak` and `yalp see --speak` speak each reply aloud after the turn completes.

### Backend selection

The TTS backend is chosen by platform:

| Platform | Backend | Install |
|---|---|---|
| macOS | built-in `say` | (none — ships with macOS) |
| Linux / Raspberry Pi | `espeak-ng` | `sudo apt-get install espeak-ng` |
| macOS (parity with Pi) | `espeak-ng` | `brew install espeak-ng` |

If **no** TTS binary is found on the host, `--speak` is a **silent no-op**: the command still prints its text reply and exits normally, it just doesn't speak. This keeps `--speak` safe to leave on in headless or CI environments.

---

## 3. Voice INPUT (STT)

### Continuous listening — `yalp listen`

`yalp listen` keeps the microphone open (`src/yalp/voice/listen.py`). It has two
modes, chosen by what the robot is doing:

| Robot is | What is listened for | How |
|---|---|---|
| Still, no command running | A sentence containing a wake word ("robot, turn left"). `YALP_WAKE_WORDS`, default `robot`; empty means everything heard is a command. | The stream is cut into utterances by loudness: speech starts when 3 of 5 blocks (0.1 s each) exceed 1.35× the background level, and ends after 0.6 s of quiet or at 12 s. Each utterance is transcribed. |
| Moving, or working on a command | Only "stop" / "halt". No wake word needed. | The newest 2 s of audio is transcribed every second and checked for a stop word. On a match the robot is sent to IDLE and the running command is cancelled. |

Plain motion commands — "turn left", "turn right 45 degrees", "turn around",
"go forward", "back up", "drive forward 1 meter", "follow me" — are recognised
locally (`parse_simple_command`) and sent straight to the wheels with no model
call, which removes 2–4 s of delay. Anything else goes to the agent. The reply
says how a move ended: `Stopped — you said "stop".`, `Finished — the move ended
by itself.`, or `Blocked by an obstacle — I stopped by myself.`

Replies print, and are also sent to Telegram when it is configured. Flags:
`--host` / `--port` (drive a real reactive server), `--file WAV` (replay a
recording, then exit), `--dry-run` (print what is heard; run nothing).

Measured on the robot (2026-11-18, C270, `tiny.en`, Pi at 83 °C):

- Speech from 1–2 m is only 1.5–3× the quiet-room level (block RMS about 0.010
  quiet, 0.015–0.030 speech). The detection ratio is tuned to that; the first words
  of a very quiet sentence can be missed.
- Stop over motor noise: one "stop" in the motor recording was caught, with no
  false stops in 26 s. Each 2 s window takes about 0.9 s to transcribe, so a stop
  takes roughly 1–3 s to act. The sonar SAFE_STOP is still the fast safety layer.

### Push-to-talk — `yalp agent --listen`

`yalp agent --listen` runs the push-to-talk pipeline:

1. **Record** — capture ~5 s of audio (default; configurable via `YALP_VOICE_RECORD_SECONDS`) from the configured source.
2. **Transcribe** — run the clip through faster-whisper locally (default model `tiny.en`; override with `YALP_STT_MODEL=base.en`). No audio leaves the machine.
3. **Agent** — feed the transcribed text into the normal deliberative loop.

Combine with `--speak` for a hands-free loop: `yalp agent --listen --speak`.

### Precedence

Positional words and `--command TEXT` **always take precedence** over `--listen`. If you pass command text *and* `--listen`, the text wins and the mic is not opened. `--listen` only supplies the command when no text is given.

---

## 4. The `YALP_VOICE_SOURCE` abstraction

`YALP_VOICE_SOURCE` selects where input audio comes from, so the STT pipeline can run with or without hardware:

| Value | Meaning | Typical use |
|---|---|---|
| `microphone` | Live capture via `sounddevice` (default) | Real laptop/Pi use |
| `synthetic` | Generated audio, no mic required | Unit tests, dev on machines with no mic |
| `file` | Replay a recorded WAV at `YALP_VOICE_AUDIO_FILE` | Deterministic CI, reproducing a clip |

This mirrors the existing **synthetic-camera** dev story: just as vision falls back to a generated test pattern when no webcam is present, voice input can be driven from a synthetic source or a recorded file so the pipeline is exercisable with no hardware.

---

## 5. The `YALP_STT_BACKEND` abstraction

`YALP_STT_BACKEND` selects the transcription engine:

| Value | Meaning | When to use |
|---|---|---|
| `faster-whisper` | Real local faster-whisper inference (default) | Real transcription on laptop/Pi |
| `fake` | Deterministic stub transcript, no model | CI and tests — **no model download needed** |

Use `fake` whenever you want to exercise the `--listen` code path without downloading or running a Whisper model (e.g. CI, or a quick smoke test).

---

## 6. Environment variables

All seven voice variables, with defaults and example values. They are documented in `.env.example`; copy it to `.env` to override.

| Variable | Default | Meaning | Example |
|---|---|---|---|
| `YALP_VOICE_SOURCE` | `microphone` | Audio source: `microphone`, `synthetic`, or `file` | `synthetic` |
| `YALP_VOICE_SAMPLE_RATE` | `16000` | PCM sample rate in Hz (16000 recommended for Whisper) | `16000` |
| `YALP_VOICE_CHANNELS` | `1` | Audio channels (1 = mono; Whisper expects mono) | `1` |
| `YALP_VOICE_RECORD_SECONDS` | `5` | Seconds of audio captured per utterance | `8` |
| `YALP_VOICE_AUDIO_FILE` | _(none)_ | WAV path used when `YALP_VOICE_SOURCE=file` | `tests/assets/sample.wav` |
| `YALP_STT_BACKEND` | `faster-whisper` | STT backend: `faster-whisper` or `fake` | `fake` |
| `YALP_STT_MODEL` | `tiny.en` | faster-whisper model size: `tiny.en` / `base.en` (English) or `tiny` / `base` (multilingual) | `base.en` |

---

## 7. Installation

Voice dependencies are an **optional extra**. The base install and the test suite do **not** require them.

```bash
pip install -e ".[voice]"
```

This adds `sounddevice` (microphone capture) and `faster-whisper` (local STT).

### System dependencies (Linux / Raspberry Pi)

```bash
sudo apt-get install libportaudio2   # PortAudio runtime for sounddevice
sudo apt-get install espeak-ng       # TTS backend for --speak
```

On macOS, `say` is built in for TTS; install `espeak-ng` via Homebrew only if you want parity with the Pi.

---

## 8. Dev / CI without hardware

Run the full voice path with no mic and no model download:

```bash
# Replay a recorded clip (deterministic):
YALP_VOICE_SOURCE=file YALP_VOICE_AUDIO_FILE=tests/assets/sample.wav yalp agent --listen

# Synthetic audio, no mic needed:
YALP_VOICE_SOURCE=synthetic yalp agent --listen

# Skip the Whisper model entirely (fake transcript):
YALP_STT_BACKEND=fake yalp agent --listen
```

---

## 9. Raspberry Pi 5 notes

- Same faster-whisper backend as the laptop — **keep the model at `tiny.en`** on the 4 GB Pi 5 for memory headroom.
- `sounddevice` ships ARM64 wheels; install the `libportaudio2` system package so it has a PortAudio runtime.
- TTS on the Pi uses `espeak-ng` (`sudo apt-get install espeak-ng`); a small USB speaker carries voice output once the body is built (see `hardware.md`).
- **The Logitech C270 webcam has a built-in mono microphone.** Voice INPUT (`--listen`) can be trialled on the Pi with zero new hardware — no separate USB mic required. Buy only the speaker first; add a dedicated mic later if the C270's audio quality proves insufficient.
