# Audio — yalp's voice stack

> The consolidated technical reference for yalp's voice **output** (text-to-speech) and voice **input** (speech-to-text). Backends, the record→transcribe→agent pipeline, the dev/CI abstractions that make it run without a mic or a model download, and the full environment-variable surface.

---

## 1. Overview

yalp has two independent voice halves:

- **Voice OUTPUT (TTS)** — `--speak` reads a text reply aloud. Ships in software, needs no extra hardware on the laptop, and is a silent no-op when no TTS binary is present.
- **Voice INPUT (STT)** — `--listen` records a short clip from the microphone, transcribes it locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and feeds the transcript through the agent loop.

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

`yalp agent --listen` runs the push-to-talk pipeline:

1. **Record** — capture ~5 s of audio (default; configurable via `YALP_VOICE_RECORD_SECONDS`) from the configured source.
2. **Transcribe** — run the clip through faster-whisper locally (default model `tiny`; override with `YALP_STT_MODEL=base`). No audio leaves the machine.
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
| `YALP_STT_MODEL` | `tiny` | faster-whisper model size: `tiny` or `base` | `base` |

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

- Same faster-whisper backend as the laptop — **keep the model at `tiny`** on the 4 GB Pi 5 for memory headroom.
- `sounddevice` ships ARM64 wheels; install the `libportaudio2` system package so it has a PortAudio runtime.
- TTS on the Pi uses `espeak-ng` (`sudo apt-get install espeak-ng`); the same small USB speaker carries voice output once the body is built (see `hardware.md`).
