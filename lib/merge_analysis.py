"""Channel merge analysis: scoring, plans, and best-plan selection.

Core functions used by both `tools/merge_analysis.py` (standalone report)
and `convert_mod.py` (--merge_channels=auto / analyze).
"""
import itertools
from mod_parser import ModSong, ModNote


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_active(note: ModNote) -> bool:
    """True if the row slot has any audible content."""
    return (note.note > 0 or note.instrument >= 0
            or (note.effect > 0 and note.effect_arg > 0)
            or note.xm_volume >= 0)


def _has_note(note: ModNote) -> bool:
    """True if the row has an actual pitched note (not just an effect)."""
    return note.note > 0 and note.note < 254


# ---------------------------------------------------------------------------
# 1. Channel activity profile
# ---------------------------------------------------------------------------

def channel_activity(song: ModSong):
    """Returns per-channel stats across all ordered patterns."""
    n_ch = song.channels
    stats = []
    for ch in range(n_ch):
        total_rows = 0
        note_rows = 0
        active_rows = 0
        instruments_seen = set()
        effects_seen = {}
        vol_events = 0

        for pat_id in song.orders:
            if pat_id >= len(song.patterns):
                continue
            pat = song.patterns[pat_id]
            for row in pat:
                if ch >= len(row):
                    continue
                total_rows += 1
                cell = row[ch]
                if _has_note(cell):
                    note_rows += 1
                if _is_active(cell):
                    active_rows += 1
                if cell.instrument >= 0:
                    instruments_seen.add(cell.instrument)
                if cell.effect > 0:
                    effects_seen[cell.effect] = effects_seen.get(cell.effect, 0) + 1
                if cell.xm_volume >= 0:
                    vol_events += 1

        density = (note_rows / total_rows * 100) if total_rows > 0 else 0
        stats.append({
            "ch": ch,
            "total_rows": total_rows,
            "note_rows": note_rows,
            "active_rows": active_rows,
            "density": density,
            "instruments": sorted(instruments_seen),
            "effects": effects_seen,
            "vol_events": vol_events,
        })
    return stats


# ---------------------------------------------------------------------------
# 2. Pairwise merge scoring
# ---------------------------------------------------------------------------

def merge_score(song: ModSong, ch_a: int, ch_b: int):
    """Score merging ch_b INTO ch_a (ch_b=donor, ch_a=target).

    Returns dict with preserved, conflicts, totals, and pct_preserved.
    """
    preserved = 0
    conflicts = 0
    ch_a_total = 0
    ch_b_total = 0

    for pat_id in song.orders:
        if pat_id >= len(song.patterns):
            continue
        pat = song.patterns[pat_id]
        for row in pat:
            if ch_a >= len(row) or ch_b >= len(row):
                continue
            a = row[ch_a]
            b = row[ch_b]
            a_has = _has_note(a)
            b_has = _has_note(b)
            if a_has:
                ch_a_total += 1
            if b_has:
                ch_b_total += 1
            if b_has and not a_has:
                preserved += 1
            elif b_has and a_has:
                conflicts += 1

    combined = ch_a_total + preserved
    pct = (preserved / ch_b_total * 100) if ch_b_total > 0 else 0

    return {
        "target": ch_a,
        "donor": ch_b,
        "preserved": preserved,
        "conflicts": conflicts,
        "ch_a_total": ch_a_total,
        "ch_b_total": ch_b_total,
        "combined": combined,
        "pct_preserved": pct,
    }


def all_merge_scores(song: ModSong):
    """Compute merge scores for all ordered channel pairs."""
    n = song.channels
    scores = []
    for a in range(n):
        for b in range(n):
            if a == b:
                continue
            scores.append(merge_score(song, a, b))
    return scores


# ---------------------------------------------------------------------------
# 3. Reduction plan generator & evaluator
# ---------------------------------------------------------------------------

def evaluate_plan(song: ModSong, plan: list, channel_stats: list):
    """Evaluate a reduction plan (list of ("drop",ch) / ("merge",tgt,donor))."""
    total_notes = sum(s["note_rows"] for s in channel_stats)
    notes_lost = 0
    notes_from_merge = 0

    for action in plan:
        if action[0] == "drop":
            notes_lost += channel_stats[action[1]]["note_rows"]
        elif action[0] == "merge":
            sc = merge_score(song, action[1], action[2])
            notes_lost += sc["conflicts"]
            notes_from_merge += sc["preserved"]

    notes_kept = total_notes - notes_lost
    return {
        "plan": plan,
        "total_notes": total_notes,
        "notes_kept": notes_kept,
        "notes_lost": notes_lost,
        "notes_from_merge": notes_from_merge,
        "pct_kept": (notes_kept / total_notes * 100) if total_notes > 0 else 0,
    }


def generate_plans(n_channels: int, target: int = 6):
    """Yield candidate reduction plans (drop / merge+drop combos)."""
    excess = n_channels - target
    if excess <= 0:
        return

    channels = list(range(n_channels))

    # Pure drop
    for dropped in itertools.combinations(channels, excess):
        yield [("drop", ch) for ch in dropped]

    # Single merge + drops
    if excess >= 1:
        for a, b in itertools.permutations(channels, 2):
            remaining = [c for c in channels if c != b]
            droppable = [c for c in remaining if c != a]
            if excess - 1 > len(droppable):
                continue
            for dropped in itertools.combinations(droppable, excess - 1):
                yield [("merge", a, b)] + [("drop", ch) for ch in dropped]

    # Double merge + drops
    if excess >= 2:
        for a1, b1 in itertools.permutations(channels, 2):
            remaining1 = [c for c in channels if c != b1]
            for a2, b2 in itertools.permutations(remaining1, 2):
                if a2 == a1 or b2 == a1:
                    continue
                remaining2 = [c for c in remaining1 if c != b2]
                droppable = [c for c in remaining2 if c != a1 and c != a2]
                need_drop = excess - 2
                if need_drop > len(droppable) or need_drop < 0:
                    continue
                for dropped in itertools.combinations(droppable, need_drop):
                    yield [("merge", a1, b1), ("merge", a2, b2)] + [("drop", ch) for ch in dropped]


# ---------------------------------------------------------------------------
# High-level: find best plan
# ---------------------------------------------------------------------------

def find_best_plan(song: ModSong, target: int = 6):
    """Return the best reduction plan as a ranked list (best first).

    Each entry is a dict with plan, total_notes, notes_kept, etc.
    Returns empty list if no reduction needed.
    """
    n_ch = song.channels
    if n_ch <= target:
        return []

    stats = channel_activity(song)
    best_plans = []
    seen = set()
    for plan in generate_plans(n_ch, target):
        result = evaluate_plan(song, plan, stats)
        sig = str(sorted(str(a) for a in plan))
        if sig in seen:
            continue
        seen.add(sig)
        best_plans.append(result)

    best_plans.sort(key=lambda r: (-r["notes_kept"], r["notes_lost"]))
    return best_plans


def plan_to_cli(plan: list) -> str:
    """Convert a plan's actions to CLI flags string."""
    parts = []
    merges = []
    drops = []
    for a in plan:
        if a[0] == "merge":
            merges.append(f"{a[2]+1}:{a[1]+1}")  # donor:target, 1-based
        elif a[0] == "drop":
            drops.append(str(a[1] + 1))
    if merges:
        parts.append("--merge_channels=" + ",".join(merges))
    if drops:
        parts.append("--drop_channels=" + ",".join(drops))
    return " ".join(parts)


def plan_to_actions(plan: list):
    """Extract (merge_pairs, drop_list) from a plan, both 1-based.

    Returns ([(donor, target), ...], [ch, ...])
    """
    merge_pairs = []
    drop_list = []
    for a in plan:
        if a[0] == "merge":
            merge_pairs.append((a[2] + 1, a[1] + 1))  # donor, target 1-based
        elif a[0] == "drop":
            drop_list.append(a[1] + 1)  # 1-based
    return merge_pairs, drop_list
