# Debugging Guide for AI Agents

Tips for diagnosing issues in the MOD → Furnace converter output.

## Tool Quick Reference

| What to check | Tool / command | Notes |
|---------------|----------------|-------|
| .fur file structure valid | `python tools/verify_fur.py output.fur` | Checks block pointers, magic bytes, counts |
| Pattern data (notes, effects) | `python tools/dump_pattern.py output.fur [pat] [rows]` | Shows all 6 channels. Default: pattern 0, 18 rows |
| Wavetable shapes | `python tools/dump_wavetables.py input.mod` | Shows extracted wavetable values, ZC count, period info |
| Period detection per sample | `python tools/analyze_octaves.py input.mod` | AC/FFT period, cycle count, confidence, octave shift |
| Full conversion with diagnostics | `python convert_mod.py input.mod --noise_insts=5` | Watch for `--noise override` and `Noise notes migrated` lines |

## Verifying Noise Migration

After running with `--noise_insts=N`:

1. Check converter output for `--noise override: Sample N ... -> noise`
2. Check `Noise notes migrated to ch 5/6: <count>` — should be > 0
3. Dump the target pattern: `python tools/dump_pattern.py output.fur 0 64`
4. Look at Ch5/Ch6 columns for notes with `1101` effect (noise enable)
5. Verify the source channel rows are blanked (no duplicate notes)

If `Noise notes migrated` is 0 but notes exist:
- Check the sample index is correct (0-based, matches converter `Sample N:` output)
- Verify the instrument is actually used in patterns (`used_instruments` set)
- The instrument may only have implicit triggers (note without instrument number) — these won't be caught by the `ins < 0` check

## Verifying Wavetable Quality

Run `dump_wavetables.py` and check:
- **ZC (zero crossings)**: Should be 1-3 for tonal instruments. High ZC (>10) means the extraction captured noise or multiple cycles
- **Canonical match**: Triangle, sine, or square matches show as `(triangle)` etc. in converter output
- **Octave shift**: `oct -N` means N octaves of correction. Confidence < 0.4 means the shift is unreliable

## Verifying Effect Persistence

Dump a pattern and check:
- Persistent effects (arp, slide, vibrato, tremolo, volslide) should re-emit on state changes
- A `0400` should appear when vibrato stops (not just disappear)
- Volume column should show restore values when a new note triggers and the channel volume differs from the instrument default

## Verifying Volume Envelopes

In the converter output, look for `[percussive]` vs `[tonal]` classification:
- Percussive unlooped with short max duration → should get a log-decay envelope
- Tonal looped → attack ramp + sustain at loop RMS level
- Empty samples → flat zero envelope

To inspect the actual envelope data, add a debug print in `sample_processor.py`:
```python
print(f"  vol_env: {vol_data['volume_env']}")
```

## Common Issues

### "No notes in ch5/6 after noise migration"
- The dump tool previously capped at 4 channels — now fixed to show all 6
- Verify with `dump_pattern.py` using the `rows` argument set to 64

### "Effect column count looks wrong"
- `set_effect_cols` is called *after* noise migration, so 11xx effects on ch4/5 are counted
- Check converter output: `Effect columns per ch: [...]`

### "Instrument index mismatch"
- MOD instruments are 1-based internally, but the converter maps to 0-based
- The `ins_remap` dict compacts indices: only used instruments get sequential IDs
- Sample 5 in MOD may become instrument 4 in Furnace after compaction

### "Octave sounds wrong"
- Single-cycle extraction shifts octaves by `-round(log2(cycles))`
- Only applied when confidence > 0.4
- Use `analyze_octaves.py` to see raw AC/FFT period detection results
- Unlooped percussive samples use a different extraction path (`_extract_unlooped_cycle`) with lowpass filtering

### "Pattern data doesn't match Furnace display"
- Furnace pattern view is 1-indexed for channels (Ch1 = channel 0)
- Effect `0x11` with value `0x01` displays as `1101` in Furnace
- The PATN binary packs effects as `cmd | (val << 8)` — a tuple `(0x11, 0x01)` will crash the writer; use the packed integer `0x111`

## Debug Script Pattern

For ad-hoc investigation, create a temporary script that imports the converter internals:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / 'lib'))

from convert_mod import (_optimize_wavetables, _scan_fx_usage, _apply_persistence,
                         _scan_max_note_durations, _find_free_noise_channel, mod_vol_to_pce)
from mod_parser import parse_mod, ModNote
from sample_processor import process_samples_for_pce

song = parse_mod('Tinytune.mod')
# ... inspect internals ...
```

Delete the script when done.
