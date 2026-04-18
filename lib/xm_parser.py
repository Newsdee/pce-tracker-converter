# xm_parser.py
# FastTracker II .XM parser
# Returns ModSong/ModSample/ModNote objects for the existing converter pipeline.
#
# Reference: XM module format specification (FT2 clone / MilkyTracker docs)
# Supports version 0x0104 (standard XM)

import struct
from pathlib import Path
from typing import List
from mod_parser import ModSong, ModSample, ModNote


def _read_str(data: bytes, offset: int, length: int) -> str:
    """Read a fixed-length ASCII string, stripping nulls and trailing spaces."""
    raw = data[offset:offset + length]
    return raw.decode("ascii", errors="replace").rstrip("\x00").strip()


def _xm_note_to_furnace(xm_note: int):
    """Convert XM note value to (furnace_note, octave).
    XM: 0=empty, 1=C-0, 2=C#0, ..., 96=B-7, 97=note-off."""
    if xm_note == 0:
        return 0, 3          # empty
    if xm_note == 97:
        return 100, 0         # note-off
    n = xm_note - 1
    note = (n % 12) + 1      # 1=C .. 12=B
    octave = n // 12          # 0-7
    return note, octave


def _map_xm_volcol(volcol: int):
    """Map XM volume column byte to effect data.
    Returns (is_volume_set, vol_value, effect, effect_arg).
    is_volume_set=True + vol_value>=0 means inject a 0x0C set-volume.
    is_volume_set=False means use effect/effect_arg."""
    if volcol == 0:
        return False, -1, 0, 0           # empty
    if 0x10 <= volcol <= 0x50:
        return True, volcol - 0x10, 0, 0  # set volume 0-64
    if 0x60 <= volcol <= 0x6F:            # vol slide down
        return False, -1, 0x0A, (volcol & 0x0F)
    if 0x70 <= volcol <= 0x7F:            # vol slide up
        return False, -1, 0x0A, (volcol & 0x0F) << 4
    if 0x80 <= volcol <= 0x8F:            # fine vol slide down
        return False, -1, 0x0E, 0xB0 | (volcol & 0x0F)
    if 0x90 <= volcol <= 0x9F:            # fine vol slide up
        return False, -1, 0x0E, 0xA0 | (volcol & 0x0F)
    if 0xA0 <= volcol <= 0xAF:            # vibrato speed
        return False, -1, 0x04, (volcol & 0x0F) << 4
    if 0xB0 <= volcol <= 0xBF:            # vibrato depth
        return False, -1, 0x04, volcol & 0x0F
    if 0xC0 <= volcol <= 0xCF:            # set panning (ignored for PCE)
        return False, -1, 0, 0
    if 0xD0 <= volcol <= 0xDF:            # pan slide left (ignored)
        return False, -1, 0, 0
    if 0xE0 <= volcol <= 0xEF:            # pan slide right (ignored)
        return False, -1, 0, 0
    if 0xF0 <= volcol <= 0xFF:            # tone portamento
        return False, -1, 0x03, (volcol & 0x0F) << 4
    return False, -1, 0, 0


def _parse_xm_patterns(data, offset, num_patterns, num_channels):
    """Parse all XM pattern blocks.
    Returns (patterns, new_offset).
    patterns: list of [rows], each row = list of ModNote."""
    patterns = []
    for pat_id in range(num_patterns):
        pat_hdr_size = struct.unpack_from('<I', data, offset)[0]
        # packing_type = data[offset + 4]  # always 0
        num_rows = struct.unpack_from('<H', data, offset + 5)[0]
        packed_size = struct.unpack_from('<H', data, offset + 7)[0]
        offset += pat_hdr_size

        rows = []
        if packed_size == 0:
            for _ in range(num_rows):
                rows.append([ModNote() for _ in range(num_channels)])
            patterns.append(rows)
            continue

        end = offset + packed_size
        for _ in range(num_rows):
            row = []
            for _ in range(num_channels):
                xm_note = 0
                xm_ins = 0
                xm_volcol = 0
                xm_fx = 0
                xm_param = 0

                b = data[offset]; offset += 1
                if b & 0x80:
                    if b & 0x01: xm_note  = data[offset]; offset += 1
                    if b & 0x02: xm_ins   = data[offset]; offset += 1
                    if b & 0x04: xm_volcol = data[offset]; offset += 1
                    if b & 0x08: xm_fx    = data[offset]; offset += 1
                    if b & 0x10: xm_param = data[offset]; offset += 1
                else:
                    xm_note = b
                    xm_ins   = data[offset]; offset += 1
                    xm_volcol = data[offset]; offset += 1
                    xm_fx    = data[offset]; offset += 1
                    xm_param = data[offset]; offset += 1

                note, octave = _xm_note_to_furnace(xm_note)
                instrument = xm_ins - 1 if xm_ins > 0 else -1

                # Volume column
                is_vol, vol_val, vcol_fx, vcol_param = _map_xm_volcol(xm_volcol)

                # Effect precedence: effect column wins over volume column
                effect = xm_fx
                effect_arg = xm_param

                if effect == 0 and effect_arg == 0 and vcol_fx != 0:
                    effect = vcol_fx
                    effect_arg = vcol_param

                # Volume column set-volume -> 0x0C if effect column is free,
                # otherwise store as xm_volume for later pickup
                xm_vol = -1
                if is_vol and vol_val >= 0:
                    if effect == 0 and effect_arg == 0:
                        effect = 0x0C
                        effect_arg = vol_val
                    else:
                        xm_vol = vol_val  # effect column busy; carry volume separately

                row.append(ModNote(
                    note=note,
                    octave=octave,
                    instrument=instrument,
                    effect=effect,
                    effect_arg=effect_arg,
                    xm_volume=xm_vol,
                ))
            rows.append(row)

        offset = end  # safety: align to packed_size boundary
        patterns.append(rows)

    return patterns, offset


def _parse_xm_instruments(data, offset, num_instruments):
    """Parse XM instrument + sample blocks.
    Returns (samples_list, new_offset).
    samples_list: one ModSample per instrument (most-used sub-sample).
    Extra attributes attached: xm_vol_env, xm_vol_sustain, etc."""
    samples_list = []

    for ins_id in range(num_instruments):
        ins_hdr_size = struct.unpack_from('<I', data, offset)[0]
        ins_name = _read_str(data, offset + 4, 22)
        # ins_type = data[offset + 26]
        num_samples = struct.unpack_from('<H', data, offset + 27)[0]

        if num_samples == 0:
            ms = ModSample(name=ins_name)
            _attach_empty_xm_env(ms)
            samples_list.append(ms)
            offset += ins_hdr_size
            continue

        smp_hdr_size = struct.unpack_from('<I', data, offset + 29)[0]

        # Note-to-sample mapping (96 bytes)
        sample_map = list(data[offset + 33:offset + 33 + 96])

        # Volume envelope: 12 points x (2B frame + 2B value) = 48 bytes
        vol_env_raw = data[offset + 129:offset + 129 + 48]
        vol_env_points = []
        for i in range(12):
            frame = struct.unpack_from('<H', vol_env_raw, i * 4)[0]
            value = struct.unpack_from('<H', vol_env_raw, i * 4 + 2)[0]
            vol_env_points.append((frame, value))

        vol_num_points = data[offset + 225]
        # pan_num_points = data[offset + 226]
        vol_sustain_pt = data[offset + 227]
        vol_loop_start = data[offset + 228]
        vol_loop_end   = data[offset + 229]
        # pan envelope fields at 230-232
        vol_env_type = data[offset + 233]
        # pan_env_type = data[offset + 234]

        vol_fadeout = struct.unpack_from('<H', data, offset + 239)[0]

        offset += ins_hdr_size

        # Sample headers
        sample_headers = []
        for s in range(num_samples):
            sh = {}
            sh['length']        = struct.unpack_from('<I', data, offset)[0]
            sh['loop_start']    = struct.unpack_from('<I', data, offset + 4)[0]
            sh['loop_length']   = struct.unpack_from('<I', data, offset + 8)[0]
            sh['volume']        = data[offset + 12]
            sh['finetune']      = struct.unpack_from('<b', data, offset + 13)[0]
            sh['type']          = data[offset + 14]
            sh['panning']       = data[offset + 15]
            sh['relative_note'] = struct.unpack_from('<b', data, offset + 16)[0]
            sh['name']          = _read_str(data, offset + 18, 22)
            sample_headers.append(sh)
            offset += smp_hdr_size

        # Sample data (immediately after all headers)
        sample_datas = []
        for sh in sample_headers:
            length = sh['length']
            is_16bit = bool(sh['type'] & 0x10)
            raw = data[offset:offset + length]
            offset += length

            if is_16bit:
                decoded = []
                acc = 0
                for j in range(0, len(raw) - 1, 2):
                    delta = struct.unpack_from('<h', raw, j)[0]
                    acc = (acc + delta) & 0xFFFF
                    signed = acc if acc < 32768 else acc - 65536
                    decoded.append(((signed >> 8) + 128) & 0xFF)
                sample_datas.append(decoded)
            else:
                decoded = []
                acc = 0
                for b in raw:
                    delta = b if b < 128 else b - 256  # signed
                    acc = (acc + delta) & 0xFF
                    # XM 8-bit is signed-centered (0=center); convert to
                    # unsigned-centered (128=center) to match MOD convention
                    decoded.append((acc + 128) & 0xFF)
                sample_datas.append(decoded)

        # Pick most-used sub-sample
        if num_samples == 1:
            best_idx = 0
        else:
            counts = [0] * num_samples
            for s_idx in sample_map:
                if 0 <= s_idx < num_samples:
                    counts[s_idx] += 1
            best_idx = counts.index(max(counts))

        sh = sample_headers[best_idx]
        sd = sample_datas[best_idx]
        is_16bit = bool(sh['type'] & 0x10)

        # Adjust loop offsets for 16-bit (stored as byte offsets in XM)
        loop_start  = sh['loop_start']  // (2 if is_16bit else 1)
        loop_length = sh['loop_length'] // (2 if is_16bit else 1)

        ms = ModSample(
            name=ins_name or sh['name'] or f"Inst_{ins_id}",
            length=len(sd),
            finetune=sh['finetune'],
            volume=sh['volume'],
            loop_start=loop_start,
            loop_length=loop_length,
            data=sd,
        )

        # Attach XM envelope data
        if (vol_env_type & 0x01) and vol_num_points > 0:
            ms.xm_vol_env = vol_env_points[:vol_num_points]
            ms.xm_vol_sustain = vol_sustain_pt if (vol_env_type & 0x02) else -1
            ms.xm_vol_loop_start = vol_loop_start if (vol_env_type & 0x04) else -1
            ms.xm_vol_loop_end   = vol_loop_end   if (vol_env_type & 0x04) else -1
            ms.xm_vol_fadeout = vol_fadeout
        else:
            _attach_empty_xm_env(ms)

        ms.xm_relative_note = sh['relative_note']

        samples_list.append(ms)

    return samples_list, offset


def _attach_empty_xm_env(ms):
    """Attach empty XM envelope fields to a ModSample."""
    ms.xm_vol_env = None
    ms.xm_vol_sustain = -1
    ms.xm_vol_loop_start = -1
    ms.xm_vol_loop_end = -1
    ms.xm_vol_fadeout = 0
    ms.xm_relative_note = 0


def parse_xm(file_path: str) -> ModSong:
    """Parse a FastTracker II .XM file into a ModSong."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(path, "rb") as f:
        data = f.read()

    # Verify magic
    magic = data[0:17].decode("ascii", errors="replace")
    if not magic.startswith("Extended Module: "):
        raise ValueError(f"Not a valid XM file: bad magic '{magic}'")

    song = ModSong()
    song.name = _read_str(data, 17, 20)

    version = struct.unpack_from('<H', data, 58)[0]
    hdr_size = struct.unpack_from('<I', data, 60)[0]

    song.song_length      = struct.unpack_from('<H', data, 64)[0]
    song.restart_position = struct.unpack_from('<H', data, 66)[0]
    song.channels         = struct.unpack_from('<H', data, 68)[0]
    num_patterns          = struct.unpack_from('<H', data, 70)[0]
    num_instruments       = struct.unpack_from('<H', data, 72)[0]
    flags                 = struct.unpack_from('<H', data, 74)[0]
    song.initial_speed    = struct.unpack_from('<H', data, 76)[0]
    song.initial_bpm      = struct.unpack_from('<H', data, 78)[0]

    # Sanity-check speed/BPM
    if song.initial_speed < 1 or song.initial_speed > 31:
        print(f"  WARNING: XM speed={song.initial_speed} suspect, defaulting to 6")
        song.initial_speed = 6
    if song.initial_bpm < 32 or song.initial_bpm > 255:
        print(f"  WARNING: XM BPM={song.initial_bpm} suspect, defaulting to 125")
        song.initial_bpm = 125

    linear_freq = bool(flags & 0x01)

    # Order table at offset 80 (up to 256 entries)
    song.orders = list(data[80:80 + song.song_length])

    print(f"Detected XM v{version >> 8}.{version & 0xFF:02d}: "
          f"'{song.name}', {song.channels}ch, {num_patterns} pat, "
          f"{num_instruments} ins")
    print(f"  Freq table: {'Linear' if linear_freq else 'Amiga'}, "
          f"Speed: {song.initial_speed}, BPM: {song.initial_bpm}")

    # Patterns start at offset 60 + hdr_size
    pat_offset = 60 + hdr_size
    song.patterns, ins_offset = _parse_xm_patterns(
        data, pat_offset, num_patterns, song.channels)

    # Instruments
    song.samples, _ = _parse_xm_instruments(data, ins_offset, num_instruments)

    print(f"  Parsed {len(song.patterns)} patterns, {len(song.samples)} instruments")

    return song
