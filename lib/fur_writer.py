# lib/fur_writer.py
# Furnace .fur writer -- v232 (INFO format) for Furnace 0.6.8.3
# Derived directly from fur.cpp saveFur() in furnace-0.6.8.3 source.

import struct
import zlib
from typing import List, Dict

DIV_ENGINE_VERSION = 232
DIV_MAX_CHIPS = 32

class FurWriter:
    def __init__(self):
        self.song_name: str = "Untitled"
        self.author: str = "MOD Converter"
        self.chip: int = 0x05          # PC Engine
        self.rows_per_pattern: int = 64
        self.orders: List[int] = []
        self.instruments: List[dict] = []
        self.wavetables: List[List[int]] = []
        self.patterns: Dict[int, Dict[int, List[dict]]] = {}  # ch -> pat_id -> rows
        self.pcm_samples: List[List[int]] = []
        self.speed1: int = 6
        self.speed2: int = 6
        self.hz: float = 50.0          # MOD default: 125 BPM * 2 / 5 = 50 Hz
        self.effect_cols: List[int] = [1] * 6  # effect columns per channel

    def set_song_info(self, name: str, author: str = "MOD Converter"):
        self.song_name = name[:255]
        self.author = author[:255]

    def set_orders(self, orders: List[int]):
        self.orders = orders

    def set_rows_per_pattern(self, rows: int):
        self.rows_per_pattern = rows

    def set_tempo(self, speed: int, bpm: int):
        """Set timing from MOD speed/BPM. MOD tick rate = BPM * 2 / 5."""
        self.speed1 = speed
        self.speed2 = speed
        self.hz = bpm * 2.0 / 5.0

    def add_instrument(self, idx: int, name: str, volume_env: list = None,
                       volume_loop: int = 255, volume_release: int = 255,
                       wavetable_index: int = 0, noise_env: list = None,
                       noise_loop: int = 255,
                       arp_env: list = None, arp_loop: int = 255):
        self.instruments.append({
            "name": name,
            "volume_env": volume_env or [31],
            "volume_loop": volume_loop,
            "volume_release": volume_release,
            "arp_env": arp_env or [0],
            "arp_loop": arp_loop,
            "wave_env": [wavetable_index],
            "wave_loop": 255,
            "noise_env": noise_env,
            "noise_loop": noise_loop,
        })

    def add_wavetable(self, data: List[int]):
        self.wavetables.append([min(31, max(0, x)) for x in data[:32]])

    def set_effect_cols(self, cols: List[int]):
        """Set number of effect columns per channel (1-8)."""
        self.effect_cols = [max(1, min(8, c)) for c in cols]

    def add_pattern(self, channel: int, pattern_id: int, rows: List[dict]):
        if channel not in self.patterns:
            self.patterns[channel] = {}
        self.patterns[channel][pattern_id] = rows

    # --- helpers ---

    def _write_str(self, buf: bytearray, s: str):
        """Null-terminated UTF-8 string (same as Furnace writeString(val, false))."""
        buf.extend(s.encode('utf-8').rstrip(b'\x00'))
        buf.append(0)

    def _pack_into(self, buf: bytearray, fmt: str, *args):
        buf.extend(struct.pack(fmt, *args))

    # --- build ---

    def build(self) -> bytes:
        """Build a v232 .fur file using the INFO block format."""
        num_channels = 6  # PC Engine
        ins_count = len(self.instruments)
        wav_count = len(self.wavetables)
        smp_count = len(self.pcm_samples)
        ord_len = len(self.orders)

        # Collect pattern list (subsong=0, chan, pat_id)
        pats_to_write = []
        for ch in range(num_channels):
            for pat_id in sorted(self.patterns.get(ch, {}).keys()):
                pats_to_write.append((0, ch, pat_id))
        num_pats = len(pats_to_write)

        # Two-pass: write everything with placeholder pointers,
        # then seek back and fill in real offsets.

        raw = bytearray()

        # -- HEADER (32 bytes) --
        raw.extend(b"-Furnace module-")
        self._pack_into(raw, '<H', DIV_ENGINE_VERSION)
        self._pack_into(raw, '<H', 0)             # reserved
        self._pack_into(raw, '<I', 32)             # song info pointer
        raw.extend(b'\x00' * 8)                    # reserved

        # -- INFO block --
        raw.extend(b"INFO")
        info_size_pos = len(raw)
        self._pack_into(raw, '<I', 0)             # placeholder for block size
        info_data_start = len(raw)

        # Song timing data (matches saveFur order exactly)
        raw.append(0)                              # timeBase
        raw.append(self.speed1)                    # speed1
        raw.append(self.speed2)                    # speed2
        raw.append(1)                              # arpLen
        self._pack_into(raw, '<f', self.hz)        # hz
        self._pack_into(raw, '<H', self.rows_per_pattern)
        self._pack_into(raw, '<H', ord_len)
        raw.append(4)                              # hilightA (4 rows/beat, MOD standard)
        raw.append(16)                             # hilightB
        self._pack_into(raw, '<H', ins_count)
        self._pack_into(raw, '<H', wav_count)
        self._pack_into(raw, '<H', smp_count)
        self._pack_into(raw, '<I', num_pats)

        # Chip IDs (32 bytes)
        raw.append(self.chip)
        raw.extend(b'\x00' * (DIV_MAX_CHIPS - 1))

        # Chip volumes (32 signed bytes, 64=1.0)
        raw.append(64)
        raw.extend(b'\x00' * (DIV_MAX_CHIPS - 1))

        # Chip panning (32 signed bytes, 0=center)
        raw.extend(b'\x00' * DIV_MAX_CHIPS)

        # Chip flag pointers (32 x u32 = 128 bytes)
        raw.extend(b'\x00' * (DIV_MAX_CHIPS * 4))

        # Song name and author
        self._write_str(raw, self.song_name)
        self._write_str(raw, self.author)

        # Tuning (>=33)
        self._pack_into(raw, '<f', 440.0)

        # Compatibility flags (20 bytes)
        raw.append(0)   # limitSlides
        raw.append(2)   # linearPitch (2=full linear, >=94)
        raw.append(0)   # loopModality
        raw.append(1)   # properNoiseLayout
        raw.append(0)   # waveDutyIsVol
        raw.append(0)   # resetMacroOnPorta
        raw.append(0)   # legacyVolumeSlides
        raw.append(0)   # compatibleArpeggio
        raw.append(0)   # noteOffResetsSlides
        raw.append(0)   # targetResetsSlides
        raw.append(0)   # arpNonPorta
        raw.append(0)   # algMacroBehavior
        raw.append(0)   # brokenShortcutSlides
        raw.append(0)   # ignoreDuplicateSlides
        raw.append(0)   # stopPortaOnNoteOff
        raw.append(0)   # continuousVibrato
        raw.append(0)   # brokenDACMode
        raw.append(0)   # oneTickCut
        raw.append(0)   # newInsTriggersInPorta
        raw.append(0)   # arp0Reset

        # Pointer placeholders (filled in pass 2)
        ins_ptr_pos = len(raw)
        raw.extend(b'\x00' * (ins_count * 4))
        wav_ptr_pos = len(raw)
        raw.extend(b'\x00' * (wav_count * 4))
        smp_ptr_pos = len(raw)
        raw.extend(b'\x00' * (smp_count * 4))
        pat_ptr_pos = len(raw)
        raw.extend(b'\x00' * (num_pats * 4))

        # Orders -- CHANNEL-major (outer=channel, inner=order position)
        for ch in range(num_channels):
            for order in self.orders:
                raw.append(order)

        # Effect columns per channel
        for ch in range(num_channels):
            raw.append(self.effect_cols[ch] if ch < len(self.effect_cols) else 1)

        # Channel show/collapse/names/shortNames (>=39)
        for ch in range(num_channels):
            raw.append(1)                          # chanShow
        for ch in range(num_channels):
            raw.append(0)                          # collapse
        for ch in range(num_channels):
            raw.append(0)                          # chanName (empty STR)
        for ch in range(num_channels):
            raw.append(0)                          # chanShortName (empty STR)

        # Song comment (>=39)
        raw.append(0)

        # Master volume (>=59)
        self._pack_into(raw, '<f', 1.0)

        # Extended compat flags (>=70): 28 bytes
        raw.append(0)   # brokenSpeedSel
        raw.append(0)   # noSlidesOnFirstTick
        raw.append(0)   # rowResetsArpPos
        raw.append(0)   # ignoreJumpAtEnd
        raw.append(0)   # buggyPortaAfterSlide
        raw.append(0)   # gbInsAffectsEnvelope
        raw.append(0)   # sharedExtStat
        raw.append(0)   # ignoreDACModeOutsideIntendedChannel
        raw.append(0)   # e1e2AlsoTakePriority
        raw.append(1)   # newSegaPCM
        raw.append(0)   # fbPortaPause
        raw.append(0)   # snDutyReset
        raw.append(1)   # pitchMacroIsLinear
        raw.append(0)   # pitchSlideSpeed
        raw.append(0)   # oldOctaveBoundary
        raw.append(0)   # noOPN2Vol
        raw.append(1)   # newVolumeScaling
        raw.append(0)   # volMacroLinger
        raw.append(0)   # brokenOutVol
        raw.append(0)   # e1e2StopOnSameNote
        raw.append(0)   # brokenPortaArp
        raw.append(0)   # snNoLowPeriods
        raw.append(0)   # delayBehavior
        raw.append(0)   # jumpTreatment
        raw.append(0)   # autoSystem
        raw.append(0)   # disableSampleMacro
        raw.append(0)   # brokenOutVol2
        raw.append(0)   # oldArpStrategy

        # Virtual tempo of first song (>=96)
        self._pack_into(raw, '<H', 150)
        self._pack_into(raw, '<H', 150)

        # Subsong list (>=95)
        self._write_str(raw, "")                   # first subsong name
        self._write_str(raw, "")                   # first subsong comment
        raw.append(0)                              # additional subsongs count
        raw.extend(b'\x00' * 3)                    # reserved

        # Additional metadata (>=103)
        self._write_str(raw, "")                   # systemName
        self._write_str(raw, "")                   # category
        self._write_str(raw, "")                   # nameJ
        self._write_str(raw, "")                   # authorJ
        self._write_str(raw, "")                   # systemNameJ
        self._write_str(raw, "")                   # categoryJ

        # System output config (>=135)
        self._pack_into(raw, '<f', 1.0)            # chip volume
        self._pack_into(raw, '<f', 0.0)            # chip panning
        self._pack_into(raw, '<f', 0.0)            # chip balance

        # Patchbay (>=135)
        self._pack_into(raw, '<I', 0)              # connection count
        raw.append(1)                              # auto patchbay (>=136)

        # More compat flags (>=138)
        raw.append(0)   # brokenPortaLegato
        raw.append(0)   # brokenFMOff
        raw.append(0)   # preNoteNoEffect
        raw.append(0)   # oldDPCM
        raw.append(0)   # resetArpPhaseOnNewNote
        raw.append(0)   # ceilVolumeScaling
        raw.append(0)   # oldAlwaysSetVolume
        raw.append(0)   # oldSampleOffset

        # Speed pattern of first song (>=139)
        raw.append(1)                              # length
        raw.append(self.speed1)                    # speed[0]
        raw.extend(b'\x00' * 15)                   # speed[1..15]

        # Groove list (>=139)
        raw.append(0)                              # no grooves

        # Asset directory pointers (>=156) -- placeholder
        adir_ptr_pos = len(raw)
        raw.extend(b'\x00' * 12)                   # 3 x u32

        # -- Patch INFO block size --
        info_size = len(raw) - info_data_start
        struct.pack_into('<I', raw, info_size_pos, info_size)

        # -- ADIR blocks (3 required: instruments, wavetables, samples) --
        adir_offsets = []
        for _ in range(3):
            adir_offsets.append(len(raw))
            raw.extend(b"ADIR")
            raw.extend(struct.pack('<I', 4))       # block size = 4
            raw.extend(struct.pack('<I', 0))       # 0 directories
        for i, off in enumerate(adir_offsets):
            struct.pack_into('<I', raw, adir_ptr_pos + i * 4, off)

        # -- INS2 blocks --
        ins_offsets = []
        for inst in self.instruments:
            ins_offsets.append(len(raw))
            raw.extend(self._build_ins2(inst))
        for i, off in enumerate(ins_offsets):
            struct.pack_into('<I', raw, ins_ptr_pos + i * 4, off)

        # -- WAVE blocks --
        wav_offsets = []
        for idx, wt in enumerate(self.wavetables):
            wav_offsets.append(len(raw))
            raw.extend(self._build_wave(idx, wt))
        for i, off in enumerate(wav_offsets):
            struct.pack_into('<I', raw, wav_ptr_pos + i * 4, off)

        # -- PATN blocks (v232: 1-byte channel) --
        pat_offsets = []
        for subsong, ch, pat_id in pats_to_write:
            rows = self.patterns[ch][pat_id]
            pat_offsets.append(len(raw))
            raw.extend(self._build_patn_v232(subsong, ch, pat_id, rows))
        for i, off in enumerate(pat_offsets):
            struct.pack_into('<I', raw, pat_ptr_pos + i * 4, off)

        return zlib.compress(bytes(raw), level=9)

    def _build_ins2(self, inst: dict) -> bytes:
        """INS2 -- Feature-based instrument (NA/MA/EN codes)."""
        data = bytearray()
        data.extend(struct.pack('<H', DIV_ENGINE_VERSION))
        data.extend(struct.pack('<H', 5))                   # type = PCE wavetable

        # Feature: NA (name)
        name_b = inst["name"].encode('utf-8').rstrip(b'\x00') + b'\x00'
        data.extend(b'NA')
        data.extend(struct.pack('<H', len(name_b)))
        data.extend(name_b)

        # Feature: MA (macros)
        ma = bytearray()
        ma.extend(struct.pack('<H', 8))                     # macroHeaderLen

        # Volume macro (code=0)
        vol = inst.get("volume_env", [15])
        vol_loop = inst.get("volume_loop", 255)
        vol_release = inst.get("volume_release", 255)
        ma.append(0)
        ma.append(len(vol))
        ma.append(vol_loop if vol_loop < len(vol) else 255)
        ma.append(vol_release if vol_release < len(vol) else 255)
        ma.append(0); ma.append(0)
        ma.append(0); ma.append(1)
        ma.extend(bytes(vol))

        # Arpeggio macro (code=1)
        arp = inst.get("arp_env", [0])
        arp_loop = inst.get("arp_loop", 255)
        ma.append(1)
        ma.append(len(arp))
        ma.append(arp_loop if arp_loop < len(arp) else 255)
        ma.append(255); ma.append(0); ma.append(0)
        ma.append(0); ma.append(1)
        ma.extend(bytes(arp))

        # Wave macro (code=3)
        wav = inst.get("wave_env", [0])
        wav_loop = inst.get("wave_loop", 255)
        ma.append(3)
        ma.append(len(wav))
        ma.append(wav_loop if wav_loop < len(wav) else 255)
        ma.append(255); ma.append(0); ma.append(0)
        ma.append(0); ma.append(1)
        ma.extend(bytes(wav))

        # Noise/EX1 macro (code=5) -- only if present
        noise = inst.get("noise_env")
        if noise:
            noise_loop = inst.get("noise_loop", 255)
            ma.append(5)
            ma.append(len(noise))
            ma.append(noise_loop if noise_loop < len(noise) else 255)
            ma.append(255); ma.append(0); ma.append(0)
            ma.append(0); ma.append(1)
            ma.extend(bytes(noise))

        ma.append(255)                                       # macro list end

        data.extend(b'MA')
        data.extend(struct.pack('<H', len(ma)))
        data.extend(ma)

        # Feature: EN (end)
        data.extend(b'EN')
        data.extend(struct.pack('<H', 0))

        block = b"INS2" + struct.pack('<I', len(data))
        return block + bytes(data)

    def _build_wave(self, idx: int, data: List[int]) -> bytes:
        """WAVE block."""
        buf = bytearray()
        name = f"Wave{idx}".encode('utf-8')
        buf.extend(name); buf.append(0)
        buf.extend(struct.pack('<I', len(data)))             # width
        buf.extend(b'\x00\x00\x00\x00')                     # reserved
        buf.extend(struct.pack('<I', 31))                    # height (PCE 5-bit)
        for val in data:
            buf.extend(struct.pack('<I', val))

        block = b"WAVE" + struct.pack('<I', len(buf))
        return block + bytes(buf)

    def _build_patn_v232(self, subsong: int, channel: int,
                         pattern_id: int, rows: List[dict]) -> bytes:
        """PATN block for v232 (1-byte channel, compressed row encoding).
        Matches C++ saveFur newPatternFormat=true path with effectMask."""
        data = bytearray()
        data.append(subsong)                                 # u8 subsong
        data.append(channel)                                 # u8 channel (<240)
        data.extend(struct.pack('<H', pattern_id))           # u16 patIndex
        data.append(0)                                       # STR patternName (empty)

        empty_rows = 0
        for row in rows:
            note_val = row.get("note", 0)
            octave = row.get("octave", 0)
            instr = row.get("instrument", -1)
            vol = row.get("volume", -1)
            effects = row.get("effects", [])

            # Compute finalNote (matching C++ logic)
            if note_val == 100:
                final_note = 180   # note off
            elif note_val == 101:
                final_note = 181   # note release
            elif note_val == 102:
                final_note = 182   # macro release
            elif note_val == 0:
                final_note = 255   # empty
            else:
                seek = ((note_val - 1) + octave * 12) + 60
                final_note = seek if 0 <= seek < 180 else 255

            # Build mask and effectMask
            mask = 0
            effect_mask = 0  # 16-bit: pairs of bits per effect column

            if final_note != 255:
                mask |= 0x01
            if instr != -1:
                mask |= 0x02
            if vol != -1:
                mask |= 0x04

            # Parse effects into (cmd, val) pairs
            fx_pairs = []
            for fx_word in effects[:8]:
                fx_pairs.append((fx_word & 0xFF, (fx_word >> 8) & 0xFF))

            # Build effectMask and set mask flags
            for i, (cmd, val) in enumerate(fx_pairs):
                k = i * 2
                effect_mask |= (1 << k) | (1 << (k + 1))
                if i == 0:
                    mask |= 0x08 | 0x10        # fx0 in main mask bits
                elif i <= 3:
                    mask |= 0x20               # effectMask low byte needed
                else:
                    mask |= 0x40               # effectMask high byte needed

            if mask == 0:
                empty_rows += 1
                if empty_rows > 127:
                    data.append(0x80 | (empty_rows - 2))
                    empty_rows = 0
            else:
                if empty_rows > 1:
                    data.append(0x80 | (empty_rows - 2))
                    empty_rows = 0
                elif empty_rows == 1:
                    data.append(0)
                    empty_rows = 0

                data.append(mask)

                # effectMask bytes (before note/ins/vol/effects)
                if mask & 0x20:
                    data.append(effect_mask & 0xFF)
                if mask & 0x40:
                    data.append((effect_mask >> 8) & 0xFF)

                if mask & 0x01:
                    data.append(final_note)
                if mask & 0x02:
                    data.append(instr & 0xFF)
                if mask & 0x04:
                    data.append(vol & 0xFF)

                # Effect data in effectMask bit order
                for i, (cmd, val) in enumerate(fx_pairs):
                    k = i * 2
                    if effect_mask & (1 << k):
                        data.append(cmd & 0xFF)
                    if effect_mask & (1 << (k + 1)):
                        data.append(val & 0xFF)

        if empty_rows > 1:
            data.append(0x80 | (empty_rows - 2))
        elif empty_rows == 1:
            data.append(0)

        data.append(0xFF)                                    # end-of-pattern

        block = b"PATN" + struct.pack('<I', len(data))
        return block + bytes(data)

    def save(self, filepath: str):
        data = self.build()
        with open(filepath, "wb") as f:
            f.write(data)
        print(f"[ok] Saved Furnace file: {filepath} ({len(data):,} bytes)")