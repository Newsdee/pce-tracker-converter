#!/usr/bin/env python3
"""Dump all wavetables from a MOD file to inspect quality."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.mod_parser import parse_mod
from lib.sample_processor import extract_wavetable, classify_sample

song = parse_mod(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / 'Tinytune.mod'))
for i, s in enumerate(song.samples):
    wt_info = extract_wavetable(s)
    wt = wt_info["wavetable"]
    cls = classify_sample(s)
    mn, mx = min(wt), max(wt)
    flat = "FLAT" if mn == mx else "    "
    oct_s = f"oct={wt_info['octave_shift']:+d}" if wt_info['octave_shift'] != 0 else ""
    print(f"  WT {i:2d}: [{cls:12s}] len={s.length:5d} loop={s.loop_length:5d}  range={mn:2d}-{mx:2d} {flat}  {s.name} {oct_s}")
    if mn != mx:
        print(f"         {wt}")
