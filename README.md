# MOD/XM/S3M/FTM → Furnace Converter for PC Engine

Converts ProTracker `.mod`, FastTracker II `.xm`, Scream Tracker `.s3m`, and FamiTracker `.ftm` files into Furnace `.fur` tracker files targeting the **PC Engine / TurboGrafx-16** sound chip (HuC6280, 6 wavetable channels).

## Quick Start

```bash
python convert_mod.py Tinytune.mod              # outputs Tinytune.fur
python convert_mod.py input.mod output.fur      # explicit output path
python convert_mod.py input.mod --noise_insts=5,8  # force samples 5,8 to noise channels
python convert_mod.py song.xm                   # auto-detects XM format
python convert_mod.py song.xm --drop_channels=5,6 --noise_channel=4
python convert_mod.py song.s3m                   # auto-detects S3M format
python convert_mod.py song.s3m --merge_channels=auto     # auto-merge to fit 6 channels
python convert_mod.py song.s3m --split_extra             # save overflow channels to second .fur
python convert_mod.py song.ftm                   # auto-detects FamiTracker format
python convert_mod.py song.ftm output.fur        # explicit output path
```

Requires Python 3.8+ and NumPy.

## What It Does

1. **Parses** MOD (4/6/8-channel ProTracker), XM (FastTracker II, any channel count), S3M (Scream Tracker 3, any channel count), or FTM (FamiTracker, NES 2A03) files
2. **Classifies** each sample/instrument as tonal, percussive, or noise
3. **Extracts** 32-sample 5-bit wavetables from sample data using single-cycle detection
4. **Maps** MOD/XM effects to Furnace effect IDs with persistent-effect re-emission
5. **Compacts** instruments and wavetables (drops unused, deduplicates, detects canonical shapes)
6. **Writes** a Furnace v232 `.fur` file compatible with Furnace 0.6.8.3

For XM files, multi-sample instruments pick the most-used sub-sample, and XM volume envelopes are converted to PCE volume macros with sustain/loop/release mapping.

For FTM files, volume and arpeggio envelopes from the SEQUENCES block are extracted and converted to Furnace instrument macros. NES 2A03 channels are mapped to PCE wavetable channels with appropriate volume scaling.

## Project Structure

```
convert_mod.py          Main converter — orchestrates parsing, processing, writing
convert_mod.bat         Windows batch runner

lib/
  mod_parser.py         ProTracker MOD parser (samples, patterns, orders)
  xm_parser.py          FastTracker II XM parser (instruments, envelopes, packed patterns)
  s3m_parser.py         Scream Tracker S3M parser (packed patterns, unsigned samples)
  ftm_parser.py         FamiTracker FTM parser (SEQUENCES, instruments, patterns)
  fur_writer.py         Furnace .fur writer (v232 INFO format, zlib compressed)
  sample_processor.py   Sample → wavetable conversion + instrument macros
  effect_mapper.py      MOD → Furnace effect ID mapping (reference; not used by persistence engine)
  merge_analysis.py     Channel merge analysis — scoring, plan generation, auto-merge

tools/
  analyze_octaves.py    Diagnostic — fundamental period detection per sample
  dump_pattern.py       Inspect PATN blocks with effectMask decoding
  dump_wavetables.py    Inspect wavetable data from a MOD file
  verify_fur.py         Validate .fur file structure (block pointers, counts)
  merge_analysis.py     Standalone merge analysis report (imports from lib/)
  regression_test.py    Regression test runner across all examples

examples/
  TinyTune/             MOD example with convert.bat
  LittleSwedishGirl/    XM example with convert.bat (9ch, --drop_channels demo)
  SatteliteOne/         S3M example with convert.bat (8ch, --drop_channels + --merge_channels demos)
  FamiTracker_EnhantedLands/  FTM example with convert.bat (volume + arpeggio envelopes)
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

- **6 channels max**: Files with more than 6 channels are truncated (PCE has 6 wavetable channels). Use `--drop_channels` to choose which to remove, `--merge_channels` to fold channels together preserving notes, or `--split_extra` to save overflow channels to a second `.fur` file
- **No PCM sample support**: Long samples are converted to single-cycle wavetables, not PCM. Original samples are exported to a `.zip` for reference
- **Finetune ignored**: The MOD sample finetune field (sub-semitone tuning) is not yet mapped to Furnace detune
- **Noise channel**: Noise-classified instruments are migrated to channels 5-6 (PCE noise channels) when possible, but polyphonic noise may be dropped. Use `--noise_insts` to manually tag instruments that should use noise mode
- **Wavetable fidelity**: Complex multi-cycle waveforms lose harmonic richness when reduced to a single cycle. The original MOD samples are preserved in the exported `.zip` for manual refinement in Furnace
- **XM multi-sample instruments**: Only the most-used sub-sample (by note mapping frequency) is kept per instrument. Other sub-samples are discarded
- **XM volume column priority**: When both the volume column and effect column contain an effect, the effect column wins; the volume column effect is lost

## S3M-Specific Features

### Format Support

Scream Tracker 3 `.s3m` files are auto-detected by extension. Key differences from MOD/XM handled by the parser:

- **Packed patterns**: S3M uses a per-row channel mask with flag-based field packing (note+inst, volume, effect+param)
- **Unsigned 8-bit samples**: S3M samples are unsigned (bias 128), converted to signed for processing
- **Pattern break (Cxx)**: S3M Cxx param is plain hex, unlike MOD's BCD encoding — the parser converts to BCD for the downstream pipeline
- **Global volume**: Scaled into per-channel volume calculations

### Channel Merging

When a song has more channels than the PCE's 6, merging folds a donor channel's notes into a target channel's gaps (silent rows) — preserving notes that would otherwise be lost by truncation.

**`--merge_channels=D:T[,D2:T2]`** — Manual merge (1-based channels):
```bash
python convert_mod.py song.s3m out.fur --merge_channels=5:3,7:6
```
Donor 5 merges into target 3, then donor 7 into target 6. Donors are removed after merge. If the donor note conflicts with an existing target note, the donor note is lost.

**`--merge_channels=auto`** — Auto-select optimal merge plan:
```bash
python convert_mod.py song.s3m out.fur --merge_channels=auto
```
Evaluates all possible merge+drop combinations to maximize preserved notes. Prints the chosen plan and proceeds with conversion.

**`--merge_channels=analyze`** — Print analysis report and exit:
```bash
python convert_mod.py song.s3m out.fur --merge_channels=analyze
```
Shows per-channel activity, pairwise merge scores, and ranked plans with preservation percentages. Use this to inform manual `--merge_channels` decisions.

**Standalone analysis tool**:
```bash
python tools/merge_analysis.py song.s3m
```

#### Merge Scoring

For each donor→target pair, the analyzer computes:
- **preserved**: donor notes that fill target gaps (no conflict)
- **conflicts**: rows where both channels have notes (donor note lost)
- **pct_preserved**: preserved / total donor notes

Plans are ranked by total preserved notes across all merges + remaining channels.

#### Processing Order

`drop → merge → noise swap → split_extra → limit_to_6`

Merges run before truncation, so channels beyond 6 can be merged into channels 1–6 instead of being silently dropped.

### Split Extra Channels

**`--split_extra`** saves overflow channels (7+) to a separate `.fur` file instead of discarding them:

```bash
python convert_mod.py song.s3m out.fur --split_extra
# Produces: out.fur (channels 1-6) + out_extra.fur (channels 7+)
```

The extra `.fur` shares the same instruments, wavetables, tempo, and order list as the main file. It pads to 6 Furnace channels (PCE requirement) with empty rows for unused slots. This allows manual refinement in Furnace — you can cherry-pick parts from the extra file into the main conversion.

Combinable with other options:
```bash
python convert_mod.py song.s3m out.fur --merge_channels=5:3 --split_extra
# Merges ch5→ch3, then splits remaining overflow to extra .fur
```



### Format Auto-Detection

The converter detects MOD vs XM by file extension (`.mod` / `.xm`). Both formats produce the same `ModSong` intermediate representation and flow through the same pipeline.

### Channel Management

XM files often have more than 6 channels. Two CLI options help fit the music into PCE's 6 channels:

**`--drop_channels=N,M`** removes channels (1-based) before any other processing:
```bash
python convert_mod.py song.xm --drop_channels=5,6,7   # remove channels 5, 6, 7
```

**`--noise_channel=N[,M]`** swaps channel N into PCE noise slot (ch5), and optionally M into ch6:
```bash
python convert_mod.py song.xm --noise_channel=4       # ch4 <-> ch5
python convert_mod.py song.xm --noise_channel=4,7     # ch4 <-> ch5, ch7 <-> ch6
```

Processing order: drop → swap → limit to 6 → conversion.

### Variable Pattern Length

XM patterns can have different row counts (1-256). The converter sets `rows_per_pattern` to the longest pattern in the file. MOD files always use 64 rows.

### XM Volume Envelopes

XM instruments can have multi-point volume envelopes with sustain and loop points. These are interpolated to per-frame PCE volume macros:

- **Sustain point** → Furnace macro loop (holds until note-off)
- **Envelope loop** → Furnace macro loop (repeats segment)
- **Fadeout** → applied post-release (mapped to Furnace release envelope)

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

## FamiTracker (.ftm) Support

### Format Overview

FamiTracker `.ftm` files target the NES 2A03 (2 pulse + 1 triangle + 1 noise + 1 DPCM). The converter maps NES channels to PCE wavetable channels and preserves instrument envelope data that would otherwise be lost in a naive conversion.

### SEQUENCES Block Parsing

FTM files store instrument envelopes in a SEQUENCES block. Each sequence has a type and an index:

| Type | ID | Purpose |
|------|----|---------|
| Volume | 0 | Amplitude envelope (NES 0–15) |
| Arpeggio | 1 | Semitone offset pattern |
| Pitch | 2 | Fine pitch sweep |
| Hi-Pitch | 3 | Coarse pitch sweep |
| Duty | 4 | Pulse width (NES-specific, ignored) |

The converter supports SEQUENCES block versions 1–6. Older versions (≤2) use the **COldSequence** pair format: each step is a `(value, length)` byte pair where `length ≥ 0` means repeat `value` for `length+1` frames, and `length < 0` means loop back `|length|` items. Newer versions (3+) store flat value arrays with explicit loop/release point indices.

### Envelope Conversion

**Volume envelopes**: NES volume (0–15) is scaled to PCE volume (0–31) by multiplying by 2, capped at 31. Loop points are preserved as Furnace macro loops.

**Arpeggio envelopes**: Signed semitone offsets are passed through directly to Furnace arpeggio macros (macro code 1 in INS2 format). Loop points are preserved.

Pitch, Hi-Pitch, and Duty sequences are currently parsed but not mapped to Furnace macros.

### Instrument → Sequence Binding

Each FTM instrument stores 5 `(enabled, seq_index)` pairs — one per sequence type. During conversion, the parser resolves these references and attaches the expanded envelope data to each `ModSample`. The downstream pipeline reads `ftm_volume_env`, `ftm_arp_env`, and their loop/release indices to override the default flat macros.

### Tempo Mapping

FamiTracker's "Tempo" value is **not** BPM. The actual BPM is:

$$\text{BPM} = \frac{60 \times \text{Tempo} \times 6}{150 \times \text{Speed}}$$

For example, Tempo=150 with Speed=7 yields BPM ≈ 128.57. The converter stores the raw Speed value and frame rate (60 Hz for NTSC) in the Furnace file, letting Furnace compute the correct playback rate. A Furnace display of "128 BPM" for FT Tempo=150 / Speed=7 is correct.

### Debugging FTM Conversions

When an FTM conversion sounds wrong, check:

1. **"FTM volume envelopes applied to N instruments"** — confirms envelope extraction worked. If N=0, the SEQUENCES block may be missing or all instruments have envelopes disabled.
2. **Arpeggio values** — open the `.fur` in Furnace, inspect the instrument macro tab. Arpeggio values should match FamiTracker's sequence editor (signed semitone offsets).
3. **Triangle channel** — NES triangle has no volume control (always full or silent). The converter assigns a flat volume envelope [31]. If the triangle sounds too loud relative to other channels, adjust manually in Furnace.
4. **Noise instruments** — NES noise channel (ch4) maps to PCE noise channels (5–6) with `1101` noise-enable effects. Verify noise notes landed on the correct channels.

## Regression Tests

The regression suite converts all example files and validates output structure:

```bash
python tools/regression_test.py           # run all tests
python tools/regression_test.py --keep    # keep temp .fur files for inspection
```

Each example directory under `examples/` must have a `convert.bat` whose `python convert_mod.py ...` line is parsed for the source file and CLI flags. The test runner:

1. Parses `convert.bat` for source file and flags
2. Runs the conversion to a temporary `.fur` file
3. Validates the output with `tools/verify_fur.py`
4. Reports PASS/FAIL/SKIP per example

**Current test coverage** (5 examples):

| Example | Format | Key Features Tested |
|---------|--------|---------------------|
| TinyTune | MOD | Basic conversion, noise instruments, arpeggio cloning |
| LittleSwedishGirl | XM | 9-channel drop, arpeggio patterns |
| SatteliteOne | S3M | Channel drop + merge |
| SecondReality | S3M | Large instrument set (54 → 31 used) |
| FamiTracker_EnhantedLands | FTM | Volume + arpeggio envelope extraction |

Run the suite after any converter changes to catch regressions.
