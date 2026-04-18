#!/usr/bin/env python3
"""Verify v232 INFO .fur file structure against Furnace 0.6.8.3 expectations."""
import zlib, struct, sys
from pathlib import Path

fname = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / 'Tinytune.fur')
with open(fname, 'rb') as f:
    raw = zlib.decompress(f.read())

print(f"Total decompressed: {len(raw)} bytes")
print(f"Magic: {raw[0:16]}")
ver = struct.unpack_from('<H', raw, 16)[0]
print(f"Version: {ver}")
song_ptr = struct.unpack_from('<I', raw, 20)[0]
print(f"songInfoPtr: {song_ptr}")

# Check block at songInfoPtr
blk_id = raw[song_ptr:song_ptr+4].decode('ascii', errors='replace')
blk_size = struct.unpack_from('<I', raw, song_ptr+4)[0]
print(f"Block at ptr {song_ptr}: '{blk_id}', size={blk_size}")

if blk_id != "INFO":
    print(f"FAIL: expected INFO, got '{blk_id}'")
    sys.exit(1)
print("OK: INFO block found at songInfoPtr")

# Parse INFO fields
p = song_ptr + 8
timeBase = raw[p]; p += 1
speed1 = raw[p]; p += 1
speed2 = raw[p]; p += 1
arpLen = raw[p]; p += 1
hz = struct.unpack_from('<f', raw, p)[0]; p += 4
patLen = struct.unpack_from('<H', raw, p)[0]; p += 2
ordLen = struct.unpack_from('<H', raw, p)[0]; p += 2
hlA = raw[p]; p += 1
hlB = raw[p]; p += 1
insLen = struct.unpack_from('<H', raw, p)[0]; p += 2
waveLen = struct.unpack_from('<H', raw, p)[0]; p += 2
smpLen = struct.unpack_from('<H', raw, p)[0]; p += 2
numPats = struct.unpack_from('<I', raw, p)[0]; p += 4

print(f"\nINFO: hz={hz}, patLen={patLen}, ordLen={ordLen}")
print(f"  ins={insLen}, wave={waveLen}, smp={smpLen}, pats={numPats}")

# Skip chip IDs (32), volumes (32), panning (32), flag ptrs (128) = 224 bytes
p += 224

# Read strings
def read_str(data, pos):
    end = data.index(0, pos)
    return data[pos:end].decode('utf-8', errors='replace'), end + 1

name, p = read_str(raw, p)
author, p = read_str(raw, p)
print(f"  name='{name}', author='{author}'")

# Skip tuning (4) + compat flags (20) = 24
p += 24

# Read pointer arrays
ins_ptrs = [struct.unpack_from('<I', raw, p + i*4)[0] for i in range(insLen)]
p += insLen * 4
wav_ptrs = [struct.unpack_from('<I', raw, p + i*4)[0] for i in range(waveLen)]
p += waveLen * 4
smp_ptrs = [struct.unpack_from('<I', raw, p + i*4)[0] for i in range(smpLen)]
p += smpLen * 4
pat_ptrs = [struct.unpack_from('<I', raw, p + i*4)[0] for i in range(numPats)]
p += numPats * 4

# Verify instrument pointers
print(f"\nInstrument pointers ({insLen}):")
for i, ptr in enumerate(ins_ptrs[:3]):
    bid = raw[ptr:ptr+4].decode('ascii', errors='replace')
    print(f"  [{i}] offset={ptr} -> '{bid}'")
if insLen > 3:
    print(f"  ... [{insLen-1}] offset={ins_ptrs[-1]} -> '{raw[ins_ptrs[-1]:ins_ptrs[-1]+4].decode('ascii',errors='replace')}'")

# Verify pattern pointers
print(f"\nPattern pointers ({numPats}):")
for i, ptr in enumerate(pat_ptrs[:3]):
    bid = raw[ptr:ptr+4].decode('ascii', errors='replace')
    print(f"  [{i}] offset={ptr} -> '{bid}'")
if numPats > 3:
    print(f"  ... [{numPats-1}] offset={pat_ptrs[-1]} -> '{raw[pat_ptrs[-1]:pat_ptrs[-1]+4].decode('ascii',errors='replace')}'")

# Check ADIR blocks exist
# Scan past the rest of INFO to find ADIR
info_end = song_ptr + 8 + blk_size
print(f"\nINFO block ends at offset {info_end}")
adir_id = raw[info_end:info_end+4].decode('ascii', errors='replace')
print(f"Next block: '{adir_id}'")

# Verify all pointers resolve to expected block types
errors = 0
for i, ptr in enumerate(ins_ptrs):
    bid = raw[ptr:ptr+4]
    if bid != b'INS2':
        print(f"  ERROR: ins[{i}] ptr {ptr} -> {bid} (expected INS2)")
        errors += 1
for i, ptr in enumerate(wav_ptrs):
    bid = raw[ptr:ptr+4]
    if bid != b'WAVE':
        print(f"  ERROR: wav[{i}] ptr {ptr} -> {bid} (expected WAVE)")
        errors += 1
for i, ptr in enumerate(pat_ptrs):
    bid = raw[ptr:ptr+4]
    if bid != b'PATN':
        print(f"  ERROR: pat[{i}] ptr {ptr} -> {bid} (expected PATN)")
        errors += 1

if errors == 0:
    print("\n=== ALL CHECKS PASSED ===")
else:
    print(f"\n=== {errors} ERRORS ===")

