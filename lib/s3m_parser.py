# s3m_parser.py
# Scream Tracker 3 .S3M parser
# Returns ModSong/ModSample/ModNote objects for the existing converter pipeline.
#
# Reference: Scream Tracker 3.2x TECH.DOC, FireLight S3M format doc
# Supports: S3M v1.x/v3.x with PCM samples (type 1)

import struct
from pathlib import Path
from typing import List
from mod_parser import ModSong, ModSample, ModNote


# S3M note encoding: hi nibble = octave (0-9), lo nibble = note (0-11)
# Special: 255 = empty, 254 = note-off/key-off
def _s3m_note_to_furnace(raw_note: int):
    """Convert S3M packed note byte to (furnace_note, octave).
    S3M: hi=octave(0-9), lo=semitone(0=C..11=B). 255=empty, 254=note-off."""
    if raw_note == 255:
        return 0, 3           # empty
    if raw_note == 254:
        return 100, 0          # note-off
    semi = raw_note & 0x0F
    if semi > 11:
        return 0, 3           # invalid -> empty
    octave = (raw_note >> 4) & 0x0F
    note = semi + 1           # 1=C .. 12=B (Furnace convention)
    return note, octave


def _map_s3m_effect(cmd: int, param: int):
    """Map S3M effect command (1-based letter index) to MOD effect + param.
    S3M cmd 1=A, 2=B, 3=C, ... Returns (mod_effect, mod_param).

    Key difference from MOD: S3M Cxx (pattern break) param is plain hex,
    not BCD. We convert to the BCD encoding that the downstream MOD pipeline
    expects for effect 0x0D, so the existing BCD->decimal decoder works.
    """
    if cmd == 0:
        return 0, 0

    letter = chr(ord('A') - 1 + cmd) if cmd < 27 else '?'

    if cmd == 1:    # A: Set Speed
        return 0x0F, param if param <= 0x20 else 0x20
    elif cmd == 2:  # B: Position Jump
        return 0x0B, param
    elif cmd == 3:  # C: Pattern Break (hex param -> encode as BCD for pipeline)
        row = param & 0x3F  # S3M row 0-63
        bcd = ((row // 10) << 4) | (row % 10)
        return 0x0D, bcd
    elif cmd == 4:  # D: Volume Slide
        return 0x0A, param
    elif cmd == 5:  # E: Pitch Slide Down
        # S3M E distinguishes fine (ExF), extra-fine (EFx), and normal
        hi = (param >> 4) & 0x0F
        lo = param & 0x0F
        if hi == 0x0F:
            return 0x0E, 0x20 | lo    # Fine slide down -> E2x
        elif hi == 0x0E:
            return 0x0E, 0x20 | lo    # Extra-fine -> treat as fine (best approx)
        else:
            return 0x02, param         # Normal slide down
    elif cmd == 6:  # F: Pitch Slide Up
        hi = (param >> 4) & 0x0F
        lo = param & 0x0F
        if hi == 0x0F:
            return 0x0E, 0x10 | lo    # Fine slide up -> E1x
        elif hi == 0x0E:
            return 0x0E, 0x10 | lo    # Extra-fine -> treat as fine
        else:
            return 0x01, param         # Normal slide up
    elif cmd == 7:  # G: Portamento (tone porta)
        return 0x03, param
    elif cmd == 8:  # H: Vibrato
        return 0x04, param
    elif cmd == 9:  # I: Tremor (not supported on PCE)
        return 0, 0
    elif cmd == 10: # J: Arpeggio
        return 0x00, param
    elif cmd == 11: # K: Vibrato + Volume Slide
        return 0x06, param
    elif cmd == 12: # L: Portamento + Volume Slide
        return 0x05, param
    elif cmd == 15: # O: Sample Offset
        return 0x09, param
    elif cmd == 17: # Q: Retrigger + Volume Slide
        # S3M Qxy: x=volume change type, y=retrigger interval
        # MOD retrigger is E9y (only interval, no volume ramp)
        lo = param & 0x0F
        return 0x0E, 0x90 | lo
    elif cmd == 18: # R: Tremolo
        return 0x07, param
    elif cmd == 19: # S: Extended commands
        hi = (param >> 4) & 0x0F
        lo = param & 0x0F
        if hi == 0x0B:   # SBx: Pattern Loop
            return 0x0E, 0x60 | lo
        elif hi == 0x0C:  # SCx: Note Cut
            return 0x0E, 0xC0 | lo
        elif hi == 0x0D:  # SDx: Note Delay
            return 0x0E, 0xD0 | lo
        elif hi == 0x0E:  # SEx: Pattern Delay
            return 0x0E, 0xE0 | lo
        return 0, 0  # other S-commands not mapped
    elif cmd == 20: # T: Set Tempo (BPM)
        if param > 0x20:
            return 0x0F, param  # speed/tempo shared in MOD 0x0F
        return 0, 0
    elif cmd == 22: # V: Global Volume (not per-channel, skip)
        return 0, 0

    return 0, 0


def _parse_s3m_pattern(data: bytes, offset: int, num_channels: int):
    """Parse one S3M packed pattern at the given file offset.
    Returns list of 64 rows, each row = list of ModNote (num_channels wide)."""
    packed_len = struct.unpack_from('<H', data, offset)[0]
    pdata = data[offset + 2:offset + 2 + packed_len]

    rows = [[ModNote() for _ in range(num_channels)] for _ in range(64)]
    row = 0
    pos = 0

    while pos < len(pdata) and row < 64:
        what = pdata[pos]; pos += 1
        if what == 0:
            row += 1
            continue

        ch = what & 31
        note_val = 255
        ins_val = 0
        vol_val = 255  # 255 = no volume
        cmd_val = 0
        param_val = 0

        if what & 32:  # note + instrument
            note_val = pdata[pos]; ins_val = pdata[pos + 1]; pos += 2
        if what & 64:  # volume
            vol_val = pdata[pos]; pos += 1
        if what & 128:  # effect
            cmd_val = pdata[pos]; param_val = pdata[pos + 1]; pos += 2

        if ch >= num_channels:
            continue

        fnote, foct = _s3m_note_to_furnace(note_val)
        # S3M instruments are 1-based; convert to 0-based (-1 = none)
        instrument = ins_val - 1 if ins_val > 0 else -1

        # Map S3M effect to MOD effect
        effect, effect_arg = _map_s3m_effect(cmd_val, param_val)

        # Volume column: S3M volume 0-63 maps to MOD 0x0C
        # If effect column is free, put volume there; otherwise use xm_volume
        xm_vol = -1
        if vol_val <= 64:
            if effect == 0 and effect_arg == 0:
                effect = 0x0C
                effect_arg = vol_val
            else:
                xm_vol = vol_val

        rows[row][ch] = ModNote(
            note=fnote,
            octave=foct,
            instrument=instrument,
            effect=effect,
            effect_arg=effect_arg,
            xm_volume=xm_vol,
        )

    return rows


def parse_s3m(file_path: str) -> ModSong:
    """Parse an S3M module file into a ModSong."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(path, "rb") as f:
        data = f.read()

    song = ModSong()

    # --- Header (96 bytes) ---
    song.name = data[0:28].split(b'\x00')[0].decode('ascii', errors='replace').strip()
    sig = data[28]
    if sig != 0x1A:
        raise ValueError(f"Not a valid S3M file (sig byte = 0x{sig:02X})")
    magic = data[44:48]
    if magic != b'SCRM':
        raise ValueError(f"Not a valid S3M file (magic = {magic!r})")

    ord_count = struct.unpack_from('<H', data, 32)[0]
    ins_count = struct.unpack_from('<H', data, 34)[0]
    pat_count = struct.unpack_from('<H', data, 36)[0]
    # flags = struct.unpack_from('<H', data, 38)[0]
    tracker_ver = struct.unpack_from('<H', data, 40)[0]
    sample_fmt = struct.unpack_from('<H', data, 42)[0]  # 1=signed, 2=unsigned

    song.initial_speed = data[49] if data[49] > 0 else 6
    song.initial_bpm = data[50] if data[50] > 0 else 125
    global_vol = data[48]

    # Channel settings (32 bytes at offset 64)
    chan_settings = list(data[64:96])
    active_channels = [i for i, c in enumerate(chan_settings) if c < 255]
    num_channels = len(active_channels)
    song.channels = num_channels

    # Build channel index remap: S3M channel slot -> sequential 0..N-1
    chan_remap = {}
    for seq_idx, s3m_ch in enumerate(active_channels):
        chan_remap[s3m_ch] = seq_idx

    print(f"Detected S3M v{tracker_ver >> 8}.{tracker_ver & 0xFF:02X}: "
          f"'{song.name}', {num_channels}ch, {pat_count} pat, {ins_count} ins")
    print(f"  Speed: {song.initial_speed}, BPM: {song.initial_bpm}, "
          f"Global vol: {global_vol}, Sample fmt: {'unsigned' if sample_fmt == 2 else 'signed'}")

    # --- Orders ---
    orders_raw = list(data[96:96 + ord_count])
    # Filter out markers: 254 = skip/marker, 255 = end-of-song
    song.orders = [o for o in orders_raw if o < 254]
    song.song_length = len(song.orders)

    # --- Parapointers ---
    ptr_base = 96 + ord_count
    ins_ptrs = [struct.unpack_from('<H', data, ptr_base + i * 2)[0]
                for i in range(ins_count)]
    pat_ptrs_off = ptr_base + ins_count * 2
    pat_ptrs = [struct.unpack_from('<H', data, pat_ptrs_off + i * 2)[0]
                for i in range(pat_count)]

    # --- Instruments / Samples ---
    for i in range(ins_count):
        off = ins_ptrs[i] * 16
        if off == 0:
            song.samples.append(ModSample())
            continue

        itype = data[off]
        iname = data[off + 48:off + 76].split(b'\x00')[0].decode('ascii', errors='replace').strip()

        if itype != 1:
            # Not a PCM sample (type 0=empty, 2+=adlib)
            song.samples.append(ModSample(name=iname))
            continue

        # S3M sample header (type 1 = PCM)
        # Byte 13-15: memseg (24-bit little-endian, *16 = file offset)
        memseg_lo = struct.unpack_from('<H', data, off + 14)[0]
        memseg_hi = data[off + 13]
        sample_data_off = ((memseg_hi << 16) | memseg_lo) * 16

        length = struct.unpack_from('<I', data, off + 16)[0]
        loop_start = struct.unpack_from('<I', data, off + 20)[0]
        loop_end = struct.unpack_from('<I', data, off + 24)[0]
        vol = data[off + 28]
        pack = data[off + 30]
        flags_s = data[off + 31]
        c2spd = struct.unpack_from('<I', data, off + 32)[0]

        is_16bit = bool(flags_s & 4)
        is_looping = bool(flags_s & 1)
        is_unsigned = (sample_fmt == 2)

        # Read sample data
        if is_16bit:
            raw_len = length * 2
            raw = data[sample_data_off:sample_data_off + raw_len]
            decoded = []
            for j in range(0, len(raw) - 1, 2):
                val = struct.unpack_from('<H', raw, j)[0]
                if not is_unsigned:
                    val = (val + 32768) & 0xFFFF  # signed -> unsigned 16-bit
                decoded.append((val >> 8) & 0xFF)  # reduce to 8-bit unsigned
            sample_data = decoded
        else:
            raw = data[sample_data_off:sample_data_off + length]
            if is_unsigned:
                sample_data = list(raw)  # already unsigned
            else:
                # signed -> unsigned: +128
                sample_data = [(b + 128) & 0xFF for b in raw]

        loop_len = (loop_end - loop_start) if is_looping and loop_end > loop_start else 0

        ms = ModSample(
            name=iname,
            length=length,
            finetune=0,
            volume=vol,
            loop_start=loop_start if is_looping else 0,
            loop_length=loop_len,
            data=sample_data,
        )
        song.samples.append(ms)

    print(f"  Parsed {len(song.orders)} orders, {ins_count} instruments")

    # --- Patterns ---
    # First, find max pattern index referenced in orders
    max_pat = max(song.orders) if song.orders else 0

    # Parse patterns; emit empty 64-row patterns for unused slots
    all_patterns = []
    for pi in range(max_pat + 1):
        if pi < pat_count and pat_ptrs[pi] != 0:
            off = pat_ptrs[pi] * 16
            rows = _parse_s3m_pattern(data, off, num_channels)
        else:
            # Empty pattern
            rows = [[ModNote() for _ in range(num_channels)] for _ in range(64)]
        all_patterns.append(rows)

    song.patterns = all_patterns

    return song
