"""Make dcmonitor's two sounds as WAV files, with the standard library only.

    alarm.wav   two-tone siren (960 / 770 Hz, the European emergency
                vehicle tones), 4 cycles, about 2 s. The panel repeats it
                every 3 s until Silence.
    notice.wav  three rising beeps, twice, about 1.4 s. Played once per new
                notice.

The alarm is as loud as a WAV file can be: each tone is a sine driven into
soft clipping (tanh), close to a square wave, peaking at 98 % of full
scale. That carries about twice the energy of a plain sine at the same
peak (about +5 dB heard; 2 Oct 2026: "more louder") and its
harmonics cut through a noisy room. Louder than this needs the volume
turned up on the computer that plays it. Run from the project folder to
rebuild them:

    python sounds/make_sounds.py
"""

import math
import struct
import wave
from pathlib import Path

RATE = 44100
FOLDER = Path(__file__).resolve().parent


DRIVE = 3.0       # how hard the sine is pushed into clipping
# The notice is softer than the alarm (2 Oct 2026: the full-power
# notice made him jump): rounder tone, lower level, still louder than the
# first version.
NOTICE_LEVEL = 0.75
NOTICE_DRIVE = 1.5


def tone(frequency: float, seconds: float, level: float = 0.98, drive: float = DRIVE) -> list:
    """One loud, nearly square tone with 8 ms fades at both ends, so it
    does not click."""
    count = int(RATE * seconds)
    fade = int(RATE * 0.008)
    samples = []
    for n in range(count):
        t = n / RATE
        value = math.tanh(drive * math.sin(2 * math.pi * frequency * t)) / math.tanh(drive)
        envelope = min(1.0, n / fade, (count - n) / fade)
        samples.append(level * envelope * value)
    return samples


def silence(seconds: float) -> list:
    return [0.0] * int(RATE * seconds)


def write(name: str, samples: list) -> None:
    with wave.open(str(FOLDER / name), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767))
                                    for s in samples))


def main() -> None:
    siren = []
    for _cycle in range(4):
        siren += tone(960, 0.25) + tone(770, 0.25)
    write("alarm.wav", siren)

    chime = []
    for _round in range(2):
        for frequency in (880, 1175, 1568):
            chime += tone(frequency, 0.14, level=NOTICE_LEVEL, drive=NOTICE_DRIVE) + silence(0.06)
        chime += silence(0.12)
    write("notice.wav", chime)
    print("wrote", FOLDER / "alarm.wav", "and", FOLDER / "notice.wav")


if __name__ == "__main__":
    main()
