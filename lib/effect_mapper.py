# lib/effect_mapper.py
# MOD Effect → Furnace Effect Mapper
# Effect IDs derived from furnace-0.6.8.3 src/engine/fileOps/mod.cpp

from dataclasses import dataclass
from typing import List, Optional

@dataclass
class FurnaceEffect:
    command: int = 0
    value: int = 0


def map_mod_effect(effect: int, param: int) -> List[FurnaceEffect]:
    """Convert MOD effect to Furnace effect(s).

    Returns a list (0-2 effects). Combined effects (0x05, 0x06) split
    into two Furnace effects.  Unmapped effects return empty list.
    """
    if effect == 0x00:  # Arpeggio (only when param != 0)
        if param:
            return [FurnaceEffect(0x00, param)]
        return []
    elif effect == 0x01:  # Portamento Up
        return [FurnaceEffect(0x01, param)]
    elif effect == 0x02:  # Portamento Down
        return [FurnaceEffect(0x02, param)]
    elif effect == 0x03:  # Tone Portamento
        return [FurnaceEffect(0x03, param)]
    elif effect == 0x04:  # Vibrato
        return [FurnaceEffect(0x04, param)]
    elif effect == 0x05:  # Tone Porta + Volume Slide → split into 2 columns
        return [FurnaceEffect(0x03, 0x00),         # continue porta
                FurnaceEffect(0xFA, param)]         # volume slide
    elif effect == 0x06:  # Vibrato + Volume Slide → split into 2 columns
        return [FurnaceEffect(0x04, 0x00),         # continue vibrato
                FurnaceEffect(0xFA, param)]         # volume slide
    elif effect == 0x07:  # Tremolo
        return [FurnaceEffect(0x07, param)]
    elif effect == 0x09:  # Sample Offset
        return [FurnaceEffect(0x91, param)]
    elif effect == 0x0A:  # Volume Slide → Furnace 0xFA
        return [FurnaceEffect(0xFA, param)]
    elif effect == 0x0B:  # Jump to Pattern
        return [FurnaceEffect(0x0B, param)]
    elif effect == 0x0C:  # Set Volume — handled via volume column in convert_mod.py
        return []
    elif effect == 0x0D:  # Break to Row (BCD param → decimal)
        row = (param >> 4) * 10 + (param & 0x0F)
        return [FurnaceEffect(0x0D, row)]
    elif effect == 0x0F:  # Set Speed / Tempo
        if param <= 0x20:
            return [FurnaceEffect(0x0F, param)]    # speed
        else:
            return [FurnaceEffect(0xF0, param)]    # tempo (BPM)

    # Extended effects (0x0E)
    elif effect == 0x0E:
        ext = (param >> 4) & 0x0F
        val = param & 0x0F
        if ext == 0x00:   # Set Filter (Amiga) → Furnace 0x10, inverted
            return [FurnaceEffect(0x10, 0 if val else 1)]
        elif ext == 0x01: # Fine Porta Up → Furnace 0xF1
            return [FurnaceEffect(0xF1, val)]
        elif ext == 0x02: # Fine Porta Down → Furnace 0xF2
            return [FurnaceEffect(0xF2, val)]
        elif ext == 0x09: # Retrigger → Furnace 0x0C
            return [FurnaceEffect(0x0C, val)]
        elif ext == 0x0A: # Fine Volume Up → Furnace 0xF8
            return [FurnaceEffect(0xF8, val)]
        elif ext == 0x0B: # Fine Volume Down → Furnace 0xF9
            return [FurnaceEffect(0xF9, val)]
        elif ext == 0x0C: # Note Cut → Furnace 0xEC
            return [FurnaceEffect(0xEC, val)]
        elif ext == 0x0D: # Note Delay → Furnace 0xED
            return [FurnaceEffect(0xED, val)]
        # E3-E8 (glissando, vib waveform, finetune, loop, trem waveform)
        # are not mapped by Furnace's MOD import — silently dropped.

    return []


def get_furnace_effects(mod_note) -> List[int]:
    """Returns list of Furnace effect words (cmd | (value << 8)), up to 2."""
    mapped = map_mod_effect(mod_note.effect, mod_note.effect_arg)
    return [fx.command | (fx.value << 8) for fx in mapped]