# lib/sample_processor.py
# MOD sample → PCE wavetable + instrument macro conversion
#
# Classification:
#   tonal      — looped sample → extract one cycle from loop region, sustaining envelope
#   percussive — unlooped or short → extract first 32 bytes, decaying envelope
#   noise      — name/waveform suggests noise (hi-hat, snare buzz) → noise macro + decay

import math
import struct
import zipfile
import io
import numpy as np
from typing import List, Optional


# ─── Classification ───

NOISE_KEYWORDS = {"hat", "hh", "hihat", "hi-hat", "cymbal", "cym", "noise",
                  "rim", "shaker", "tamb", "cabasa", "marac"}

PERC_KEYWORDS = {"kick", "bass drum", "bd", "snare", "sn", "tom", "clap",
                 "cowbell", "conga", "bongo", "wood", "click", "perc"}


def classify_sample(sample) -> str:
    """Classify MOD sample as 'tonal', 'percussive', or 'noise'."""
    name_lower = sample.name.lower().strip()

    for kw in NOISE_KEYWORDS:
        if kw in name_lower:
            return "noise"
    for kw in PERC_KEYWORDS:
        if kw in name_lower:
            return "percussive"

    if sample.loop_length > 2:
        return "tonal"

    if sample.length < 512:
        return "percussive"

    # Waveform analysis: high zero-crossing rate → noise-like
    if len(sample.data) > 32:
        data = np.array(sample.data[:min(512, len(sample.data))],
                        dtype=np.uint8).astype(np.int16) - 128
        if len(data) > 1:
            crossings = np.sum(np.diff(np.sign(data)) != 0)
            zcr = crossings / len(data)
            if zcr > 0.6:
                return "noise"

    return "percussive"


# ─── Wavetable extraction ───

def _detect_fundamental_period(signal):
    """Detect fundamental period in a signal using normalized autocorrelation.

    Returns (period_in_samples, confidence) where confidence is 0-1.
    High confidence (>0.5) means a clear periodic signal was found.
    """
    n = len(signal)
    if n < 8:
        return n, 0.0

    sig = signal - np.mean(signal)
    # Normalized autocorrelation
    corr = np.correlate(sig, sig, mode='full')
    corr = corr[n - 1:]  # positive lags only
    if corr[0] == 0:
        return n, 0.0
    corr = corr / corr[0]

    # Walk past initial descent from lag-0 to first valley
    # The correlation starts at 1.0 (lag-0) and descends
    i = 1
    while i < n // 2 and corr[i] >= corr[i - 1]:
        i += 1
    # Continue descending to the bottom of the valley
    while i < n // 2 and corr[i] <= corr[i - 1]:
        i += 1
    if i >= n // 2:
        return n, 0.0

    # Now we're ascending from valley — collect ALL local peaks
    peaks = []
    j = i
    while j < n // 2:
        # Ascend to peak
        while j + 1 < n // 2 and corr[j + 1] >= corr[j]:
            j += 1
        peaks.append((j, corr[j]))
        # Descend past this peak
        while j + 1 < n // 2 and corr[j + 1] <= corr[j]:
            j += 1
        j += 1

    if not peaks:
        return n, 0.0

    # The strongest peak is most likely the fundamental (not necessarily the first)
    best_lag, best_val = max(peaks, key=lambda p: p[1])
    return best_lag, best_val


def _detect_period_fft(signal):
    """FFT-based fundamental period detection (cross-check for autocorrelation).

    Returns (period_in_samples, magnitude).
    """
    n = len(signal)
    if n < 8:
        return n, 0.0

    sig = signal - np.mean(signal)
    windowed = sig * np.hanning(n)
    spectrum = np.abs(np.fft.rfft(windowed))

    min_bin = max(1, n // 256)
    max_bin = n // 4
    if max_bin <= min_bin:
        return n, 0.0

    peak_bin = min_bin + np.argmax(spectrum[min_bin:max_bin])
    period = n / peak_bin if peak_bin > 0 else n
    return period, float(spectrum[peak_bin])


def extract_wavetable(sample, target_size: int = 32) -> dict:
    """Extract a 32-sample 5-bit wavetable from a MOD sample.

    Looped samples: detect fundamental period via autocorrelation and extract
    exactly ONE cycle for correct pitch. Falls back to full loop if detection
    is unreliable.
    Unlooped: extract first chunk (up to 256 bytes).
    Empty: return flat DC center.

    Returns dict with keys: wavetable, cycles_detected, confidence, octave_shift
    """
    info = {"wavetable": [16] * target_size, "cycles_detected": 1.0,
            "confidence": 0.0, "octave_shift": 0}

    if sample.length == 0 or len(sample.data) == 0:
        return info

    raw = np.array(sample.data, dtype=np.uint8).astype(np.float32) - 128.0

    if sample.loop_length > 2 and sample.loop_start < len(raw):
        loop_end = min(sample.loop_start + sample.loop_length, len(raw))
        loop = raw[sample.loop_start:loop_end]
        if len(loop) == 0:
            loop = raw[:min(target_size, len(raw))]

        # Single-cycle extraction for looped samples
        if len(loop) >= 8:
            ac_period, ac_conf = _detect_fundamental_period(loop)
            fft_period, _ = _detect_period_fft(loop)

            # Choose best period estimate:
            # - If AC and FFT agree (within 30%), use AC (more precise)
            # - If they disagree, prefer the one giving a sensible cycle count
            # - For short loops (<128), FFT often more reliable
            fft_cycles = len(loop) / fft_period if fft_period > 0 else 1.0
            ac_cycles = len(loop) / ac_period if ac_period > 0 else 1.0

            if ac_conf > 0.5 and ac_period >= 8:
                # High confidence AC — trust it
                best_period = ac_period
                confidence = ac_conf
            elif fft_period >= 8 and fft_cycles >= 1.4:
                # FFT gives a reasonable result
                if ac_conf > 0.3 and ac_period >= 8:
                    # AC has some signal — check if they agree
                    ratio = ac_period / fft_period if fft_period > 0 else 999
                    if 0.7 < ratio < 1.3:
                        best_period = ac_period  # agree, use more precise AC
                        confidence = max(ac_conf, 0.5)
                    elif abs(round(ratio) - ratio) < 0.35 and round(ratio) >= 2:
                        # AC found a harmonic (period is 1/Nth of FFT) — use FFT
                        best_period = fft_period
                        confidence = 0.5
                    elif abs(1/ratio - round(1/ratio)) < 0.35 and round(1/ratio) >= 2:
                        # AC found a subharmonic — use FFT
                        best_period = fft_period
                        confidence = 0.5
                    else:
                        best_period = fft_period
                        confidence = 0.45
                else:
                    best_period = fft_period
                    confidence = 0.45
            else:
                best_period = ac_period
                confidence = ac_conf

            cycles = len(loop) / best_period if best_period > 0 else 1.0
            info["cycles_detected"] = cycles
            info["confidence"] = confidence

            if cycles > 1.4:
                info["octave_shift"] = -round(math.log2(cycles))

            # Extract single cycle if confident and period is large enough
            if confidence > 0.4 and best_period >= 8 and cycles > 1.4:
                cycle = loop[:int(round(best_period))]

                # Secondary refinement: the AC-detected "period" may itself
                # contain multiple waveform cycles (e.g. a flute sample where
                # the macro-period repeats every 238 samples but contains 5-6
                # actual sine-like oscillations). If we resample 238 samples
                # into 32, we get ~6 oscillations in the wavetable which plays
                # at a higher pitch. Extract one waveform cycle instead.
                if len(cycle) >= 16:
                    sub_period, _ = _detect_period_fft(cycle)
                    if sub_period >= 4:
                        sub_cycles = len(cycle) / sub_period
                        if sub_cycles >= 1.8:
                            one_cycle_len = int(round(sub_period))
                            cycle = cycle[:one_cycle_len]
                            # Recalculate: we now have 1 true waveform cycle.
                            # The total waveform cycles in the full loop:
                            total_wf_cycles = len(loop) / sub_period
                            info["cycles_detected"] = total_wf_cycles
                            # Octave shift: previously, resampling the full
                            # loop into 32 samples baked in total_wf_cycles
                            # oscillations. But the MOD notes were authored for
                            # the original sample rate, not the wavetable rate.
                            # The AC macro-period was the repeating unit; sub-
                            # cycles within it are harmonics, not extra octaves.
                            # Use the AC cycle count for the octave shift, not
                            # the waveform cycle count.
                            info["octave_shift"] = -round(math.log2(cycles))
            else:
                cycle = loop
        else:
            cycle = loop
    else:
        # Unlooped sample: try to extract a clean single-cycle waveform.
        # Many MOD samples (bass, plucks) have high-frequency texture on top
        # of a slower fundamental. Lowpass filtering reveals the fundamental
        # period, then we extract one cycle and compute octave correction.
        cycle = _extract_unlooped_cycle(raw, info)

    info["wavetable"] = _resample_and_quantize(cycle, target_size)
    return info


def _extract_unlooped_cycle(raw: np.ndarray, info: dict) -> np.ndarray:
    """Extract a single waveform cycle from an unlooped sample.

    Strategy: lowpass filter to suppress high-frequency texture, detect the
    fundamental period via AC on the filtered signal, then use FFT sub-cycle
    refinement to isolate one clean waveform cycle from the *filtered* data.
    Falls back to first 256 raw bytes if detection fails.
    """
    MIN_ANALYSIS_LEN = 256
    LOWPASS_KERNEL_SZ = 16
    SKIP_TRANSIENT = 256  # skip initial attack transient

    if len(raw) < MIN_ANALYSIS_LEN:
        return raw[:min(len(raw), 256)]

    # Take a stable region past the initial transient for analysis
    analysis_start = min(SKIP_TRANSIENT, len(raw) // 4)
    analysis_end = min(analysis_start + 2304, len(raw))
    region = raw[analysis_start:analysis_end]

    if len(region) < 64:
        return raw[:min(len(raw), 256)]

    # Lowpass to reveal fundamental (suppresses high-freq texture)
    kernel = np.ones(LOWPASS_KERNEL_SZ) / LOWPASS_KERNEL_SZ
    filtered = np.convolve(region, kernel, mode='valid')

    if len(filtered) < 32:
        return raw[:min(len(raw), 256)]

    # Detect macro-period on filtered signal
    ac_period, ac_conf = _detect_fundamental_period(filtered)

    if ac_conf < 0.4 or ac_period < 8:
        # Low confidence — try FFT alone on filtered signal
        fft_period, _ = _detect_period_fft(filtered)
        if fft_period >= 16 and len(filtered) / fft_period >= 1.4:
            ac_period = fft_period
            ac_conf = 0.45
        else:
            return raw[:min(len(raw), 256)]

    macro_cycles = len(filtered) / ac_period if ac_period > 0 else 1.0

    # Extract the macro-period chunk from filtered data
    macro_chunk = filtered[:int(round(ac_period))]

    # Sub-cycle FFT refinement (same logic as looped path)
    if len(macro_chunk) >= 16:
        sub_period, _ = _detect_period_fft(macro_chunk)
        if sub_period >= 4:
            sub_cycles_in_macro = len(macro_chunk) / sub_period
            if sub_cycles_in_macro >= 1.8:
                # Multiple waveform cycles in the macro-period — extract one
                cycle = filtered[:int(round(sub_period))]
                total_wf_cycles = len(raw) / sub_period
                info["cycles_detected"] = total_wf_cycles
                info["confidence"] = ac_conf
                info["octave_shift"] = -round(math.log2(total_wf_cycles))
                return cycle

    # Macro-period is itself one cycle
    total_cycles = len(raw) / ac_period
    info["cycles_detected"] = total_cycles
    info["confidence"] = ac_conf
    if total_cycles > 1.4:
        info["octave_shift"] = -round(math.log2(total_cycles))
    return macro_chunk


def _resample_and_quantize(data: np.ndarray, target_size: int = 32) -> List[int]:
    """Resample to target_size and quantize to 5-bit (0-31)."""
    if len(data) == 0:
        return [16] * target_size

    peak = max(abs(data.max()), abs(data.min()), 1.0)
    data = data / peak

    indices = np.linspace(0, len(data) - 1, target_size)
    resampled = np.interp(indices, np.arange(len(data)), data)

    wavetable = ((resampled + 1.0) * 15.5).clip(0, 31).astype(np.uint8)
    return wavetable.tolist()


# ─── Volume envelope generation ───

def make_volume_envelope(classification: str, pce_volume: int,
                         sample=None, max_note_rows: int = 0,
                         speed: int = 6) -> dict:
    """Generate volume envelope from sample amplitude contour.

    For samples with enough data, computes RMS envelope from the raw waveform
    and maps it to a PCE volume macro. For short/empty samples, falls back to
    classification-based generic envelopes.

    max_note_rows: if > 0, the maximum number of pattern rows this instrument
        is heard in the song. For unlooped percussive samples, limits the
        analysis window and generates a decay that fits the actual play time.
    speed: ticks per row (MOD speed). Envelope steps map ~1:1 to ticks.
    """
    vol = max(0, min(31, pce_volume))

    # Try to derive envelope from actual sample data
    if sample is not None and sample.length > 0 and len(sample.data) >= 32:
        raw = np.array(sample.data, dtype=np.uint8).astype(np.float32) - 128.0

        # For unlooped samples with known short play duration, limit the
        # analysis window to what's actually heard and force a decay envelope.
        if classification != "tonal" and max_note_rows > 0 and sample.loop_length <= 2:
            # MOD sample rate ~8363 Hz. Effective samples heard:
            samples_per_row = 8363 * speed / 50  # PAL 50Hz
            effective_len = int(max_note_rows * samples_per_row)
            effective_len = min(effective_len, len(raw))
            raw_eff = raw[:effective_len]

            # Generate a decay envelope that fits within the play time.
            # Envelope ticks ~= speed * max_note_rows (one tick per row-tick).
            env_ticks = max(4, min(16, speed * max_note_rows))
            chunk_sz = max(1, len(raw_eff) // env_ticks)
            peak_rms = max(np.sqrt(np.mean(raw_eff[:max(1, chunk_sz)] ** 2)), 1.0)
            env = []
            for i in range(env_ticks):
                chunk = raw_eff[i * chunk_sz:min((i + 1) * chunk_sz, len(raw_eff))]
                if len(chunk) > 0:
                    rms = np.sqrt(np.mean(chunk ** 2))
                    env.append(max(0, min(31, round(vol * rms / peak_rms))))
            # If raw envelope is mostly flat (sample has constant amplitude),
            # apply an artificial logarithmic decay — the MOD retriggers
            # the note frequently so it acts as a short hit, not a sustain.
            if len(env) >= 4 and min(env) > vol * 0.7:
                env = _log_decay(vol, env_ticks)
            # Trim trailing zeros
            while len(env) > 1 and env[-1] == 0:
                env.pop()
            if not env:
                env = [0]
            return {
                "volume_env": env,
                "volume_loop": 255,
                "volume_release": 255,
            }

        if classification == "tonal" and sample.loop_length > 2:
            # Tonal looped: envelope covers the attack portion (before loop)
            # then sustains at the loop's RMS level.
            attack = raw[:sample.loop_start] if sample.loop_start > 0 else raw[:0]
            loop_start = sample.loop_start
            loop_end = min(loop_start + sample.loop_length, len(raw))
            loop_region = raw[loop_start:loop_end]

            # Sustain level from the loop RMS
            loop_rms = np.sqrt(np.mean(loop_region ** 2)) if len(loop_region) > 0 else 0
            peak_rms = max(np.sqrt(np.mean(raw[:min(len(raw), 512)] ** 2)), 1.0)

            if len(attack) >= 32:
                # Build attack envelope (8-16 steps depending on attack length)
                num_steps = min(16, max(4, len(attack) // 64))
                chunk_size = max(1, len(attack) // num_steps)
                env = []
                for i in range(num_steps):
                    chunk = attack[i * chunk_size:min((i + 1) * chunk_size, len(attack))]
                    if len(chunk) > 0:
                        rms = np.sqrt(np.mean(chunk ** 2))
                        env.append(max(0, min(31, round(vol * rms / peak_rms))))
                # Add sustain level and set loop point there
                sustain_vol = max(0, min(31, round(vol * loop_rms / peak_rms)))
                env.append(sustain_vol)
                return {
                    "volume_env": env,
                    "volume_loop": len(env) - 1,  # loop on sustain
                    "volume_release": 255,
                }
            else:
                # Short/no attack — just sustain
                sustain_vol = max(0, min(31, round(vol * loop_rms / peak_rms)))
                return {
                    "volume_env": [sustain_vol],
                    "volume_loop": 0,
                    "volume_release": 255,
                }
        else:
            # Percussive / noise / unlooped: derive decay from full sample
            num_steps = min(24, max(6, len(raw) // 128))
            chunk_size = max(1, len(raw) // num_steps)
            peak_rms = max(np.sqrt(np.mean(raw[:max(1, chunk_size)] ** 2)), 1.0)
            env = []
            for i in range(num_steps):
                chunk = raw[i * chunk_size:min((i + 1) * chunk_size, len(raw))]
                if len(chunk) > 0:
                    rms = np.sqrt(np.mean(chunk ** 2))
                    env.append(max(0, min(31, round(vol * rms / peak_rms))))
            # Trim trailing zeros
            while len(env) > 1 and env[-1] == 0:
                env.pop()
            if not env:
                env = [0]
            return {
                "volume_env": env,
                "volume_loop": 255,
                "volume_release": 255,
            }

    # Fallback: generic envelopes
    if classification == "tonal":
        return {
            "volume_env": [vol],
            "volume_loop": 0,
            "volume_release": 255,
        }
    elif classification == "noise":
        return {
            "volume_env": _log_decay(vol, 6),
            "volume_loop": 255,
            "volume_release": 255,
        }
    else:  # percussive
        return {
            "volume_env": _log_decay(vol, 10),
            "volume_loop": 255,
            "volume_release": 255,
        }


def _log_decay(vol: int, length: int) -> List[int]:
    """Generate a logarithmic decay envelope from vol to 0."""
    if vol <= 0:
        return [0] * length
    env = []
    for i in range(length):
        frac = 1.0 - (i / (length - 1)) if length > 1 else 0.0
        db_atten = -30.0 * (1.0 - frac)
        linear = 10.0 ** (db_atten / 20.0)
        env.append(max(0, min(31, round(vol * linear))))
    return env


# ─── Noise macro generation ───

def make_noise_macro(classification: str) -> Optional[dict]:
    """Generate noise macro for noise-classified instruments."""
    if classification != "noise":
        return None
    return {
        "noise_env": [1],
        "noise_loop": 0,
    }


# ─── WAV export ───

def export_samples_zip(samples, zip_path: str):
    """Export all MOD samples as individual .wav files in a zip archive."""
    count = 0
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, sample in enumerate(samples):
            if sample.length == 0 or len(sample.data) == 0:
                continue
            data = np.array(sample.data, dtype=np.uint8).astype(np.int8)
            wav_bytes = _make_wav(data, sample_rate=8363)
            name = sample.name.strip() or f"sample_{i:02d}"
            safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
            zf.writestr(f"{i:02d}_{safe_name}.wav", wav_bytes)
            count += 1
    print(f"  Exported {count} samples to {zip_path}")


def _make_wav(data: np.ndarray, sample_rate: int = 8363) -> bytes:
    """Create a minimal WAV file from signed 8-bit PCM data."""
    num_samples = len(data)
    buf = io.BytesIO()
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 36 + num_samples))
    buf.write(b'WAVE')
    buf.write(b'fmt ')
    buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<H', 1))            # PCM
    buf.write(struct.pack('<H', 1))            # mono
    buf.write(struct.pack('<I', sample_rate))
    buf.write(struct.pack('<I', sample_rate))   # byte rate
    buf.write(struct.pack('<H', 1))            # block align
    buf.write(struct.pack('<H', 8))            # bits per sample
    buf.write(b'data')
    buf.write(struct.pack('<I', num_samples))
    buf.write(bytes((data.astype(np.int16) + 128).astype(np.uint8)))
    return buf.getvalue()


# ─── Main entry point ───

def process_samples_for_pce(samples, pce_volumes: List[int] = None,
                            max_note_rows: dict = None, speed: int = 6):
    """Process all MOD samples into PCE wavetables + instrument macro data.

    max_note_rows: dict mapping sample index -> max note duration in rows.
        Used to optimize volume envelopes for short-duration instruments.
    speed: MOD speed (ticks per row).

    Returns list of dicts, one per sample:
      { classification, wavetable, volume_env, volume_loop, volume_release,
        noise_env, noise_loop, cycles_detected, confidence, octave_shift }
    """
    results = []
    if max_note_rows is None:
        max_note_rows = {}

    for i, sample in enumerate(samples):
        vol = pce_volumes[i] if pce_volumes else 31
        classification = classify_sample(sample)

        if sample.length == 0 or len(sample.data) == 0:
            results.append({
                "classification": "empty",
                "wavetable": [16] * 32,
                "volume_env": [0],
                "volume_loop": 255,
                "volume_release": 255,
                "noise_env": None,
                "noise_loop": None,
                "cycles_detected": 1.0,
                "confidence": 0.0,
                "octave_shift": 0,
            })
            continue

        wt_info = extract_wavetable(sample)
        mnr = max_note_rows.get(i, 0)
        vol_data = make_volume_envelope(classification, vol, sample,
                                        max_note_rows=mnr, speed=speed)
        noise = make_noise_macro(classification)

        results.append({
            "classification": classification,
            "wavetable": wt_info["wavetable"],
            "volume_env": vol_data["volume_env"],
            "volume_loop": vol_data["volume_loop"],
            "volume_release": vol_data["volume_release"],
            "noise_env": noise["noise_env"] if noise else None,
            "noise_loop": noise["noise_loop"] if noise else None,
            "cycles_detected": wt_info["cycles_detected"],
            "confidence": wt_info["confidence"],
            "octave_shift": wt_info["octave_shift"],
        })

        tag = f"[{classification}]"
        oct_tag = ""
        if wt_info["octave_shift"] != 0 and wt_info["confidence"] > 0.4:
            oct_tag = f"  (1-cycle extract: {wt_info['cycles_detected']:.1f} cycles, oct {wt_info['octave_shift']:+d})"
        print(f"  Sample {i:2d}: {tag:14s} vol={vol:2d}  {sample.name}{oct_tag}")

    return results