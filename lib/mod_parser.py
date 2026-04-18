# mod_parser.py
# Full ProTracker / SoundTracker MOD parser
# Supports: M.K., 4CHN, 6CHN, 8CHN, FLT4, CD81, and most common variants
# Returns a clean ModSong object ready for conversion to Furnace .fur

import struct
from pathlib import Path
from dataclasses import dataclass, field
from typing import List

# Standard ProTracker period table (C-1 to B-3)
PERIOD_TABLE = [
    856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,   # Octave 1
    428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,   # Octave 2
    214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113,   # Octave 3
    107, 101,  95,  90,  85,  80,  75,  71,  67,  63,  60,  56    # Octave 4+
]

NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]


@dataclass
class ModSample:
    name: str = ""
    length: int = 0
    finetune: int = 0
    volume: int = 0
    loop_start: int = 0
    loop_length: int = 0
    data: List[int] = field(default_factory=list)


@dataclass
class ModNote:
    note: int = 0      # Furnace note (0=empty, 1=C, ..., 12=B)
    octave: int = 3
    instrument: int = -1  # -1 = no instrument (MOD 0); 0-30 = Furnace instrument
    effect: int = 0
    effect_arg: int = 0


class ModSong:
    def __init__(self):
        self.name: str = ""
        self.channels: int = 0
        self.song_length: int = 0
        self.restart_position: int = 0
        self.orders: List[int] = []
        self.patterns: List[List[List[ModNote]]] = []
        self.samples: List[ModSample] = []
        self.initial_speed: int = 6    # ticks per row (MOD default)
        self.initial_bpm: int = 125    # beats per minute (MOD default)

    def limit_to_6_channels(self):
        if self.channels <= 6:
            return
        print(f"WARNING: MOD has {self.channels} channels. Keeping only first 6.")
        self.channels = 6
        for pat in self.patterns:
            for row in pat:
                row[:] = row[:6]


def period_to_note_and_octave(period: int) -> tuple[int, int]:
    """Convert MOD period to (Furnace note, octave)"""
    if period == 0:
        return 0, 3

    # Find closest period
    best_idx = 0
    best_diff = abs(PERIOD_TABLE[0] - period)
    for i, p in enumerate(PERIOD_TABLE):
        diff = abs(p - period)
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    note = (best_idx % 12) + 1
    octave = (best_idx // 12) + 3   # ProTracker oct 1 = Furnace oct 3
    return note, octave


def parse_mod(file_path: str) -> ModSong:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(path, "rb") as f:
        data = f.read()

    song = ModSong()
    offset = 0

    # Song name
    song.name = data[offset:offset+20].decode("ascii", errors="replace").strip()
    offset += 20

    # 31 samples
    for i in range(31):
        sample = ModSample()
        sample.name = data[offset:offset+22].decode("ascii", errors="replace").strip()
        offset += 22

        sample.length = struct.unpack(">H", data[offset:offset+2])[0] * 2
        offset += 2

        sample.finetune = data[offset] & 0x0F
        offset += 1
        sample.volume = data[offset]
        offset += 1

        sample.loop_start = struct.unpack(">H", data[offset:offset+2])[0] * 2
        offset += 2
        sample.loop_length = struct.unpack(">H", data[offset:offset+2])[0] * 2
        offset += 2

        song.samples.append(sample)

    # Song length and restart
    song.song_length = data[offset]
    offset += 1
    song.restart_position = data[offset]
    offset += 1

    # Pattern order table
    song.orders = list(data[offset:offset+128])
    offset += 128

    # Magic ID
    magic = data[offset:offset+4].decode("ascii", errors="replace")
    offset += 4

    # Detect channels
    if magic in ("M.K.", "FLT4", "CD81", "4CHN"):
        song.channels = 4
    elif magic in ("6CHN",):
        song.channels = 6
    elif magic in ("8CHN",):
        song.channels = 8
    else:
        highest = max(song.orders[:song.song_length]) if song.orders else 0
        song.channels = 4 if highest < 64 else 8

    print(f"Detected MOD: {magic} → {song.channels} channels")

    num_patterns = max(song.orders[:song.song_length]) + 1 if song.orders else 0

    # Parse patterns
    for pat_id in range(num_patterns):
        pattern = []
        for row in range(64):
            row_data = []
            for ch in range(song.channels):
                note_data = data[offset:offset+4]
                offset += 4

                a, b, c, d = note_data
                period = ((a & 0x0F) << 8) | b
                instrument = (a & 0xF0) | (c >> 4)
                effect = c & 0x0F
                effect_arg = d

                note, octave = period_to_note_and_octave(period)

                # MOD instruments are 1-based; 0 means "no instrument"
                fur_instrument = instrument - 1 if instrument > 0 else -1

                mod_note = ModNote(
                    note=note,
                    octave=octave,
                    instrument=fur_instrument,
                    effect=effect,
                    effect_arg=effect_arg
                )
                row_data.append(mod_note)
            pattern.append(row_data)
        song.patterns.append(pattern)

    # Load sample data
    for sample in song.samples:
        if sample.length > 0:
            sample.data = list(data[offset:offset + sample.length])
            offset += sample.length

    print(f"Parsed MOD: '{song.name}' — {song.channels} channels, {num_patterns} patterns, {len(song.samples)} samples")

    # Scan first pattern row 0 for initial speed/BPM (effect 0x0F)
    if song.patterns and song.patterns[song.orders[0]]:
        for note in song.patterns[song.orders[0]][0]:
            if note.effect == 0x0F and note.effect_arg > 0:
                if note.effect_arg < 0x20:
                    song.initial_speed = note.effect_arg
                    print(f"  Initial speed from 0xF{note.effect_arg:02X}: {note.effect_arg} ticks/row")
                else:
                    song.initial_bpm = note.effect_arg
                    print(f"  Initial BPM from 0xF{note.effect_arg:02X}: {note.effect_arg}")

    return song