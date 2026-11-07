"""Postroom's sounds. The new-mail chime is generated here, so it is Postroom's own: three
soft bell notes rising (E, G, C). Played with PipeWire, PulseAudio or ALSA, whichever is
available, without blocking the app.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from .config import cache_dir

RATE = 44100
VERSION = 1  # bump to regenerate the file after changing the sound
PLAYERS = (["pw-play"], ["paplay"], ["aplay", "-q"])


def _bell(freq: float, length: float, volume: float) -> list[float]:
    """A bell-like note: a sine with an inharmonic partial, struck and decaying."""
    n = int(RATE * length)
    out = []
    for i in range(n):
        t = i / RATE
        attack = min(1.0, t / 0.004)
        env = attack * math.exp(-t * 6.5)
        s = (math.sin(2 * math.pi * freq * t)
             + 0.35 * math.sin(2 * math.pi * freq * 2.76 * t) * math.exp(-t * 9)
             + 0.15 * math.sin(2 * math.pi * freq * 5.4 * t) * math.exp(-t * 14))
        out.append(volume * env * s / 1.5)
    return out


def new_mail_samples() -> list[float]:
    notes = [(1318.5, 0.0), (1568.0, 0.11), (2093.0, 0.22)]  # E6, G6, C7
    total = int(RATE * 0.9)
    buf = [0.0] * total
    for freq, start in notes:
        off = int(RATE * start)
        for i, v in enumerate(_bell(freq, 0.65, 0.32)):
            if off + i < total:
                buf[off + i] += v
    fade = int(RATE * 0.05)
    for i in range(fade):  # no click at the end
        buf[total - fade + i] *= 1 - i / fade
    return buf


def write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767))
                               for s in samples))
    tmp.replace(path)


def new_mail_path() -> Path:
    path = cache_dir() / f"new-mail-v{VERSION}.wav"
    if not path.exists():
        write_wav(path, new_mail_samples())
    return path


def player() -> list[str] | None:
    return next((p for p in PLAYERS if shutil.which(p[0])), None)


def play_new_mail() -> bool:
    """Start the chime and return at once. False if there is no way to play it."""
    cmd = player()
    if cmd is None:
        return False
    try:
        subprocess.Popen(cmd + [str(new_mail_path())], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except OSError:
        return False
