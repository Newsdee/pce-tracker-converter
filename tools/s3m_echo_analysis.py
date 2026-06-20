#!/usr/bin/env python3
"""Analyze channel similarity in S3M patterns for echo detection."""
import struct, sys

def parse_s3m_patterns(path):
    with open(path, 'rb') as f:
        data = f.read()

    ord_count = struct.unpack_from('<H', data, 32)[0]
    ins_count = struct.unpack_from('<H', data, 34)[0]
    pat_count = struct.unpack_from('<H', data, 36)[0]

    orders = list(data[96:96 + ord_count])

    ins_ptrs_off = 96 + ord_count
    pat_ptrs_off = ins_ptrs_off + ins_count * 2
    pat_ptrs = [struct.unpack_from('<H', data, pat_ptrs_off + i * 2)[0]
                for i in range(pat_count)]

    # Parse all patterns into [pat_id][row][channel] = (note, ins, vol, cmd, arg)
    patterns = {}
    for pi in range(pat_count):
        if pat_ptrs[pi] == 0:
            continue
        off = pat_ptrs[pi] * 16
        packed_len = struct.unpack_from('<H', data, off)[0]
        pdata = data[off + 2:off + 2 + packed_len]

        rows = [[None] * 32 for _ in range(64)]
        row = 0; pos = 0
        while pos < len(pdata) and row < 64:
            what = pdata[pos]; pos += 1
            if what == 0:
                row += 1; continue
            ch = what & 31
            note = ins = vol = cmd = arg = -1
            if what & 32:
                note = pdata[pos]; ins = pdata[pos + 1]; pos += 2
            if what & 64:
                vol = pdata[pos]; pos += 1
            if what & 128:
                cmd = pdata[pos]; arg = pdata[pos + 1]; pos += 2
            rows[row][ch] = (note, ins, vol, cmd, arg)
        patterns[pi] = rows

    return orders, patterns

def compare_channels(patterns, orders, ch_a, ch_b):
    """Compare two channels across all ordered patterns, looking for echo."""
    print(f'\n=== Comparing ch{ch_a} vs ch{ch_b} ===')

    for oi, pat_id in enumerate(orders):
        if pat_id >= 254:
            continue
        if pat_id not in patterns:
            continue
        rows = patterns[pat_id]

        # Extract note sequences (ignoring empty rows)
        notes_a = [(r, rows[r][ch_a]) for r in range(64) if rows[r][ch_a] and rows[r][ch_a][0] > 0 and rows[r][ch_a][0] < 254]
        notes_b = [(r, rows[r][ch_b]) for r in range(64) if rows[r][ch_b] and rows[r][ch_b][0] > 0 and rows[r][ch_b][0] < 254]

        if not notes_a and not notes_b:
            continue

        # Check for row offset (echo delay)
        best_offset = 0
        best_match = 0
        for offset in range(0, 8):
            matches = 0
            total = max(len(notes_a), 1)
            for ra, da in notes_a:
                for rb, db in notes_b:
                    if rb == ra + offset and da[0] == db[0] and da[1] == db[1]:
                        matches += 1
                        break
            if matches > best_match:
                best_match = matches
                best_offset = offset

        # Volume comparison
        vols_a = [rows[r][ch_a][2] for r in range(64) if rows[r][ch_a] and rows[r][ch_a][2] >= 0]
        vols_b = [rows[r][ch_b][2] for r in range(64) if rows[r][ch_b] and rows[r][ch_b][2] >= 0]
        avg_a = sum(vols_a) / len(vols_a) if vols_a else -1
        avg_b = sum(vols_b) / len(vols_b) if vols_b else -1

        pct = (best_match * 100 // max(len(notes_a), 1)) if notes_a else 0
        print(f'  Ord {oi:2d} (pat {pat_id:2d}): '
              f'ch{ch_a}={len(notes_a):2d} notes, ch{ch_b}={len(notes_b):2d} notes, '
              f'match={best_match}/{len(notes_a)} ({pct}%) offset={best_offset} '
              f'avgVol={avg_a:.0f}/{avg_b:.0f}')

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'examples/SatteliteOne/SATELL.S3M'
    orders, patterns = parse_s3m_patterns(path)

    # Compare likely echo pairs (ch4/ch6, ch5/ch7, ch0/ch2, ch1/ch3)
    for a, b in [(4, 6), (5, 7), (0, 2), (1, 3), (0, 1), (4, 5), (6, 7)]:
        compare_channels(patterns, orders, a, b)

if __name__ == '__main__':
    main()
