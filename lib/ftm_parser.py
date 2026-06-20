# ftm_parser.py
# FamiTracker .ftm parser
# Supports: Original FamiTracker and 0CC-FamiTracker file formats
# Returns a ModSong object for the existing converter pipeline.
#
# Reference: 0CC-FamiTracker source (FamiTrackerDocIO.cpp, DocumentFile.cpp,
#            InstrumentIO.cpp, Effect.h, PatternNote.h)
#
# File structure: "FamiTracker Module" magic (18 bytes) + version (4 bytes LE)
# then named blocks: [16-byte ID][4-byte version][4-byte size][data...]
# terminated by "END" block.

import struct
import math
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from mod_parser import ModSong, ModSample, ModNote


# ---------------------------------------------------------------------------
# FTM effect_t enum values (from 0CC-FamiTracker Effect.h)
# These are the same for both original FT and 0CC pre-0.5.0 files.
# ---------------------------------------------------------------------------
EF_NONE         = 0
EF_SPEED        = 1
EF_JUMP         = 2
EF_SKIP         = 3
EF_HALT         = 4
EF_VOLUME       = 5
EF_PORTAMENTO   = 6   # tone portamento (3xx in tracker)
EF_PORTAOFF     = 7   # unused legacy
EF_SWEEPUP      = 8
EF_SWEEPDOWN    = 9
EF_ARPEGGIO     = 10  # 0xy in tracker
EF_VIBRATO      = 11  # 4xy
EF_TREMOLO      = 12  # 7xy
EF_PITCH        = 13  # Pxx
EF_DELAY        = 14  # Gxx note delay
EF_DAC          = 15  # Zxx
EF_PORTA_UP     = 16  # 1xx
EF_PORTA_DOWN   = 17  # 2xx
EF_DUTY_CYCLE   = 18  # Vxx
EF_SAMPLE_OFFSET = 19 # Yxx
EF_SLIDE_UP     = 20  # Qxy
EF_SLIDE_DOWN   = 21  # Rxy
EF_VOLUME_SLIDE = 22  # Axy
EF_NOTE_CUT     = 23  # Sxx
EF_RETRIGGER    = 24  # Xxx
EF_DELAYED_VOLUME = 25

# FTM note values
NOTE_NONE    = 0
NOTE_RELEASE = 13
NOTE_HALT    = 14
NOTE_ECHO    = 15

# NES channel type indices (in HEADER block)
CH_PULSE1   = 0
CH_PULSE2   = 1
CH_TRIANGLE = 2
CH_NOISE    = 3
CH_DPCM     = 4

# Expansion channel base indices
CH_VRC6_PULSE1  = 5
CH_VRC6_PULSE2  = 6
CH_VRC6_SAW     = 7
CH_MMC5_PULSE1  = 8
CH_MMC5_PULSE2  = 9
CH_MMC5_PCM     = 10
CH_N163_BASE    = 11  # 11..18
CH_FDS          = 19
CH_VRC7_BASE    = 20  # 20..25
CH_S5B_BASE     = 26  # 26..28

FTM_MAGIC = b"FamiTracker Module"

# Instrument types
INST_2A03 = 0
INST_VRC6 = 1
INST_VRC7 = 2
INST_FDS  = 3
INST_N163 = 4
INST_S5B  = 5

# Sequence types
SEQ_COUNT = 5  # volume, arpeggio, pitch, hi-pitch, duty

DEFAULT_TEMPO_NTSC = 150
DEFAULT_TEMPO_PAL  = 125
DEFAULT_SPEED      = 6


# ---------------------------------------------------------------------------
# Block reader helper
# ---------------------------------------------------------------------------
class BlockReader:
    """Reads from a block's data bytes with a position cursor."""
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def get_int(self) -> int:
        val = struct.unpack_from('<i', self.data, self.pos)[0]
        self.pos += 4
        return val

    def get_uint(self) -> int:
        val = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return val

    def get_char(self) -> int:
        """Read one signed byte."""
        val = struct.unpack_from('<b', self.data, self.pos)[0]
        self.pos += 1
        return val

    def get_uchar(self) -> int:
        """Read one unsigned byte."""
        val = self.data[self.pos]
        self.pos += 1
        return val

    def get_bytes(self, n: int) -> bytes:
        val = self.data[self.pos:self.pos + n]
        self.pos += n
        return val

    def get_string(self) -> str:
        """Read null-terminated string."""
        end = self.data.index(0, self.pos)
        s = self.data[self.pos:end].decode('ascii', errors='replace')
        self.pos = end + 1
        return s

    def done(self) -> bool:
        return self.pos >= len(self.data)

    def remaining(self) -> int:
        return len(self.data) - self.pos


# ---------------------------------------------------------------------------
# FTM effect -> MOD effect mapping
# ---------------------------------------------------------------------------
def _map_ftm_effect(fx: int, param: int) -> Tuple[int, int]:
    """Map FTM effect byte to (MOD effect, MOD param)."""
    if fx == EF_NONE:
        return 0, 0
    elif fx == EF_SPEED:
        return 0x0F, param   # speed (<0x20) or tempo (>=0x20)
    elif fx == EF_JUMP:
        return 0x0B, param
    elif fx == EF_SKIP:
        # FTM skip param is plain decimal row, MOD 0x0D uses BCD
        row = param & 0xFF
        bcd = ((row // 10) << 4) | (row % 10)
        return 0x0D, bcd
    elif fx == EF_HALT:
        return 0x0D, 0x00    # halt = pattern break to row 0
    elif fx == EF_VOLUME:
        return 0x0C, param
    elif fx == EF_PORTAMENTO:
        return 0x03, param
    elif fx == EF_PORTAOFF:
        return 0x03, 0       # legacy: portamento off
    elif fx == EF_ARPEGGIO:
        return 0x00, param
    elif fx == EF_VIBRATO:
        return 0x04, param
    elif fx == EF_TREMOLO:
        return 0x07, param
    elif fx == EF_PITCH:
        # Pxx: pitch slide, 0x80 = center. <0x80 = slide up, >0x80 = slide down
        if param < 0x80:
            return 0x01, param        # treat as portamento up
        elif param > 0x80:
            return 0x02, param - 0x80  # portamento down
        return 0, 0
    elif fx == EF_DELAY:
        return 0x0E, 0xD0 | (param & 0x0F)  # note delay
    elif fx == EF_PORTA_UP:
        return 0x01, param
    elif fx == EF_PORTA_DOWN:
        return 0x02, param
    elif fx == EF_VOLUME_SLIDE:
        return 0x0A, param
    elif fx == EF_NOTE_CUT:
        return 0x0E, 0xC0 | (param & 0x0F)
    elif fx == EF_RETRIGGER:
        return 0x0E, 0x90 | (param & 0x0F)
    elif fx == EF_SLIDE_UP:
        # Qxy: x=speed, y=semitones -- approximate as portamento up
        return 0x01, param
    elif fx == EF_SLIDE_DOWN:
        return 0x02, param
    elif fx == EF_SAMPLE_OFFSET:
        return 0x09, param
    # Effects not mappable to MOD (chip-specific, duty, etc.)
    return 0, 0


# ---------------------------------------------------------------------------
# SEQUENCES block parsing
# ---------------------------------------------------------------------------
SEQ_VOLUME  = 0
SEQ_ARPEGGIO = 1
SEQ_PITCH   = 2
SEQ_HIPITCH = 3
SEQ_DUTY    = 4


def _convert_old_sequence(values: List[int], lengths: List[int], seq_type: int) -> dict:
    """Convert old-format (value, length) pairs to modern expanded sequence.

    Old format: each item has a value and a length byte.
    Length >= 0: repeat value (length+1) times.
    Length < 0: loop marker, jump back |length| items.
    For Pitch/HiPitch, only the first repetition uses the value; rest are 0.

    Returns dict with 'values' (list), 'loop' (int or -1), 'release' (-1).
    """
    expanded = []
    loop_ticks_after = 0
    has_loop = False
    is_pitch = seq_type in (SEQ_PITCH, SEQ_HIPITCH)
    count = len(values)

    for i in range(count):
        if lengths[i] < 0:
            has_loop = True
            # Sum ticks of items being looped over to find loop position
            start_idx = max(0, i + lengths[i])
            loop_ticks_after = sum(lengths[j] + 1 for j in range(start_idx, i))
        else:
            for rep in range(lengths[i] + 1):
                expanded.append(0 if (is_pitch and rep > 0) else values[i])

    loop_point = -1
    if has_loop:
        loop_point = max(0, len(expanded) - loop_ticks_after)

    return {'values': expanded, 'loop': loop_point, 'release': -1}


def _parse_sequences(blocks: Dict[str, Tuple[int, bytes]]) -> Dict[Tuple[int, int], dict]:
    """Parse the SEQUENCES block.

    Returns dict of (seq_type, seq_index) -> {'values': [...], 'loop': int, 'release': int}.
    Handles block versions 1-6.
    """
    if "SEQUENCES" not in blocks:
        return {}

    ver, bdata = blocks["SEQUENCES"]
    br = BlockReader(bdata)
    result: Dict[Tuple[int, int], dict] = {}

    count = br.get_uint()

    if ver <= 2:
        # Old pair format: each item is (value_byte, length_byte)
        for i in range(count):
            if ver == 1:
                # Version 1: only Index, no Type. Stored linearly.
                idx = br.get_uint()
                seq_type = i % SEQ_COUNT
                seq_index = i // SEQ_COUNT
            else:
                # Version 2: Index + Type
                seq_index = br.get_uint()
                seq_type = br.get_uint()
            seq_count = br.get_uchar()
            values = []
            lengths = []
            for _ in range(seq_count):
                val = br.get_char()  # signed value
                length = br.get_char()  # signed length/marker
                values.append(val)
                lengths.append(length)
            seq = _convert_old_sequence(values, lengths, seq_type)
            result[(seq_type, seq_index)] = seq

    else:
        # Version 3+: modern format with explicit loop point, single-byte values
        for i in range(count):
            seq_index = br.get_uint()
            seq_type = br.get_uint()
            seq_count = br.get_uchar()
            loop_point = br.get_int()  # signed, -1 = no loop

            # Version 4: inline release + setting
            release_point = -1
            setting = 0
            if ver == 4:
                release_point = br.get_int()
                setting = br.get_int()

            values = []
            for _ in range(seq_count):
                values.append(br.get_char())

            result[(seq_type, seq_index)] = {
                'values': values,
                'loop': loop_point,
                'release': release_point,
            }

        # Version 5-6: release/setting appended after all entries
        if ver >= 5:
            for i in range(count):
                release = br.get_int()
                setting = br.get_int()
                # Need to match back to entries -- they're in the same order
            # Re-read: version 5/6 store release/setting in a second pass
            # We need to pair them up. Reset and re-parse the second pass.
            # The entries were stored in order, so zip with the same keys.
            keys_in_order = []
            br2 = BlockReader(bdata)
            br2.pos = 4  # skip count
            for i in range(count):
                si = br2.get_uint()
                st = br2.get_uint()
                sc = br2.get_uchar()
                br2.get_int()  # loop
                if ver == 4:
                    br2.get_int()  # release
                    br2.get_int()  # setting
                br2.get_bytes(sc)
                keys_in_order.append((st, si))
            # Now read release/setting from where br left off
            for key in keys_in_order:
                rel = br.get_int()
                sett = br.get_int()
                if key in result:
                    result[key]['release'] = rel

    return result


# ---------------------------------------------------------------------------
# Instrument data reading -- returns sequence references alongside advancing
# ---------------------------------------------------------------------------
def _read_instrument_data(br: BlockReader, itype: int, block_ver: int
                          ) -> Tuple[List[Tuple[int, int]], Dict[int, int]]:
    """Read one instrument's type-specific data from the INSTRUMENTS block.

    Returns:
        seq_refs: list of (enabled, seq_index) for 5 sequence types (Vol,Arp,Pitch,HiPitch,Duty)
        dpcm_map: dict of note_index -> dpcm_sample_index (only for INST_2A03)
    """
    seq_refs: List[Tuple[int, int]] = []
    dpcm_map: Dict[int, int] = {}

    if itype in (INST_2A03, INST_VRC6, INST_S5B):
        # IOSeq: int(seq_count) + seq_count * (char enabled, char index)
        seq_count = br.get_int()
        for _ in range(seq_count):
            enabled = br.get_uchar()
            index = br.get_uchar()
            seq_refs.append((enabled, index))

        if itype == INST_2A03:
            # DPCM assignments: per note (12 notes * octaves)
            octaves = 6 if block_ver == 1 else 8
            for note_idx in range(12 * octaves):
                sample_idx = br.get_uchar()
                br.get_uchar()  # pitch
                if block_ver > 5:
                    br.get_uchar()  # delta value
                if sample_idx > 0:
                    dpcm_map[note_idx] = sample_idx - 1  # 1-based in file

    elif itype == INST_VRC7:
        br.get_int()  # patch number
        br.get_bytes(8)  # 8 custom registers

    elif itype == INST_FDS:
        br.get_bytes(64)  # wave samples
        br.get_bytes(32)  # mod table
        br.get_int()  # mod speed
        br.get_int()  # mod depth
        br.get_int()  # mod delay
        a = struct.unpack_from('<I', br.data, br.pos)[0]
        b = struct.unpack_from('<I', br.data, br.pos + 4)[0]
        if a < 256 and (b & 0xFF) != 0x00:
            pass  # Old format -- skip
        else:
            for _ in range(3 if block_ver > 2 else 2):
                seq_count = br.get_uchar()
                br.get_int()  # loop
                br.get_int()  # release
                br.get_int()  # setting
                br.get_bytes(seq_count)

    elif itype == INST_N163:
        seq_count = br.get_int()
        for _ in range(seq_count):
            enabled = br.get_uchar()
            index = br.get_uchar()
            seq_refs.append((enabled, index))
        wave_size = br.get_int()
        br.get_int()  # wave pos
        if block_ver >= 8:
            br.get_int()  # auto wave pos flag
        wave_count = br.get_int()
        br.get_bytes(wave_count * wave_size)

    else:
        raise ValueError(f"Unknown FTM instrument type {itype}")

    return seq_refs, dpcm_map


# ---------------------------------------------------------------------------
# Synthetic PCM generation for canonical NES waveforms
# ---------------------------------------------------------------------------
_SYNTH_CYCLES = 8       # repeat waveform this many times
_SYNTH_PERIOD = 64      # samples per cycle

def _synth_square(cycles: int = _SYNTH_CYCLES, period: int = _SYNTH_PERIOD) -> List[int]:
    """50% duty square wave, unsigned 8-bit PCM (128 = center)."""
    one_cycle = [192] * (period // 2) + [64] * (period // 2)
    return (one_cycle * cycles)

def _synth_triangle(cycles: int = _SYNTH_CYCLES, period: int = _SYNTH_PERIOD) -> List[int]:
    """Triangle wave, unsigned 8-bit PCM."""
    one_cycle = []
    for i in range(period):
        val = int(round(128 + 127 * (1 - abs(4 * i / period - 2) + 1) / 2))
        # Proper triangle: ramp up then down
        t = i / period
        if t < 0.5:
            val = int(round(64 + 128 * (t * 2)))
        else:
            val = int(round(64 + 128 * (2 - t * 2)))
        one_cycle.append(max(0, min(255, val)))
    return (one_cycle * cycles)

def _synth_sawtooth(cycles: int = _SYNTH_CYCLES, period: int = _SYNTH_PERIOD) -> List[int]:
    """Sawtooth wave, unsigned 8-bit PCM."""
    one_cycle = [int(round(32 + 192 * i / period)) for i in range(period)]
    return (one_cycle * cycles)

def _synth_noise(length: int = 512) -> List[int]:
    """Pseudo-random noise, unsigned 8-bit PCM. High ZCR for classify_sample()."""
    import random
    rng = random.Random(42)
    return [rng.randint(0, 255) for _ in range(length)]


def _make_instrument_samples(
    num_instruments: int,
    inst_names: List[str],
    inst_channel_type: Dict[int, int],
    dpcm_samples: Dict[int, Tuple[str, List[int]]],
    dpcm_assignments: Dict[int, int],
) -> List[ModSample]:
    """Create one ModSample per FTM instrument with synthetic PCM waveform data.

    Args:
        num_instruments: total instrument count (max instrument index + 1)
        inst_names: per-instrument names from the INSTRUMENTS block
        inst_channel_type: map instrument_idx -> NES channel type (from pattern scan)
        dpcm_samples: map sample_idx -> (name, pcm_data) from DPCM SAMPLES block
        dpcm_assignments: map instrument_idx -> dpcm_sample_idx (from 2A03 DPCM table)
    """
    samples = []
    for i in range(num_instruments):
        name = inst_names[i] if i < len(inst_names) else f"Inst_{i}"
        ch_type = inst_channel_type.get(i, CH_PULSE1)

        if ch_type in (CH_PULSE1, CH_PULSE2):
            pcm = _synth_square()
            if not name:
                name = f"Pulse {i}"
        elif ch_type == CH_TRIANGLE:
            pcm = _synth_triangle()
            if not name:
                name = f"Triangle {i}"
        elif ch_type == CH_NOISE:
            pcm = _synth_noise()
            # Ensure "noise" is in the name for classify_sample() detection
            if name and "noise" not in name.lower():
                name = f"Noise: {name}"
            elif not name:
                name = f"Noise {i}"
        elif ch_type == CH_DPCM:
            # Check for actual DPCM sample data
            dpcm_idx = dpcm_assignments.get(i)
            if dpcm_idx is not None and dpcm_idx in dpcm_samples:
                dpcm_name, pcm = dpcm_samples[dpcm_idx]
                if not name:
                    name = dpcm_name or f"DPCM {i}"
            else:
                pcm = _synth_square()  # fallback
                if not name:
                    name = f"DPCM {i}"
        elif ch_type == CH_VRC6_SAW:
            pcm = _synth_sawtooth()
            if not name:
                name = f"VRC6 Saw {i}"
        elif ch_type in (CH_VRC6_PULSE1, CH_VRC6_PULSE2):
            pcm = _synth_square()
            if not name:
                name = f"VRC6 Pulse {i}"
        else:
            pcm = _synth_square()
            if not name:
                name = f"Inst {i}"

        loop_start = 0
        loop_length = len(pcm) if ch_type != CH_NOISE else 0

        samples.append(ModSample(
            name=name,
            length=len(pcm),
            volume=64,
            loop_start=loop_start,
            loop_length=loop_length,
            data=pcm,
        ))
    return samples


# ---------------------------------------------------------------------------
# DPCM SAMPLES block parser
# ---------------------------------------------------------------------------
def _parse_dpcm_samples(blocks: Dict[str, Tuple[int, bytes]]) -> Dict[int, Tuple[str, List[int]]]:
    """Parse the DPCM SAMPLES block into a dict of sample_idx -> (name, pcm_data).

    NES DPCM is 1-bit delta-encoded at ~33kHz. We decode to unsigned 8-bit PCM.
    """
    result: Dict[int, Tuple[str, List[int]]] = {}
    if "DPCM SAMPLES" not in blocks:
        return result

    ver, bdata = blocks["DPCM SAMPLES"]
    br = BlockReader(bdata)
    count = br.get_uchar()

    for _ in range(count):
        idx = br.get_uchar()
        name_len = br.get_int()
        name = br.get_bytes(name_len).decode('ascii', errors='replace') if name_len > 0 else ""
        size = br.get_int()
        raw = br.get_bytes(size)

        # Decode NES DPCM: 1-bit delta modulation
        # Each byte has 8 bits, LSB first. Counter starts at 64 (range 0-127).
        # Bit=1 -> counter += 2, bit=0 -> counter -= 2, clamped to [0, 126].
        pcm = []
        counter = 64
        for byte_val in raw:
            for bit in range(8):
                if byte_val & (1 << bit):
                    counter = min(126, counter + 2)
                else:
                    counter = max(0, counter - 2)
                # Scale 0-126 to unsigned 8-bit (0-255)
                pcm.append(int(round(counter * 255 / 126)))

        result[idx] = (name, pcm)
        if len(pcm) > 0:
            print(f"    DPCM sample {idx}: '{name}' {len(pcm)} samples ({size} bytes raw)")

    return result


# ---------------------------------------------------------------------------
# Extract instrument names and sequence references from INSTRUMENTS block
# ---------------------------------------------------------------------------
def _extract_instrument_info(blocks: Dict[str, Tuple[int, bytes]], file_version: int
                             ) -> Tuple[List[str], Dict[int, List[Tuple[int, int]]], Dict[int, int]]:
    """Parse the INSTRUMENTS block.

    Returns:
        names: per-instrument name list
        inst_seq_refs: map instrument_idx -> list of (enabled, seq_index) for 5 seq types
        dpcm_assignments: map instrument_idx -> first referenced dpcm_sample_idx
    """
    if "INSTRUMENTS" not in blocks:
        return [], {}, {}

    ver, bdata = blocks["INSTRUMENTS"]
    ins_count = struct.unpack_from('<I', bdata, 0)[0]
    is_original_ft = file_version < 0x0440

    names = []
    inst_seq_refs: Dict[int, List[Tuple[int, int]]] = {}
    dpcm_assignments: Dict[int, int] = {}
    br = BlockReader(bdata)
    br.pos = 4  # skip count

    for i in range(ins_count):
        if br.remaining() < 5:
            names.append("")
            continue

        inst_idx = br.get_uint()
        raw_type = br.get_uchar()
        itype = (raw_type - 1) if is_original_ft and raw_type > 0 else raw_type

        try:
            seq_refs, dpcm_map = _read_instrument_data(br, itype, ver)
            inst_seq_refs[inst_idx] = seq_refs
            if dpcm_map:
                # Use first DPCM assignment found
                first_sample = next(iter(dpcm_map.values()))
                dpcm_assignments[inst_idx] = first_sample
        except (struct.error, IndexError, ValueError):
            names.append("")
            continue

        # Read name: uint32 length + bytes
        if br.remaining() >= 4:
            name_len = br.get_uint()
            if name_len > 0 and name_len < 256 and br.remaining() >= name_len:
                name = br.get_bytes(name_len).decode('ascii', errors='replace')
                names.append(name)
            else:
                names.append("")
        else:
            names.append("")

    return names, inst_seq_refs, dpcm_assignments


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------
def parse_ftm(file_path: str) -> ModSong:
    """Parse a FamiTracker .ftm file into a ModSong."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(path, "rb") as f:
        data = f.read()

    # --- File header ---
    if data[:18] != FTM_MAGIC:
        raise ValueError("Not a FamiTracker module (bad magic)")
    file_version = struct.unpack_from('<I', data, 18)[0]
    if file_version < 0x0100:
        raise ValueError(f"FTM version too old: 0x{file_version:04X}")
    print(f"FTM file version: 0x{file_version:04X}")

    # --- Read all blocks ---
    blocks: Dict[str, Tuple[int, bytes]] = {}  # name -> (version, data)
    pos = 22
    while pos < len(data) - 16:
        block_id = data[pos:pos + 16].split(b'\x00')[0].decode('ascii', errors='replace')
        if block_id == "END":
            break
        block_ver = struct.unpack_from('<I', data, pos + 16)[0]
        block_size = struct.unpack_from('<I', data, pos + 20)[0]
        block_data = data[pos + 24:pos + 24 + block_size]
        blocks[block_id] = (block_ver, block_data)
        pos += 24 + block_size

    # --- PARAMS ---
    params_ver, params_data = blocks["PARAMS"]
    br = BlockReader(params_data)

    expansion_flag = 0
    if params_ver == 1:
        initial_speed = br.get_int()
    else:
        expansion_flag = br.get_uchar()
    num_channels = br.get_int()
    machine = br.get_int()  # 0=NTSC, 1=PAL
    engine_speed = br.get_int()

    # Vibrato style (ver > 2)
    if params_ver > 2:
        br.get_int()  # vibrato style

    # Speed split point (ver >= 6)
    speed_split = 21  # old default
    if params_ver >= 6:
        speed_split = br.get_int()

    print(f"  Channels: {num_channels}, Machine: {'NTSC' if machine == 0 else 'PAL'}, "
          f"Expansion: 0x{expansion_flag:02X}")

    # Derive initial speed/tempo for PARAMS ver 1
    if params_ver == 1:
        if initial_speed > 19:
            initial_tempo = initial_speed
            initial_speed = DEFAULT_SPEED
        else:
            initial_tempo = DEFAULT_TEMPO_NTSC if machine == 0 else DEFAULT_TEMPO_PAL
    else:
        initial_speed = DEFAULT_SPEED
        initial_tempo = DEFAULT_TEMPO_NTSC if machine == 0 else DEFAULT_TEMPO_PAL

    # --- INFO ---
    if "INFO" in blocks:
        _, info_data = blocks["INFO"]
        song_name = info_data[0:32].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        song_author = info_data[32:64].split(b'\x00')[0].decode('ascii', errors='replace').strip()
        print(f"  Name: '{song_name}', Author: '{song_author}'")
    else:
        song_name = ""

    # --- HEADER ---
    header_ver, header_data = blocks["HEADER"]
    br = BlockReader(header_data)

    num_songs = 1
    channel_types: List[int] = []
    fx_columns: List[int] = []  # per-channel effect column count

    if header_ver == 1:
        # Single track
        for _ in range(num_channels):
            ch_type = br.get_uchar()
            fx_cols = br.get_uchar() + 1
            channel_types.append(ch_type)
            fx_columns.append(fx_cols)
    elif header_ver >= 2:
        num_songs = br.get_uchar() + 1  # 0-based stored
        # Song names (ver >= 3)
        if header_ver >= 3:
            for _ in range(num_songs):
                br.get_string()  # song name, null-terminated
        # Channel types + effect columns per song
        for _ in range(num_channels):
            ch_type = br.get_uchar()
            channel_types.append(ch_type)
            cols = []
            if header_ver == 1:
                cols.append(br.get_uchar() + 1)
            else:
                for _ in range(num_songs):
                    cols.append(br.get_uchar() + 1)
            fx_columns.append(max(cols))  # use max across songs

    print(f"  Songs: {num_songs}, Channel types: {channel_types}")

    # --- INSTRUMENTS (names + sequence references) ---
    ins_count = 0
    inst_names: List[str] = []
    inst_seq_refs: Dict[int, List[Tuple[int, int]]] = {}
    dpcm_assignments: Dict[int, int] = {}
    if "INSTRUMENTS" in blocks:
        ins_ver, ins_data = blocks["INSTRUMENTS"]
        ins_count = struct.unpack_from('<I', ins_data, 0)[0]
        inst_names, inst_seq_refs, dpcm_assignments = _extract_instrument_info(blocks, file_version)
        print(f"  Instruments: {ins_count} (names: {inst_names})")

    # --- SEQUENCES (volume/arpeggio/pitch/duty envelopes) ---
    sequences = _parse_sequences(blocks)
    if sequences:
        vol_seqs = sum(1 for (t, _) in sequences if t == SEQ_VOLUME)
        print(f"  Sequences: {len(sequences)} total ({vol_seqs} volume envelopes)")

    # --- FRAMES (order list) ---
    frames_ver, frames_data = blocks["FRAMES"]
    br = BlockReader(frames_data)

    # We only convert the first song
    if frames_ver == 1:
        frame_count = br.get_int()
        br.get_int()  # channel count (unused, already known)
        pattern_length = 64  # default for ver 1
        # Speed/tempo already from PARAMS
        orders_per_frame = []
        for f in range(frame_count):
            frame_orders = []
            for _ in range(num_channels):
                frame_orders.append(br.get_uchar())
            orders_per_frame.append(frame_orders)
    else:
        frame_count = br.get_int()
        speed_val = br.get_int()
        if frames_ver >= 3:
            tempo_val = br.get_int()
            initial_speed = speed_val
            initial_tempo = tempo_val
        else:
            if speed_val < 20:
                initial_speed = speed_val
            else:
                initial_tempo = speed_val
        pattern_length = br.get_int()
        orders_per_frame = []
        for f in range(frame_count):
            frame_orders = []
            for _ in range(num_channels):
                frame_orders.append(br.get_uchar())
            orders_per_frame.append(frame_orders)

        # Skip remaining songs if multi-song
        for song_idx in range(1, num_songs):
            fc = br.get_int()
            br.get_int()  # speed
            if frames_ver >= 3:
                br.get_int()  # tempo
            br.get_int()  # pattern length
            for _ in range(fc):
                for _ in range(num_channels):
                    br.get_uchar()

    print(f"  Frames: {frame_count}, Pattern length: {pattern_length}, "
          f"Speed: {initial_speed}, Tempo: {initial_tempo}")

    # --- PATTERNS ---
    pat_ver, pat_data = blocks["PATTERNS"]
    br = BlockReader(pat_data)

    compat200 = (file_version == 0x0200)

    if pat_ver == 1:
        pattern_length = br.get_int()

    # Storage: patterns_db[(track, channel, pattern_idx)] = list of (row, ModNote)
    patterns_db: Dict[Tuple[int, int, int], Dict[int, ModNote]] = {}

    while not br.done():
        track = 0
        if pat_ver > 1:
            track = br.get_int()

        channel = br.get_int()
        pattern_idx = br.get_int()
        items = br.get_int()

        key = (track, channel, pattern_idx)
        row_dict: Dict[int, ModNote] = {}

        for _ in range(items):
            if compat200 or pat_ver >= 6:
                row = br.get_uchar()
            else:
                row = br.get_int()

            note_val = br.get_uchar()   # note_t
            octave = br.get_uchar()
            inst = br.get_uchar()
            vol = br.get_uchar()

            # Read effects
            if compat200:
                fx_count = 1
            elif pat_ver >= 6:
                fx_count = 4  # MAX_EFFECT_COLUMNS
            else:
                fx_count = fx_columns[channel] if channel < len(fx_columns) else 1

            best_fx = 0
            best_param = 0
            for _ in range(fx_count):
                eff_num = br.get_uchar()
                if eff_num != EF_NONE:
                    eff_param = br.get_uchar()
                    if best_fx == 0:
                        best_fx, best_param = _map_ftm_effect(eff_num, eff_param)
                elif pat_ver < 6:
                    br.get_uchar()  # unused blank param

            # Map note
            fur_note = 0
            fur_octave = octave
            fur_inst = -1

            if note_val == NOTE_NONE:
                fur_note = 0
            elif note_val == NOTE_HALT:
                fur_note = 100  # note-off
                fur_octave = 0
            elif note_val == NOTE_RELEASE:
                fur_note = 100
                fur_octave = 0
            elif 1 <= note_val <= 12:
                fur_note = note_val  # 1=C..12=B, same as Furnace
                fur_octave = octave

            # Instrument: FTM uses 0-based, 0xFF = none
            if inst < 0x40:
                fur_inst = inst
            # else: no instrument

            # Volume: FTM 0-15, 0x10 = none
            xm_vol = -1
            if vol <= 0x0F:
                # FTM vol 0-15 maps to MOD 0-64 range
                mod_vol = vol * 4  # approximate: 15 -> 60
                if best_fx == 0 and best_param == 0:
                    best_fx = 0x0C
                    best_param = mod_vol
                else:
                    xm_vol = mod_vol

            mn = ModNote(
                note=fur_note,
                octave=fur_octave,
                instrument=fur_inst,
                effect=best_fx,
                effect_arg=best_param,
                xm_volume=xm_vol,
            )
            row_dict[row] = mn

        patterns_db[key] = row_dict

    # --- Scan patterns to build instrument -> channel type mapping ---
    # Count how many notes each instrument has per channel type, pick majority
    inst_ch_counts: Dict[int, Dict[int, int]] = {}  # inst -> {ch_type -> count}
    for (track, channel, pat_idx), row_dict in patterns_db.items():
        if track != 0:
            continue  # only first song
        ch_type = channel_types[channel] if channel < len(channel_types) else CH_PULSE1
        for row, mn in row_dict.items():
            if mn.instrument >= 0:
                counts = inst_ch_counts.setdefault(mn.instrument, {})
                counts[ch_type] = counts.get(ch_type, 0) + 1

    inst_channel_type: Dict[int, int] = {}
    for inst_idx, counts in inst_ch_counts.items():
        # Pick channel type with most notes for this instrument
        inst_channel_type[inst_idx] = max(counts, key=counts.get)

    # --- Parse DPCM samples ---
    dpcm_samples = _parse_dpcm_samples(blocks)

    # --- Build ModSong ---
    song = ModSong()
    song.name = song_name
    song.channels = num_channels
    song.initial_speed = initial_speed
    song.initial_bpm = initial_tempo

    # Determine total instrument count (max index seen + 1, or ins_count)
    max_inst = max(inst_channel_type.keys()) if inst_channel_type else -1
    num_instruments = max(ins_count, max_inst + 1)

    # Create per-instrument samples with synthetic waveform PCM data
    song.samples = _make_instrument_samples(
        num_instruments, inst_names, inst_channel_type,
        dpcm_samples, dpcm_assignments,
    )

    # --- Attach FTM sequence envelopes to ModSamples ---
    # Look up each instrument's volume and arpeggio sequence references and
    # attach the expanded envelope data.
    for inst_idx in range(num_instruments):
        sample = song.samples[inst_idx]
        seq_refs = inst_seq_refs.get(inst_idx, [])

        # Volume envelope: scale NES 0-15 to PCE 0-31
        sample.ftm_volume_env = None
        sample.ftm_volume_loop = 255
        sample.ftm_volume_release = 255
        if len(seq_refs) > SEQ_VOLUME:
            enabled, seq_index = seq_refs[SEQ_VOLUME]
            if enabled:
                seq = sequences.get((SEQ_VOLUME, seq_index))
                if seq and seq['values']:
                    pce_values = [min(31, max(0, v * 2)) for v in seq['values']]
                    sample.ftm_volume_env = pce_values
                    sample.ftm_volume_loop = seq['loop'] if seq['loop'] >= 0 else 255
                    sample.ftm_volume_release = seq['release'] if seq['release'] >= 0 else 255

        # Arpeggio envelope: values are signed semitone offsets, pass through directly
        sample.ftm_arp_env = None
        sample.ftm_arp_loop = 255
        if len(seq_refs) > SEQ_ARPEGGIO:
            enabled, seq_index = seq_refs[SEQ_ARPEGGIO]
            if enabled:
                seq = sequences.get((SEQ_ARPEGGIO, seq_index))
                if seq and seq['values']:
                    sample.ftm_arp_env = seq['values']
                    sample.ftm_arp_loop = seq['loop'] if seq['loop'] >= 0 else 255

    # Build a unique pattern list.
    # FTM stores patterns per-channel; MOD stores patterns as full rows across
    # all channels. We need to "interleave" -- each FTM frame becomes one MOD
    # pattern containing all channels.
    song.song_length = frame_count
    song.orders = list(range(frame_count))  # each frame is a unique pattern

    for frame_idx in range(frame_count):
        pattern_rows = []
        for row in range(pattern_length):
            row_notes = []
            for ch in range(num_channels):
                pat_idx = orders_per_frame[frame_idx][ch]
                key = (0, ch, pat_idx)  # track 0
                note = ModNote()
                if key in patterns_db and row in patterns_db[key]:
                    note = patterns_db[key][row]
                row_notes.append(note)
            pattern_rows.append(row_notes)
        song.patterns.append(pattern_rows)

    # Count non-empty notes for stats
    total_notes = 0
    for pat in song.patterns:
        for row in pat:
            for n in row:
                if n.note > 0 or n.effect > 0:
                    total_notes += 1

    print(f"Parsed FTM: '{song.name}' -- {song.channels} channels, "
          f"{frame_count} frames x {pattern_length} rows, "
          f"{total_notes} active notes")

    # Warn about DPCM channel
    if CH_DPCM in channel_types:
        dpcm_ch = channel_types.index(CH_DPCM)
        dpcm_notes = 0
        for pat in song.patterns:
            for row in pat:
                if dpcm_ch < len(row) and row[dpcm_ch].note > 0:
                    dpcm_notes += 1
        if dpcm_notes > 0:
            print(f"  WARNING: DPCM channel ({dpcm_notes} notes) will be mapped "
                  f"as a regular PCE channel. Consider --drop_channels={dpcm_ch + 1}")

    return song
