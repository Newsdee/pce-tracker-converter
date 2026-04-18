# convert_mod.py
# MOD/XM -> Furnace .fur Converter for PC Engine (6 channels)
# Usage: python convert_mod.py input.mod [output.fur]
#        python convert_mod.py input.xm  [output.fur] [--drop_channels=5,6] [--noise_channel=4]

import sys
import math
from pathlib import Path

# Add lib to path
sys.path.insert(0, str(Path(__file__).parent / "lib"))

from mod_parser import parse_mod, ModSong, ModNote
from xm_parser import parse_xm
from sample_processor import process_samples_for_pce, export_samples_zip
from fur_writer import FurWriter

# --- Effect persistence categories ---
# MOD effects persist until changed; Furnace effects are one-shot.
# We track 5 categories and re-emit when state changes, matching
# Furnace's MOD import logic (mod.cpp fxUsage / fxUsageTyp).
FX_CAT_ARP    = 0
FX_CAT_SLIDE  = 1
FX_CAT_VIB    = 2
FX_CAT_TREM   = 3
FX_CAT_VSLIDE = 4
NUM_FX_CATS   = 5


def mod_vol_to_pce(mod_vol: int) -> int:
    """Convert MOD linear volume (0-64) to PCE log volume (0-31, 1.5 dB/step)."""
    if mod_vol <= 0:
        return 0
    gain = mod_vol / 64.0
    db = 20.0 * math.log10(gain)
    return max(0, min(31, round(31 + db / 1.5)))


# --- Canonical waveforms (32 samples, 5-bit 0-31) ---

def _make_canonical_waveforms():
    """Generate canonical PCE waveforms for matching and substitution."""
    N = 32
    triangle = [int(round(31 * (1 - abs(2 * i / (N - 1) - 1)))) for i in range(N)]
    sine = [int(round(15.5 + 15.5 * math.sin(2 * math.pi * i / N))) for i in range(N)]
    square = [31] * 16 + [0] * 16
    return {"triangle": triangle, "sine": sine, "square": square}

CANONICAL_WAVEFORMS = _make_canonical_waveforms()


def _correlation(a, b):
    """Pearson correlation between two lists."""
    n = len(a)
    if n == 0:
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    num = sum(x * y for x, y in zip(da, db))
    den_a = sum(x * x for x in da) ** 0.5
    den_b = sum(x * x for x in db) ** 0.5
    if den_a < 1e-9 or den_b < 1e-9:
        return 0.0
    return num / (den_a * den_b)


def _classify_waveform(wt):
    """Check if waveform matches a canonical shape. Returns name or None."""
    if len(set(wt)) <= 1:
        return "flat"
    # Also check phase-inverted variants
    inv = [31 - v for v in wt]
    for name, canon in CANONICAL_WAVEFORMS.items():
        if abs(_correlation(wt, canon)) > 0.92:
            return name
        if abs(_correlation(inv, canon)) > 0.92:
            return name
    return None


def _optimize_wavetables(sample_data, used_instruments):
    """Compact wavetables: drop unused, deduplicate, substitute canonical shapes.

    Returns (wt_list, ins_wt_map) where:
      wt_list: list of unique wavetable data arrays
      ins_wt_map: dict mapping original instrument index -> wavetable index in wt_list
    """
    # Collect wavetables only for used instruments
    wt_registry = []  # list of (data_tuple, canonical_name_or_None)
    ins_wt_map = {}

    for orig_idx in sorted(used_instruments):
        wt = sample_data[orig_idx]["wavetable"]
        canon = _classify_waveform(wt)

        # Substitute canonical waveform if matched
        if canon and canon != "flat" and canon in CANONICAL_WAVEFORMS:
            wt = CANONICAL_WAVEFORMS[canon]

        # Flat -> single shared DC wavetable
        if canon == "flat":
            wt = [16] * 32

        wt_key = tuple(wt)

        # Deduplicate: find existing identical wavetable
        found = None
        for i, (existing_key, _) in enumerate(wt_registry):
            if existing_key == wt_key:
                found = i
                break

        if found is not None:
            ins_wt_map[orig_idx] = found
        else:
            ins_wt_map[orig_idx] = len(wt_registry)
            wt_registry.append((wt_key, canon))

    wt_list = [list(data) for data, _ in wt_registry]
    wt_names = [name for _, name in wt_registry]
    return wt_list, ins_wt_map, wt_names


def _mod_fx_categories(effect, param):
    """Map MOD effect+param to persistence categories.
    Returns list of (cat, value) tuples."""
    cats = []
    if effect == 0x00 and param != 0:
        cats.append((FX_CAT_ARP, param))
    elif effect == 0x01:
        cats.append((FX_CAT_SLIDE, param))
    elif effect == 0x02:
        cats.append((FX_CAT_SLIDE, param))
    elif effect == 0x03:
        cats.append((FX_CAT_SLIDE, param))
    elif effect == 0x04:
        cats.append((FX_CAT_VIB, param))
    elif effect == 0x05:   # porta + volslide
        cats.append((FX_CAT_SLIDE, 0))     # continue porta
        cats.append((FX_CAT_VSLIDE, param))
    elif effect == 0x06:   # vibrato + volslide
        cats.append((FX_CAT_VIB, 0))       # continue vibrato
        cats.append((FX_CAT_VSLIDE, param))
    elif effect == 0x07:
        cats.append((FX_CAT_TREM, param))
    elif effect == 0x0A:
        cats.append((FX_CAT_VSLIDE, param))
    return cats


def _scan_fx_usage(song, ch):
    """Scan all patterns reachable by channel to build the 5-bool fxUsage bitmap."""
    fx_usage = [False] * NUM_FX_CATS
    seen_pats = set()
    for order_idx in range(song.song_length):
        pat_id = song.orders[order_idx]
        if pat_id in seen_pats or pat_id >= len(song.patterns):
            continue
        seen_pats.add(pat_id)
        for row_notes in song.patterns[pat_id]:
            if ch < len(row_notes):
                mn = row_notes[ch]
                for cat, _ in _mod_fx_categories(mn.effect, mn.effect_arg):
                    fx_usage[cat] = True
    return fx_usage


def _convert_oneshot_effects(effect, param):
    """Convert non-persistent MOD effects to Furnace effect words.
    Returns list of (cmd | val<<8) ints. Persistent effects return []."""
    if effect == 0x09:     # Sample offset
        return [0x91 | (param << 8)]
    elif effect == 0x0B:   # Position jump
        return [0x0B | (param << 8)]
    elif effect == 0x0C:   # Set volume -> volume column (handled separately)
        return []
    elif effect == 0x0D:   # Pattern break, BCD->decimal
        row = (param >> 4) * 10 + (param & 0x0F)
        return [0x0D | (row << 8)]
    elif effect == 0x0F:   # Speed / Tempo
        if param <= 0x20:
            return [0x0F | (param << 8)]
        else:
            return [0xF0 | (param << 8)]
    elif effect == 0x0E:   # Extended
        ext = (param >> 4) & 0x0F
        val = param & 0x0F
        ext_map = {
            0x00: (0x10, 0 if val else 1),   # filter (inverted)
            0x01: (0xF1, val),               # fine porta up
            0x02: (0xF2, val),               # fine porta down
            0x09: (0x0C, val),               # retrigger
            0x0A: (0xF8, val),               # fine vol up
            0x0B: (0xF9, val),               # fine vol down
            0x0C: (0xEC, val),               # note cut
            0x0D: (0xED, val),               # note delay
        }
        if ext in ext_map:
            cmd, v = ext_map[ext]
            return [cmd | (v << 8)]
    return []


def _apply_persistence(raw_rows, fx_usage, ins_default_vol=None):
    """Process a channel's pattern rows with MOD-style persistent effect tracking.

    raw_rows: list of dicts with note/octave/instrument/mod_effect/mod_param
    fx_usage: 5-bool list of which categories this channel ever uses
    ins_default_vol: dict mapping remapped instrument index -> PCE default volume

    Returns list of Furnace-format row dicts with 'effects' list.
    """
    if ins_default_vol is None:
        ins_default_vol = {}

    last_state = [-1] * NUM_FX_CATS    # -1 = never set (triggers first emission)
    set_state  = [-1] * NUM_FX_CATS    # memory: last nonzero param
    last_slide_type = 0x01             # default: slide up
    # Furnace channel volume tracker: tracks what Furnace's channel vol
    # currently is so we can detect when it diverges from MOD behavior.
    # -1 = unknown (forces restore on next note trigger).
    fur_chan_vol = -1
    last_ins = -1           # last instrument seen on this channel

    result = []
    for row in raw_rows:
        effect = row["mod_effect"]
        param  = row["mod_param"]
        has_note = row["note"] != 0

        # Determine effective state for each category this row.
        # Matching Furnace mod.cpp: effectState is set to the RESOLVED value
        # (after memory recall for param=0), not the raw param.
        # If param=0 and memory was never set, the category is SKIPPED.
        cur_state = [0] * NUM_FX_CATS
        cur_slide_type = last_slide_type

        cats = _mod_fx_categories(effect, param)
        for cat, val in cats:
            if cat == FX_CAT_SLIDE:
                if effect in (0x01, 0x02, 0x03):
                    cur_slide_type = effect
                elif effect == 0x05:
                    cur_slide_type = 0x03  # porta
                # Memory recall for param=0 (slide/vib/trem only)
                if val == 0:
                    if set_state[cat] < 0:
                        continue   # never seen -> skip (effectState stays 0)
                    val = set_state[cat]
            elif cat in (FX_CAT_VIB, FX_CAT_TREM):
                if val == 0:
                    if set_state[cat] < 0:
                        continue
                    val = set_state[cat]
            # ARP and VSLIDE: no memory recall, use raw value
            cur_state[cat] = val

        # Update memory for nonzero params
        for cat in range(NUM_FX_CATS):
            if cur_state[cat] != 0:
                set_state[cat] = cur_state[cat]

        # Build Furnace effects list
        effects = []

        # One-shot effects (non-persistent) go first
        effects.extend(_convert_oneshot_effects(effect, param))

        # Persistent effects: re-emit when state changed
        for cat in range(NUM_FX_CATS):
            if not fx_usage[cat]:
                continue
            if cur_state[cat] == last_state[cat]:
                # Special case: pitch slide with new note should re-emit
                if cat == FX_CAT_SLIDE and cur_state[cat] != 0 and has_note:
                    pass  # fall through to emit
                else:
                    continue

            # Emit the Furnace effect for this category
            val = cur_state[cat]
            if cat == FX_CAT_ARP:
                effects.append(0x00 | (val << 8))
            elif cat == FX_CAT_SLIDE:
                effects.append(cur_slide_type | (val << 8))
            elif cat == FX_CAT_VIB:
                effects.append(0x04 | (val << 8))
            elif cat == FX_CAT_TREM:
                effects.append(0x07 | (val << 8))
            elif cat == FX_CAT_VSLIDE:
                effects.append(0xFA | (val << 8))

        last_state = cur_state[:]
        if effect in (0x01, 0x02, 0x03, 0x05):
            last_slide_type = cur_slide_type

        # Volume column -- MOD vs Furnace semantics reconciliation.
        # MOD: note+instrument trigger always resets volume to sample default.
        #      Cxx on same row overrides that default.
        # Furnace: channel volume persists until explicitly set in volume column.
        # Strategy: track fur_chan_vol (what Furnace thinks the volume is).
        # When a note triggers and MOD's volume would differ, inject a restore.
        vol = -1
        xm_vol = row.get("xm_volume", -1)
        if effect == 0x0C:
            # Explicit volume set -- applies in both MOD and Furnace
            pce_vol = mod_vol_to_pce(param)
            vol = pce_vol
            fur_chan_vol = pce_vol
        elif xm_vol >= 0:
            # XM volume column was set alongside another effect (e.g. pitch slide)
            pce_vol = mod_vol_to_pce(xm_vol)
            vol = pce_vol
            fur_chan_vol = pce_vol
        elif has_note:
            # MOD resets volume to instrument default on note trigger.
            # Determine which instrument is active.
            effective_ins = row["instrument"] if row["instrument"] >= 0 else last_ins
            if effective_ins >= 0:
                default_vol = ins_default_vol.get(effective_ins, -1)
                if default_vol >= 0 and default_vol != fur_chan_vol:
                    vol = default_vol
                    fur_chan_vol = default_vol

        # Track last instrument for implicit reuse
        if row["instrument"] >= 0:
            last_ins = row["instrument"]

        # Vol slide and fine vol effects make channel vol unpredictable --
        # mark as unknown so the next note trigger injects a restore.
        if cur_state[FX_CAT_VSLIDE] != 0:
            fur_chan_vol = -1
        if effect == 0x0E and (param >> 4) in (0x0A, 0x0B):
            fur_chan_vol = -1

        result.append({
            "note": row["note"],
            "octave": row["octave"],
            "instrument": row["instrument"],
            "volume": vol,
            "effects": effects,
        })

    return result


def _scan_max_note_durations(song) -> dict:
    """Scan patterns to find maximum note duration per instrument (in rows).

    Returns dict: instrument_index -> max_rows.
    A note's duration = rows until the next note/instrument on the same channel.
    """
    max_dur = {}  # instrument -> max rows

    for pat in song.patterns:
        num_ch = min(len(pat[0]) if pat else 0, song.channels)
        for ch in range(num_ch):
            col = [row[ch] for row in pat]
            # Walk through rows, tracking current instrument
            i = 0
            while i < len(col):
                mn = col[i]
                if mn.instrument >= 0 and mn.note > 0:
                    ins = mn.instrument
                    dur = 1
                    for j in range(i + 1, len(col)):
                        if col[j].note > 0 or col[j].instrument >= 0:
                            break
                        dur += 1
                    max_dur[ins] = max(max_dur.get(ins, 0), dur)
                i += 1

    return max_dur


def _parse_args():
    """Parse CLI arguments.
    Returns (positional, forced_noise, drop_channels, noise_channels).
    """
    positional = []
    forced_noise = {}       # sample_index -> tick_count (0 = permanent)
    drop_channels = []      # 1-based channel indices to remove
    noise_channels = []     # 1-based channel indices to swap to PCE noise slots
    for arg in sys.argv[1:]:
        if arg.startswith("--noise_insts="):
            for spec in arg[len("--noise_insts="):].split(","):
                spec = spec.strip()
                if ":" in spec:
                    idx_str, ticks_str = spec.split(":", 1)
                    if idx_str.isdigit() and ticks_str.isdigit():
                        forced_noise[int(idx_str)] = int(ticks_str)
                elif spec.isdigit():
                    forced_noise[int(spec)] = 0  # permanent
        elif arg.startswith("--drop_channels="):
            for ch_str in arg[len("--drop_channels="):].split(","):
                ch_str = ch_str.strip()
                if ch_str.isdigit():
                    drop_channels.append(int(ch_str))
        elif arg.startswith("--noise_channel="):
            for ch_str in arg[len("--noise_channel="):].split(","):
                ch_str = ch_str.strip()
                if ch_str.isdigit():
                    noise_channels.append(int(ch_str))
            noise_channels = noise_channels[:2]  # max 2
        else:
            positional.append(arg)
    return positional, forced_noise, drop_channels, noise_channels


def _drop_channels(song, drop_list):
    """Remove 1-based channel indices from all patterns, update channel count."""
    drop_set = set(d - 1 for d in drop_list)  # convert to 0-based
    for pat in song.patterns:
        for row in pat:
            for idx in sorted(drop_set, reverse=True):
                if idx < len(row):
                    del row[idx]
    new_ch = song.channels - len(drop_set & set(range(song.channels)))
    print(f"  Dropped channels {drop_list} -> {new_ch} channels remain")
    song.channels = new_ch


def _swap_noise_channels(song, noise_list):
    """Swap 1-based channel indices into PCE noise positions (ch5, ch6).
    noise_list[0] swaps with ch5 (0-based 4), noise_list[1] with ch6 (0-based 5)."""
    targets = [4, 5]  # 0-based PCE noise channel slots
    for i, src_1based in enumerate(noise_list):
        src = src_1based - 1  # 0-based
        tgt = targets[i]
        if src == tgt:
            continue
        if src >= song.channels or tgt >= song.channels:
            print(f"  WARNING: Cannot swap ch{src_1based} <-> ch{tgt+1}: "
                  f"only {song.channels} channels")
            continue
        print(f"  Swapping ch{src_1based} <-> ch{tgt+1} (PCE noise slot)")
        for pat in song.patterns:
            for row in pat:
                if src < len(row) and tgt < len(row):
                    row[src], row[tgt] = row[tgt], row[src]


def main():
    positional, forced_noise, drop_channels_list, noise_channels = _parse_args()

    if len(positional) < 1:
        print("Usage: python convert_mod.py <input> [output.fur] [options]")
        print("  --noise_insts=N,M     Force sample indices (0-based) to noise channel")
        print("                        N:T = burst noise for T ticks (e.g. 7:2)")
        print("  --drop_channels=N,M   Remove channels (1-based) before conversion")
        print("  --noise_channel=N[,M] Swap channel N to PCE noise slot (ch5/ch6)")
        sys.exit(1)

    input_path = Path(positional[0])
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    output_path = Path(positional[1]) if len(positional) > 1 else input_path.with_suffix(".fur")

    # Auto-detect format by extension
    ext = input_path.suffix.lower()
    fmt_name = "XM" if ext == ".xm" else "MOD"

    print(f"Converting {fmt_name} -> Furnace .fur")
    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    print("-" * 60)

    # 1. Parse input
    if ext == ".xm":
        song: ModSong = parse_xm(str(input_path))
    else:
        song: ModSong = parse_mod(str(input_path))

    # 1b. Drop channels (before noise swap and limit)
    if drop_channels_list:
        _drop_channels(song, drop_channels_list)

    # 1c. Swap noise channels into PCE noise slots
    if noise_channels:
        _swap_noise_channels(song, noise_channels)

    # 2. Limit to 6 channels
    if song.channels > 6:
        print(f"WARNING: {fmt_name} has {song.channels} channels -> limiting to 6 for PC Engine.")
        song.limit_to_6_channels()

    # 3. Export samples as WAV zip
    zip_path = input_path.with_name(input_path.stem + "_samples.zip")
    print("Exporting samples...")
    export_samples_zip(song.samples, str(zip_path))

    # 4. Process samples -> wavetables + macros
    # First, scan max note duration per instrument from pattern data
    max_note_rows = _scan_max_note_durations(song)

    print("Processing samples...")
    pce_volumes = [mod_vol_to_pce(s.volume) for s in song.samples]
    sample_data = process_samples_for_pce(song.samples, pce_volumes,
                                          max_note_rows=max_note_rows,
                                          speed=song.initial_speed)

    # Track which instruments are noise-type
    noise_instruments = set()
    for i, sd in enumerate(sample_data):
        if sd["noise_env"] is not None:
            noise_instruments.add(i)

    # Apply --noise overrides: force specified samples to noise classification
    for i, ticks in forced_noise.items():
        if i < len(sample_data):
            old_cls = sample_data[i]["classification"]
            sample_data[i]["classification"] = "noise"
            if ticks > 0:
                # Burst noise: N ticks on, then off (no loop)
                sample_data[i]["noise_env"] = [1] * ticks + [0]
                sample_data[i]["noise_loop"] = None
            else:
                # Permanent noise
                sample_data[i]["noise_env"] = [1]
                sample_data[i]["noise_loop"] = 0
            noise_instruments.add(i)
            sname = song.samples[i].name if i < len(song.samples) else f"Sample_{i}"
            tag = f" ({ticks}-tick burst)" if ticks > 0 else " (permanent)"
            print(f"  --noise override: Sample {i} ({sname}) {old_cls} -> noise{tag}")

    # 4b. Scan which instruments are actually used in patterns
    used_instruments = set()
    num_samples = len(song.samples)
    for pat in song.patterns:
        for row_notes in pat:
            for mn in row_notes:
                if 0 <= mn.instrument < num_samples:
                    used_instruments.add(mn.instrument)

    # 4c. Optimize wavetables: compact, dedup, canonical substitution
    wt_list, ins_wt_map, wt_names = _optimize_wavetables(sample_data, used_instruments)

    # Build compact instrument mapping: original index -> new index
    used_sorted = sorted(used_instruments)
    ins_remap = {orig: new for new, orig in enumerate(used_sorted)}

    print(f"  Instruments: {len(song.samples)} total, {len(used_sorted)} used")
    print(f"  Wavetables:  {len(song.samples)} total -> {len(wt_list)} unique")
    for i, (wt, name) in enumerate(zip(wt_list, wt_names)):
        tag = f" ({name})" if name else ""
        print(f"    WT {i}: range [{min(wt):2d},{max(wt):2d}]{tag}")
    # Report octave corrections
    for orig_idx in used_sorted:
        sd = sample_data[orig_idx]
        if sd.get("octave_shift", 0) != 0 and sd.get("confidence", 0) > 0.4:
            sname = song.samples[orig_idx].name if orig_idx < len(song.samples) else f"Sample_{orig_idx}"
            print(f"    Ins {ins_remap[orig_idx]} ({sname}): "
                  f"{sd['cycles_detected']:.1f} cycles detected (conf={sd['confidence']:.2f}), "
                  f"single-cycle extracted")

    # 5. Build Furnace .fur
    print("Building Furnace .fur...")
    writer = FurWriter()

    writer.set_song_info(
        name=song.name or input_path.stem,
        author="MOD to Furnace Converter"
    )
    writer.set_orders(song.orders[:song.song_length])

    # Variable rows per pattern: use the longest pattern in the file
    rows_per_pattern = 64
    if song.patterns:
        rows_per_pattern = max(len(pat) for pat in song.patterns)
    writer.set_rows_per_pattern(rows_per_pattern)
    writer.set_tempo(song.initial_speed, song.initial_bpm)

    # Add only used instruments with compacted indices
    for new_idx, orig_idx in enumerate(used_sorted):
        sd = sample_data[orig_idx]
        name = song.samples[orig_idx].name if orig_idx < len(song.samples) else f"Sample_{orig_idx}"
        wt_idx = ins_wt_map[orig_idx]
        writer.add_instrument(
            idx=new_idx,
            name=name or f"Sample_{orig_idx}",
            volume_env=sd["volume_env"],
            volume_loop=sd["volume_loop"],
            volume_release=sd["volume_release"],
            wavetable_index=wt_idx,
            noise_env=sd["noise_env"],
            noise_loop=sd["noise_loop"] if sd["noise_loop"] is not None else 255,
        )

    # Add compacted wavetables
    for wt in wt_list:
        writer.add_wavetable(wt)

    # Build per-instrument octave shift lookup (original instrument index -> shift)
    # Single-cycle extraction compresses N cycles into 1, so notes must be
    # transposed down by log2(N) octaves to compensate.
    octave_shift_map = {}
    for orig_idx in used_sorted:
        sd = sample_data[orig_idx]
        shift = sd.get("octave_shift", 0)
        if shift != 0 and sd.get("confidence", 0) > 0.4:
            octave_shift_map[orig_idx] = shift

    # 6. Build patterns with persistent effect tracking
    noise_warnings = []
    noise_migrated = 0

    # Phase 1: Build raw pattern data (MOD-level effects, not yet converted)
    # Remap instrument indices to compacted numbering + apply octave correction
    raw_patterns = {}
    for ch in range(6):
        raw_patterns[ch] = {}
        last_ins = -1   # track implicit instrument for octave correction
        for pat_id, mod_pattern in enumerate(song.patterns):
            rows = []
            for row_notes in mod_pattern:
                if ch >= len(row_notes):
                    mod_note = ModNote()
                else:
                    mod_note = row_notes[ch]
                ins = mod_note.instrument
                note = mod_note.note
                octave = mod_note.octave

                # Track last explicit instrument (MOD reuses it implicitly)
                if ins >= 0:
                    last_ins = ins
                # Apply octave correction for single-cycle extracted samples
                effective_ins = ins if ins >= 0 else last_ins
                if note != 0 and effective_ins >= 0 and effective_ins in octave_shift_map:
                    octave += octave_shift_map[ins]
                    # Clamp to valid Furnace range (0-7)
                    if octave < 0:
                        octave = 0
                    elif octave > 7:
                        octave = 7

                if ins >= 0:
                    if ins in ins_remap:
                        ins = ins_remap[ins]
                    else:
                        ins = -1  # strip non-existing instrument
                rows.append({
                    "note": note,
                    "octave": octave,
                    "instrument": ins,
                    "mod_effect": mod_note.effect,
                    "mod_param": mod_note.effect_arg,
                    "xm_volume": getattr(mod_note, 'xm_volume', -1),
                })
            raw_patterns[ch][pat_id] = rows

    # Phase 2: Apply persistence per channel, track max effect columns
    # Build remapped instrument -> default PCE volume lookup
    ins_default_vol = {}
    for orig_idx in used_sorted:
        new_idx = ins_remap[orig_idx]
        ins_default_vol[new_idx] = pce_volumes[orig_idx]

    all_patterns = {}
    max_fx_cols = [1] * 6
    for ch in range(6):
        all_patterns[ch] = {}
        fx_usage = _scan_fx_usage(song, ch)
        for pat_id in raw_patterns[ch]:
            converted = _apply_persistence(raw_patterns[ch][pat_id], fx_usage,
                                           ins_default_vol)
            all_patterns[ch][pat_id] = converted
            for row in converted:
                if len(row["effects"]) > max_fx_cols[ch]:
                    max_fx_cols[ch] = len(row["effects"])
        max_fx_cols[ch] = min(max_fx_cols[ch], 8)

    # Phase 3: Noise migration pass (uses remapped instrument indices)
    # Two sub-passes:
    #   3a. Migrate noise notes from ch1-4 to free ch5/6 slots
    #   3b. Inject 0x11 01 (noise on) / 0x11 00 (noise off) on ch5/6
    noise_instruments_remapped = set()
    for orig in noise_instruments:
        if orig in ins_remap:
            noise_instruments_remapped.add(ins_remap[orig])

    # 3a. Migrate noise from ch1-4 -> ch5/6
    for ch in range(4):
        for pat_id in range(len(song.patterns)):
            rows = all_patterns[ch][pat_id]
            for row_idx, row in enumerate(rows):
                ins = row["instrument"]
                if ins < 0 or ins not in noise_instruments_remapped:
                    continue
                if row["note"] == 0:
                    continue
                target_ch = _find_free_noise_channel(all_patterns, pat_id,
                                                     row_idx, [4, 5])
                if target_ch is not None:
                    migrated_row = dict(row)
                    fx = list(migrated_row.get("effects", []))
                    migrated_row["effects"] = fx
                    all_patterns[target_ch][pat_id][row_idx] = migrated_row
                    rows[row_idx] = {
                        "note": 0, "octave": 0, "instrument": -1,
                        "volume": -1, "effects": [],
                    }
                    noise_migrated += 1
                else:
                    noise_warnings.append(
                        f"  WARNING: Noise ins {ins} on ch {ch+1}, "
                        f"pat {pat_id} row {row_idx} - ch 5/6 occupied")

    # 3b. Inject 0x11 on ch5/6: noise_on when a noise instrument plays,
    #     noise_off when a non-noise instrument plays after noise.
    #     Reset tracking per pattern (patterns reused across order positions).
    for ch in (4, 5):
        for pat_id in range(len(song.patterns)):
            if pat_id not in all_patterns[ch]:
                continue
            rows = all_patterns[ch][pat_id]
            noise_active = False
            for row_idx, row in enumerate(rows):
                ins = row["instrument"]
                has_note = row["note"] != 0

                if ins >= 0 and ins in noise_instruments_remapped and has_note:
                    if not noise_active:
                        fx = list(row.get("effects", []))
                        fx.append(0x11 | (0x01 << 8))  # noise on
                        row["effects"] = fx
                        noise_active = True
                        if len(fx) > max_fx_cols[ch]:
                            max_fx_cols[ch] = min(len(fx), 8)
                elif ins >= 0 and ins not in noise_instruments_remapped and has_note:
                    if noise_active:
                        fx = list(row.get("effects", []))
                        fx.append(0x11 | (0x00 << 8))  # noise off
                        row["effects"] = fx
                        noise_active = False
                        if len(fx) > max_fx_cols[ch]:
                            max_fx_cols[ch] = min(len(fx), 8)

    # Phase 4: Arpeggio -> instrument macro conversion
    # PCE doesn't support arpeggio as a tracker effect -- we convert each unique
    # (base_instrument, arp_param) into a cloned instrument with an arp macro.
    arp_clone_map = {}   # (base_ins, arp_param) -> new_instrument_index
    next_ins_id = len(used_sorted)  # next available instrument index

    for ch in range(6):
        for pat_id in sorted(all_patterns[ch].keys()):
            rows = all_patterns[ch][pat_id]
            effective_ins = -1
            for row_idx, row in enumerate(rows):
                # Track effective instrument (last explicitly set)
                if row["instrument"] >= 0:
                    effective_ins = row["instrument"]

                arp_found = None
                arp_stop = False
                for fx_word in row["effects"]:
                    fx_cmd = fx_word & 0xFF
                    fx_val = (fx_word >> 8) & 0xFF
                    if fx_cmd == 0x00 and fx_val != 0:
                        arp_found = fx_val
                    elif fx_cmd == 0x00 and fx_val == 0:
                        arp_stop = True

                if arp_found is not None and effective_ins >= 0:
                    key = (effective_ins, arp_found)
                    if key not in arp_clone_map:
                        arp_clone_map[key] = next_ins_id
                        next_ins_id += 1
                    clone_id = arp_clone_map[key]
                    row["instrument"] = clone_id
                elif arp_stop and effective_ins >= 0:
                    # Restore base instrument when arpeggio stops
                    base_ins = effective_ins
                    for (bi, ap), ci in arp_clone_map.items():
                        if ci == effective_ins:
                            base_ins = bi
                            break
                    row["instrument"] = base_ins
                    effective_ins = base_ins

    # Create arpeggio clone instruments
    arp_clones_created = 0
    for (base_ins, arp_param), clone_id in sorted(arp_clone_map.items(),
                                                   key=lambda x: x[1]):
        x = (arp_param >> 4) & 0x0F  # high nibble = semitones up 1
        y = arp_param & 0x0F          # low nibble = semitones up 2
        arp_macro = [0, x, y]
        # Find the base instrument's data to clone
        base_orig_idx = used_sorted[base_ins] if base_ins < len(used_sorted) else 0
        sd = sample_data[base_orig_idx]
        base_name = song.samples[base_orig_idx].name if base_orig_idx < len(song.samples) else f"Sample_{base_orig_idx}"
        wt_idx = ins_wt_map[base_orig_idx]
        writer.add_instrument(
            idx=clone_id,
            name=f"{base_name} arp{arp_param:02X}",
            volume_env=sd["volume_env"],
            volume_loop=sd["volume_loop"],
            volume_release=sd["volume_release"],
            wavetable_index=wt_idx,
            noise_env=sd["noise_env"],
            noise_loop=sd["noise_loop"] if sd["noise_loop"] is not None else 255,
            arp_env=arp_macro,
            arp_loop=0,
        )
        arp_clones_created += 1

    if arp_clones_created:
        print(f"  Arpeggio: {len(arp_clone_map)} patterns -> "
              f"{arp_clones_created} clone instruments")
        for (base_ins, arp_param), clone_id in sorted(arp_clone_map.items(),
                                                       key=lambda x: x[1]):
            x = (arp_param >> 4) & 0x0F
            y = arp_param & 0x0F
            print(f"    Ins {clone_id}: base={base_ins} arp=[0,{x},{y}] "
                  f"(0x{arp_param:02X})")
        # Recalculate max effect columns after arp removal
        max_fx_cols = [1] * 6
        for ch in range(6):
            for pat_id in all_patterns[ch]:
                for row in all_patterns[ch][pat_id]:
                    if len(row["effects"]) > max_fx_cols[ch]:
                        max_fx_cols[ch] = len(row["effects"])
            max_fx_cols[ch] = min(max_fx_cols[ch], 8)

    # Set effect columns after noise migration (17xx may have added columns)
    writer.set_effect_cols(max_fx_cols)
    print(f"  Effect columns per ch: {max_fx_cols[:6]}")

    # Add all patterns to writer
    for ch in range(6):
        for pat_id in sorted(all_patterns[ch].keys()):
            writer.add_pattern(ch, pat_id, all_patterns[ch][pat_id])

    # Save
    writer.save(str(output_path))

    # Report
    print(f"\nConversion completed successfully!")
    print(f"   Song name   : {song.name}")
    print(f"   Channels    : 6 (PC Engine)")
    print(f"   Patterns    : {len(song.patterns)}")
    print(f"   Instruments : {len(used_sorted)} (of {len(song.samples)} samples)"
          + (f" + {arp_clones_created} arp clones" if arp_clones_created else ""))
    print(f"   Wavetables  : {len(wt_list)} unique")
    classifications = {}
    for sd in sample_data:
        c = sd["classification"]
        classifications[c] = classifications.get(c, 0) + 1
    for cls, count in sorted(classifications.items()):
        print(f"   {cls:12s} : {count}")
    if noise_migrated:
        print(f"   Noise notes migrated to ch 5/6: {noise_migrated}")
    for w in noise_warnings:
        print(w)


def _find_free_noise_channel(all_patterns, pat_id, row_idx, candidates):
    """Find a noise-capable channel (4 or 5) that is free at row_idx
    and for 2 rows after (to avoid cutting off the note)."""
    for ch in candidates:
        if pat_id not in all_patterns[ch]:
            continue
        rows = all_patterns[ch][pat_id]
        free = True
        for offset in range(3):
            r = row_idx + offset
            if r >= len(rows):
                break
            if rows[r]["note"] != 0:
                free = False
                break
        if free:
            return ch
    return None


if __name__ == "__main__":
    main()