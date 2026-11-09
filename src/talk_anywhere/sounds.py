"""Short original tones. Playback uses the current speakers and does not change them."""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

from talk_anywhere.paths import ROOT

RATE = 44100

# Each note is frequency, seconds, gain. Frequency 0 is silence.
PAIRS: dict[str, dict] = {
    "rise": {
        "title": "Rise and fall",
        "start": [(659.25, 0.08, 0.22), (0, 0.035, 0), (880.0, 0.16, 0.24)],
        "stop": [(880.0, 0.07, 0.2), (0, 0.03, 0), (493.88, 0.18, 0.22)],
    },
    "bell": {
        "title": "Bright bell",
        "start": [(988.0, 0.22, 0.16), (1568.0, 0.16, 0.08)],
        "stop": [(494.0, 0.26, 0.16), (740.0, 0.2, 0.07)],
    },
    "soft": {
        "title": "Soft pop",
        "start": [(1568.0, 0.07, 0.2)],
        "stop": [(311.13, 0.12, 0.22)],
    },
    "knock": {
        "title": "Wood knock",
        "start": [(1800.0, 0.03, 0.2), (0, 0.05, 0), (1400.0, 0.04, 0.18)],
        "stop": [(146.83, 0.14, 0.28)],
    },
}

DEFAULT_PAIR = "rise"


def sound_dir() -> Path:
    path = ROOT / "assets" / "sounds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def sound_file(pair: str, role: str) -> Path:
    key = pair if pair in PAIRS else DEFAULT_PAIR
    return sound_dir() / f"{key}-{role}.wav"


def _render(notes: list[tuple[float, float, float]], mix: bool) -> list[float]:
    if mix:
        parts = [_tone(freq, seconds, gain) for freq, seconds, gain in notes if freq]
        length = max(len(part) for part in parts)
        mixed = [0.0] * length
        for part in parts:
            for index, sample in enumerate(part):
                mixed[index] += sample
        peak = max(1e-6, max(abs(sample) for sample in mixed))
        return [sample / peak * 0.35 for sample in mixed]
    samples: list[float] = []
    for freq, seconds, gain in notes:
        if freq <= 0:
            samples.extend([0.0] * int(seconds * RATE))
        else:
            samples.extend(_tone(freq, seconds, gain))
    return samples


def _tone(freq: float, seconds: float, gain: float) -> list[float]:
    count = max(1, int(seconds * RATE))
    samples = []
    for index in range(count):
        time = index / RATE
        attack = min(1.0, index / (0.006 * RATE))
        decay = math.exp(-4.2 * time / max(seconds, 0.04))
        samples.append(gain * attack * decay * math.sin(2 * math.pi * freq * time))
    return samples


def _write_wav(path: Path, samples: list[float]) -> None:
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        frames = b"".join(
            struct.pack("<h", max(-32767, min(32767, int(sample * 32767))))
            for sample in samples
        )
        handle.writeframes(frames)


def write_sounds() -> None:
    folder = sound_dir()
    for key, spec in PAIRS.items():
        for role in ("start", "stop"):
            path = folder / f"{key}-{role}.wav"
            mix = key == "bell"
            _write_wav(path, _render(spec[role], mix=mix))


def ensure_sounds() -> None:
    missing = any(not sound_file(key, role).is_file() for key in PAIRS for role in ("start", "stop"))
    if missing:
        write_sounds()


def play(pair: str, role: str) -> None:
    ensure_sounds()
    path = sound_file(pair, role)
    try:
        subprocess.Popen(
            ["paplay", "--client-name=TalkAnywhere", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return
