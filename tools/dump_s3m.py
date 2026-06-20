#!/usr/bin/env python3
"""Dump S3M file header, instruments, and pattern stats."""
import struct, sys

def dump_s3m(path):
    with open(path, 'rb') as f:
        data = f.read()

    print(f'File size: {len(data)} bytes')

    # S3M header (96 bytes)
    name = data[0:28].split(b'\x00')[0].decode('ascii', errors='replace')
    sig = data[28]
    ord_count = struct.unpack_from('<H', data, 32)[0]
    ins_count = struct.unpack_from('<H', data, 34)[0]
    pat_count = struct.unpack_from('<H', data, 36)[0]
    flags = struct.unpack_from('<H', data, 38)[0]
    tracker_ver = struct.unpack_from('<H', data, 40)[0]
    sample_fmt = struct.unpack_from('<H', data, 42)[0]
    magic = data[44:48]
    global_vol = data[48]
    initial_speed = data[49]
    initial_tempo = data[50]
    master_vol = data[51]
    default_pan = data[53]

    chan_settings = list(data[64:96])
    active_channels = [i for i, c in enumerate(chan_settings) if c < 255]

    print(f'Name: "{name}"')
    print(f'Magic: {magic}')
    print(f'Tracker ver: 0x{tracker_ver:04X}, Sample fmt: {sample_fmt}')
    print(f'Orders: {ord_count}, Instruments: {ins_count}, Patterns: {pat_count}')
    print(f'Global vol: {global_vol}, Speed: {initial_speed}, Tempo: {initial_tempo}')
    print(f'Active channels ({len(active_channels)}): {active_channels}')
    print(f'Chan settings: {chan_settings[:16]}')

    orders = list(data[96:96 + ord_count])
    real_orders = [o for o in orders if o < 254]
    print(f'Orders ({len(real_orders)} real): {orders}')

    ins_ptrs_off = 96 + ord_count
    ins_ptrs = [struct.unpack_from('<H', data, ins_ptrs_off + i * 2)[0]
                for i in range(ins_count)]

    pat_ptrs_off = ins_ptrs_off + ins_count * 2
    pat_ptrs = [struct.unpack_from('<H', data, pat_ptrs_off + i * 2)[0]
                for i in range(pat_count)]

    # Instruments
    print()
    print('=== INSTRUMENTS ===')
    for i in range(ins_count):
        off = ins_ptrs[i] * 16
        if off == 0:
            continue
        itype = data[off]
        iname = data[off + 48:off + 76].split(b'\x00')[0].decode('ascii', errors='replace')
        if itype == 1:
            length = struct.unpack_from('<I', data, off + 16)[0]
            loop_start = struct.unpack_from('<I', data, off + 20)[0]
            loop_end = struct.unpack_from('<I', data, off + 24)[0]
            vol = data[off + 28]
            flags_s = data[off + 31]
            c2spd = struct.unpack_from('<I', data, off + 32)[0]
            lp = 'loop' if flags_s & 1 else 'once'
            bits = '16b' if flags_s & 4 else '8b'
            print(f'  Ins {i:2d}: [{lp},{bits}] vol={vol:2d} len={length:6d} '
                  f'c2spd={c2spd:5d} "{iname}"')
        elif itype == 0:
            if iname:
                print(f'  Ins {i:2d}: [empty] "{iname}"')
        else:
            print(f'  Ins {i:2d}: [type={itype}] "{iname}"')

    # Pattern channel usage analysis
    print()
    print('=== PATTERN CHANNEL USAGE ===')
    num_channels = len(active_channels)
    for pi in range(pat_count):
        if pat_ptrs[pi] == 0:
            print(f'  Pat {pi:2d}: [empty]')
            continue
        off = pat_ptrs[pi] * 16
        packed_len = struct.unpack_from('<H', data, off)[0]
        pdata = data[off + 2:off + 2 + packed_len]

        ch_notes = [0] * 32  # note count per channel
        ch_effects = {}      # effects seen per channel
        row = 0
        pos = 0
        while pos < len(pdata) and row < 64:
            what = pdata[pos]; pos += 1
            if what == 0:
                row += 1
                continue
            ch = what & 31
            if what & 32:  # note + instrument
                note = pdata[pos]; ins = pdata[pos + 1]; pos += 2
                if note < 255:
                    ch_notes[ch] += 1
            if what & 64:  # volume
                pos += 1
            if what & 128:  # effect
                cmd = pdata[pos]; arg = pdata[pos + 1]; pos += 2
                if cmd > 0:
                    ch_effects.setdefault(ch, set()).add(cmd)

        used = [c for c in range(32) if ch_notes[c] > 0 or c in ch_effects]
        fx_str = ', '.join(f'ch{c}:{sorted(ch_effects.get(c, []))}' for c in used if c in ch_effects)
        note_str = ', '.join(f'ch{c}:{ch_notes[c]}' for c in used)
        print(f'  Pat {pi:2d}: {len(used)} ch used [{note_str}] fx:[{fx_str}]')

    # Effect summary across all patterns
    print()
    print('=== EFFECT SUMMARY ===')
    all_fx = {}
    for pi in range(pat_count):
        if pat_ptrs[pi] == 0:
            continue
        off = pat_ptrs[pi] * 16
        packed_len = struct.unpack_from('<H', data, off)[0]
        pdata = data[off + 2:off + 2 + packed_len]
        pos = 0; row = 0
        while pos < len(pdata) and row < 64:
            what = pdata[pos]; pos += 1
            if what == 0:
                row += 1; continue
            if what & 32: pos += 2
            if what & 64: pos += 1
            if what & 128:
                cmd = pdata[pos]; arg = pdata[pos + 1]; pos += 2
                if cmd > 0:
                    letter = chr(ord('A') - 1 + cmd) if cmd < 27 else f'?{cmd}'
                    all_fx[letter] = all_fx.get(letter, 0) + 1
    for fx in sorted(all_fx.keys()):
        print(f'  {fx}: {all_fx[fx]} uses')

if __name__ == '__main__':
    dump_s3m(sys.argv[1] if len(sys.argv) > 1
             else 'examples/SatteliteOne/SATELL.S3M')
