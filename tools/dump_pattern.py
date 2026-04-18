#!/usr/bin/env python3
"""Dump pattern data from a Furnace .fur file with effectMask decoding."""
import zlib, struct, sys
from pathlib import Path

NOTE_NAMES = ['C-','C#','D-','D#','E-','F-','F#','G-','G#','A-','A#','B-']

fname = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / 'Tinytune_new.fur')
target_pat = int(sys.argv[2]) if len(sys.argv) > 2 else 0
max_rows = int(sys.argv[3]) if len(sys.argv) > 3 else 18

with open(fname, 'rb') as f:
    raw = zlib.decompress(f.read())

pats = {}
pos = 0
while pos < len(raw) - 4:
    if raw[pos:pos+4] == b'PATN':
        bsz = struct.unpack_from('<I', raw, pos+4)[0]
        blk = raw[pos+8:pos+8+bsz]
        ch = blk[1]
        pat_id = struct.unpack_from('<H', blk, 2)[0]
        if pat_id == target_pat and ch < 6:
            p = 5
            rows = []; row_idx = 0
            while p < len(blk) and row_idx < 64:
                b = blk[p]; p += 1
                if b == 0xFF: break
                if b & 0x80:
                    skip = (b & 0x7F) + 2
                    for _ in range(skip):
                        rows.append(('...', -1, -1, [])); row_idx += 1
                elif b == 0:
                    rows.append(('...', -1, -1, [])); row_idx += 1
                else:
                    mask = b; effect_mask = 0
                    if mask & 0x20: effect_mask |= blk[p]; p += 1
                    if mask & 0x40: effect_mask |= (blk[p] << 8); p += 1
                    if mask & 0x08: effect_mask |= 1
                    if mask & 0x10: effect_mask |= 2
                    note_str = '...'
                    if mask & 0x01:
                        fn = blk[p]; p += 1
                        if fn == 255: note_str = '...'
                        elif fn == 180: note_str = 'OFF'
                        else:
                            n = fn % 12; o = fn // 12 - 5
                            note_str = '%s%d' % (NOTE_NAMES[n], o)
                    ins = -1
                    if mask & 0x02: ins = blk[p]; p += 1
                    vol = -1
                    if mask & 0x04: vol = blk[p]; p += 1
                    fx_data = [0xFF] * 16
                    for k in range(16):
                        if effect_mask & (1 << k):
                            fx_data[k] = blk[p]; p += 1
                    fxlist = []
                    for i in range(0, 16, 2):
                        c = fx_data[i]; v = fx_data[i+1]
                        if c != 0xFF or v != 0xFF:
                            fxlist.append(('%02X' % c if c != 0xFF else '..', '%02X' % v if v != 0xFF else '..'))
                    rows.append((note_str, ins, vol, fxlist)); row_idx += 1
            pats[ch] = rows
        pos += 8 + bsz
    else:
        pos += 1

# Print
num_ch = max(pats.keys()) + 1 if pats else 4
header = '     '
for ch in range(num_ch):
    header += 'Ch%-2d                       ' % (ch+1)
print(header)

for r in range(min(max_rows, max(len(pats.get(i,[])) for i in range(num_ch)))):
    line = '%3d ' % r
    for ch in range(num_ch):
        rows = pats.get(ch, [])
        if r < len(rows):
            n, ins, vol, fxlist = rows[r]
            ins_s = '%02X' % ins if ins >= 0 else '..'
            vol_s = '%02X' % vol if vol >= 0 else '..'
            fx = ' '.join('%s%s' % (c,v) for c,v in fxlist) if fxlist else '....'
            line += ' %3s %s %s %-18s' % (n, ins_s, vol_s, fx)
        else:
            line += ' ... .. .. ....              '
    print(line)
