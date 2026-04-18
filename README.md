# MOD → Furnace Converter for PC Engine

Converts ProTracker `.mod` files into Furnace `.fur` tracker files targeting the **PC Engine / TurboGrafx-16** sound chip (HuC6280, 6 wavetable channels).

## Quick Start

```bash
python convert_mod.py Tinytune.mod              # outputs Tinytune.fur
python convert_mod.py input.mod output.fur      # explicit output path
python convert_mod.py input.mod --noise_insts=5,8  # force samples 5,8 to noise channels
```

Requires Python 3.8+ and NumPy.

## What It Does

1. **Parses** a 4/6/8-channel ProTracker MOD (M.K., 4CHN, 6CHN, 8CHN, FLT4, etc.)
2. **Classifies** each sample as tonal, percussive, or noise
3. **Extracts** 32-sample 5-bit wavetables from sample data using single-cycle detection
4. **Maps** MOD effects to Furnace effect IDs with persistent-effect re-emission
5. **Compacts** instruments and wavetables (drops unused, deduplicates, detects canonical shapes)
6. **Writes** a Furnace v232 `.fur` file compatible with Furnace 0.6.8.3

## Project Structure

```
convert_mod.py          Main converter — orchestrates parsing, processing, writing
convert_mod.bat         Windows batch runner

lib/
  mod_parser.py         ProTracker MOD parser (samples, patterns, orders)
  fur_writer.py         Furnace .fur writer (v232 INFO format, zlib compressed)
  sample_processor.py   Sample → wavetable conversion + instrument macros
  effect_mapper.py      MOD → Furnace effect ID mapping (reference; not used by persistence engine)

tools/
  analyze_octaves.py    Diagnostic — fundamental period detection per sample
  dump_pattern.py       Inspect PATN blocks with effectMask decoding
  dump_wavetables.py    Inspect wavetable data from a MOD file
  verify_fur.py         Validate .fur file structure (block pointers, counts)
```

## Technical Details

### Volume Scaling

MOD uses linear volume 0–64. PCE uses logarithmic volume 0–31 with 1.5 dB per step.

$$v_{pce} = \text{round}\!\left(31 + \frac{20 \cdot \log_{10}(v_{mod}/64)}{1.5}\right)$$

### Wavetable Extraction & Single-Cycle Detection

MOD samples are 8-bit PCM at variable lengths. The PCE wavetable hardware plays exactly 32 samples as one cycle. When a MOD loop region contains $N$ cycles of the fundamental waveform, naively resampling the entire loop into 32 samples produces a wavetable that plays $N$ octaves too high.

**The fix**: detect the fundamental period and extract exactly one cycle.

#### Detection Method

Two complementary algorithms run on the loop region of each looped sample:

**Autocorrelation** (primary for long loops):
1. Subtract mean, compute normalized autocorrelation $R[k] = \frac{\sum_{n} x[n] \cdot x[n+k]}{\sum_{n} x[n]^2}$
2. Walk past the initial descent from $R[0]=1$ to the first valley
3. Collect all local peaks after the valley
4. The strongest peak's lag = fundamental period
5. Confidence = peak correlation value (0–1)

**FFT** (cross-check, more reliable for short loops):
1. Apply Hanning window, compute `rfft`
2. Find the dominant spectral bin (skipping DC)
3. Convert bin index to period: $T = N / k_{peak}$

#### Fusion Logic

The two methods are combined to handle edge cases:

| Condition | Action |
|-----------|--------|
| AC confidence > 0.5 and period ≥ 8 | Trust autocorrelation |
| AC and FFT agree within 30% | Use AC (more precise), boost confidence |
| AC period is an integer multiple/divisor of FFT period | AC found a harmonic — use FFT |
| AC confidence < 0.3 | Use FFT if it gives a sensible result |
| Neither method confident | Fall back to full loop (no correction) |

#### Extraction

For looped samples where detection confidence > 0.4 and cycles > 1.4:
- Extract `loop[0 : round(period)]` instead of the full loop
- Resample to 32 samples via linear interpolation
- Quantize to 5-bit (0–31)

Unlooped (percussive) samples skip period detection — their attack transient matters more than pitch.

#### Canonical Waveform Detection

After extraction, wavetables are checked against canonical PCE shapes (triangle, sine, 50% square) using Pearson correlation. Matches above $r > 0.92$ are replaced with the mathematically perfect canonical waveform, improving sound quality and enabling deduplication.

### Effect Persistence Engine

MOD effects are **persistent** — vibrato continues until a new effect replaces it. Furnace effects are **one-shot** — they must be explicitly written on every row they're active. The converter bridges this gap. (NB: I am not 100% sure about this - will revisit later, seems to work for now)

#### Categories

Five persistence categories, matching Furnace's own MOD import logic (`mod.cpp`):

| Category | MOD Effects | Furnace ID |
|----------|-------------|------------|
| Arpeggio | 0x00 (param≠0) | 0x00 |
| Slide | 0x01, 0x02, 0x03 | 0x01/0x02/0x03 |
| Vibrato | 0x04 | 0x04 |
| Tremolo | 0x07 | 0x07 |
| Vol Slide | 0x0A | 0xFA |

Combined effects 0x05 (porta+volslide) and 0x06 (vib+volslide) are split into two categories.

#### Algorithm

For each channel, across all reachable patterns:

1. **Scan pass**: Build `fx_usage[5]` — which categories the channel ever uses
2. **Convert pass** per row:
   - Reset `cur_state[5]` to 0
   - For each MOD effect present, update `cur_state[cat]` with the **memory-resolved** value (param=0 recalls last nonzero param via `set_state`)
   - If `set_state[cat]` was never set (=-1) and param=0, skip (effect never seen before)
   - For each category where `fx_usage[cat]` is true and `cur_state[cat] != last_state[cat]`: emit the Furnace effect
   - Copy `cur_state` → `last_state`

This produces the same pattern data as Furnace's native MOD import, including "stop" emissions (e.g., `0400` when vibrato ceases) and deduplication of unchanged states.

### Instrument & Wavetable Compaction

MOD files define 31 sample slots, most typically empty. The converter:

1. Scans patterns for actually-used instrument indices
2. Drops unused instruments and their wavetables
3. Deduplicates identical wavetables (all empties share one slot)
4. Remaps instrument indices in pattern data to compact numbering

Example: Tinytune.mod has 31 sample slots → 7 used instruments, 7 unique wavetables.

### Furnace File Format

Targets **Furnace v232** (Furnace 0.6.8.3) with the INFO block format:

- **Chip**: PC Engine (0x05), 6 channels
- **Timing**: MOD tick rate = BPM × 2 / 5 Hz
- **Patterns**: PATN blocks with compressed row encoding and 16-bit effectMask
- **Instruments**: INS2 blocks with volume/wave/noise macros
- **Wavetables**: WAVE blocks, 32 samples × 5-bit
- **Compression**: zlib-compressed after assembly

## Diagnostic Tools

### tools/analyze_octaves.py

Shows period detection results for every used sample — both autocorrelation and FFT — so you can verify the single-cycle extraction is working correctly.

```bash
python tools/analyze_octaves.py Tinytune.mod
```

```
Idx  Name                  Type   LoopLen   AC Per   AC Cyc   AC Cnf  FFT Per  FFT Cyc  OctShift  Action
  2  ST-14:flutesmaj       loop      7108    190.0    37.41    0.737     47.7   149.00        -5  1-cycle
  7  ST-01:Guitar4         loop        64      7.0     9.14    0.301     21.3     3.00        -2  1-cycle
```

### tools/dump_pattern.py

Dumps decoded pattern data with note names, instruments, volumes, and all effect columns.

```bash
python tools/dump_pattern.py Tinytune_new.fur 0 18    # pattern 0, first 18 rows
```

### tools/verify_fur.py

Validates the `.fur` file structure: block pointers, instrument/wavetable/pattern counts, and magic bytes.

```bash
python tools/verify_fur.py Tinytune_new.fur
```

## Limitations

- **6 channels max**: MOD files with more than 6 channels are truncated (PCE has 6 wavetable channels)
- **No PCM sample support**: Long samples are converted to single-cycle wavetables, not PCM. Original samples are exported to a `.zip` for reference
- **Finetune ignored**: The MOD sample finetune field (sub-semitone tuning) is not yet mapped to Furnace detune
- **Noise channel**: Noise-classified instruments are migrated to channels 5-6 (PCE noise channels) when possible, but polyphonic noise may be dropped. Use `--noise_insts` to manually tag instruments that should use noise mode
- **Wavetable fidelity**: Complex multi-cycle waveforms lose harmonic richness when reduced to a single cycle. The original MOD samples are preserved in the exported `.zip` for manual refinement in Furnace

## Effect Mapping Reference

| MOD Effect | Furnace | Notes |
|------------|---------|-------|
| 0 (arp) | 00 | Persistent, param≠0 only |
| 1 (porta up) | 01 | Persistent |
| 2 (porta down) | 02 | Persistent |
| 3 (tone porta) | 03 | Persistent |
| 4 (vibrato) | 04 | Persistent |
| 5 (porta+vslide) | 03 + FA | Split into two effect columns |
| 6 (vib+vslide) | 04 + FA | Split into two effect columns |
| 7 (tremolo) | 07 | Persistent |
| 9 (sample offset) | 91 | One-shot |
| A (vol slide) | FA | Persistent |
| B (position jump) | 0B | One-shot |
| C (set volume) | Volume column | Not an effect |
| D (pattern break) | 0D | BCD→decimal conversion |
| F (speed/tempo) | 0F / F0 | ≤0x20 = speed, >0x20 = tempo |
| E1 (fine porta up) | F1 | One-shot |
| E2 (fine porta down) | F2 | One-shot |
| E9 (retrigger) | 0C | One-shot |
| EA (fine vol up) | F8 | One-shot |
| EB (fine vol down) | F9 | One-shot |
| EC (note cut) | EC | One-shot |
| ED (note delay) | ED | One-shot |

### Noise Channel Migration

PCE channels 5-6 (0-indexed 4-5) support a hardware noise mode via the `11xx` effect. The converter automatically migrates noise-classified instruments from their original MOD channel to PCE channels 5-6.

#### Automatic Classification

Samples are classified as `noise` by keyword matching (hat, hihat, cymbal, shaker, etc.) or by high zero-crossing rate (>0.6). Not all drums are noise — kick drums and snares typically play pitched wavetable cycles.

#### Manual Override: `--noise_insts`

When automatic detection misses an instrument, force it with:

```bash
python convert_mod.py Tinytune.mod --noise_insts=5    # sample 5 (0-based)
python convert_mod.py input.mod --noise_insts=5,8     # multiple samples
```

The index is the 0-based sample index as shown in the converter output (e.g., `Sample  5: [percussive] ...`).

#### What Happens

1. Overridden instruments get `classification=noise`, a noise macro (`[1]` with loop), and are added to the noise instrument set
2. During pattern building, notes using noise instruments are moved from their source channel (0-3) to the first free noise channel (4 or 5)
3. Each migrated note gets a `1101` effect (noise enable) injected into its effect column
4. The source channel row is blanked (no ghost note left behind)
5. If both noise channels are occupied at a given row, the note stays on its original channel and a warning is printed

#### PCE Noise Effects

| Effect | Meaning |
|--------|---------|
| `1101` | Noise mode ON |
| `1100` | Noise mode OFF |
| `17xx` | PCM sample mode (not used by this converter) |
