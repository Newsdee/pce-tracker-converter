# analyze_octaves.py - Detect octave correction needed for MOD -> PCE wavetable conversion
# Usage: python analyze_octaves.py <input.mod>
#
# Shows fundamental period detection results for each used sample,
# using both autocorrelation and FFT methods.

import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
from mod_parser import parse_mod
from sample_processor import _detect_fundamental_period, _detect_period_fft
import numpy as np


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_octaves.py <input.mod>")
        sys.exit(1)

    path = Path(sys.argv[1])
    song = parse_mod(str(path))

    # Find which instruments are used
    used = set()
    for pat in song.patterns:
        for row in pat:
            for mn in row:
                if mn.instrument >= 0:
                    used.add(mn.instrument)

    print(f"\n{'Idx':>3s}  {'Name':20s}  {'Type':5s}  {'LoopLen':>7s}  "
          f"{'AC Per':>7s}  {'AC Cyc':>7s}  {'AC Cnf':>7s}  "
          f"{'FFT Per':>7s}  {'FFT Cyc':>7s}  "
          f"{'OctShift':>8s}  {'Action':10s}")
    print("-" * 120)

    for i in sorted(used):
        s = song.samples[i]
        if s.length == 0 or len(s.data) == 0:
            print(f"{i:3d}  {s.name:20s}  (empty)")
            continue

        data = np.array(s.data, dtype=np.uint8).astype(np.float64) - 128.0

        if s.loop_length > 2 and s.loop_start < len(data):
            loop_end = min(s.loop_start + s.loop_length, len(data))
            loop = data[s.loop_start:loop_end]
            tag = "loop"
        else:
            loop = data[:min(len(data), 512)]
            tag = "once"

        if len(loop) < 8:
            print(f"{i:3d}  {s.name:20s}  ({tag}, too short)")
            continue

        ac_period, ac_conf = _detect_fundamental_period(loop)
        fft_period, _ = _detect_period_fft(loop)

        ac_cycles = len(loop) / ac_period if ac_period > 0 else 1.0
        fft_cycles = len(loop) / fft_period if fft_period > 0 else 1.0

        # Fused period selection (mirrors extract_wavetable logic)
        if ac_conf > 0.5 and ac_period >= 8:
            best_period = ac_period
            confidence = ac_conf
        elif fft_period >= 8 and fft_cycles >= 1.4:
            if ac_conf > 0.3 and ac_period >= 8:
                ratio = ac_period / fft_period if fft_period > 0 else 999
                if 0.7 < ratio < 1.3:
                    best_period = ac_period
                    confidence = max(ac_conf, 0.5)
                elif abs(round(ratio) - ratio) < 0.35 and round(ratio) >= 2:
                    best_period = fft_period
                    confidence = 0.5
                elif abs(1/ratio - round(1/ratio)) < 0.35 and round(1/ratio) >= 2:
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
        if cycles > 1.4:
            oct_shift = -round(math.log2(cycles))
        else:
            oct_shift = 0

        action = ""
        if confidence > 0.4 and best_period >= 8 and cycles > 1.4:
            action = "1-cycle"
        elif confidence <= 0.4:
            action = "low-conf"
        else:
            action = "ok"

        print(f"{i:3d}  {s.name:20s}  {tag:5s}  {len(loop):7d}  "
              f"{ac_period:7.1f}  {ac_cycles:7.2f}  {ac_conf:7.3f}  "
              f"{fft_period:7.1f}  {fft_cycles:7.2f}  "
              f"{oct_shift:+8d}  {action:10s}")

    print("\nLegend:")
    print("  AC Per/Cyc/Cnf = Autocorrelation period/cycles/confidence")
    print("  FFT Per/Cyc    = FFT-detected period/cycles")
    print("  OctShift       = -N means N octaves too high without correction")
    print("  Action: 1-cycle = single cycle extracted, ok = ~1 cycle already, low-conf = uncertain")


if __name__ == "__main__":
    main()
