#!/usr/bin/env python3
"""Analyze channel merge strategies for reducing N channels to 6.

Given an S3M/MOD/XM with >6 channels, evaluates:
  1. Per-channel activity density (notes, effects, volume events)
  2. Per-channel instrument usage profile
  3. All pairwise merge scores (notes preserved vs lost)
  4. Optimal reduction plans ranked by total note preservation

Usage: python tools/merge_analysis.py <input_file>
"""
import sys
from pathlib import Path

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from mod_parser import ModSong, ModNote
from merge_analysis import (channel_activity, merge_score, all_merge_scores,
                             generate_plans, evaluate_plan, plan_to_cli)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_song(path_str: str) -> ModSong:
    """Auto-detect format and parse into ModSong (no channel dropping)."""
    p = Path(path_str)
    ext = p.suffix.lower()
    if ext == ".xm":
        from xm_parser import parse_xm
        return parse_xm(path_str)
    elif ext == ".s3m":
        from s3m_parser import parse_s3m
        return parse_s3m(path_str)
    else:
        from mod_parser import parse_mod
        return parse_mod(path_str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/merge_analysis.py <input_file> [target_channels]")
        sys.exit(1)

    input_path = sys.argv[1]
    target_ch = int(sys.argv[2]) if len(sys.argv) > 2 else 6

    print(f"Loading: {input_path}")
    song = _load_song(input_path)
    n_ch = song.channels
    print(f"Channels: {n_ch}, Orders: {len(song.orders)}, "
          f"Patterns: {len(song.patterns)}")
    print()

    if n_ch <= target_ch:
        print(f"Already at {target_ch} channels or fewer. Nothing to reduce.")
        return

    # --- 1. Channel activity ---
    print("=" * 70)
    print("1. CHANNEL ACTIVITY")
    print("=" * 70)
    stats = channel_activity(song)
    print(f"  {'Ch':>3} {'Notes':>6} {'Active':>7} {'Density':>8} {'Instruments':>20} {'VolEvt':>7}")
    print(f"  {'---':>3} {'-----':>6} {'------':>7} {'-------':>8} {'-----------':>20} {'------':>7}")
    for s in stats:
        ins_str = ",".join(str(i) for i in s["instruments"])
        print(f"  {s['ch']:3d} {s['note_rows']:6d} {s['active_rows']:7d} "
              f"{s['density']:7.1f}% {ins_str:>20s} {s['vol_events']:7d}")

    total_notes = sum(s["note_rows"] for s in stats)
    print(f"\n  Total note-rows: {total_notes}")

    # --- 2. Pairwise merge scores ---
    print()
    print("=" * 70)
    print("2. PAIRWISE MERGE SCORES (donor -> target)")
    print("=" * 70)
    scores = all_merge_scores(song)
    # Sort by preserved descending
    scores.sort(key=lambda s: s["preserved"], reverse=True)
    print(f"  {'Donor':>5} {'->':>3} {'Target':>6} {'Preserved':>10} {'Conflicts':>10} "
          f"{'Donor Tot':>10} {'Combined':>9} {'%Saved':>7}")
    print(f"  {'-----':>5} {'--':>3} {'------':>6} {'---------':>10} {'---------':>10} "
          f"{'---------':>10} {'--------':>9} {'------':>7}")
    for s in scores[:20]:  # top 20
        print(f"  ch{s['donor']:d} {' ->':>3} ch{s['target']:d} "
              f"{s['preserved']:10d} {s['conflicts']:10d} "
              f"{s['ch_b_total']:10d} {s['combined']:9d} "
              f"{s['pct_preserved']:6.1f}%")

    # --- 3. Best reduction plans ---
    print()
    print("=" * 70)
    print(f"3. BEST REDUCTION PLANS ({n_ch} -> {target_ch} channels)")
    print("=" * 70)

    best_plans = []
    seen = set()
    for plan in generate_plans(n_ch, target_ch):
        result = evaluate_plan(song, plan, stats)
        # Deduplicate by notes_kept + plan signature
        sig = str(sorted(str(a) for a in plan))
        if sig in seen:
            continue
        seen.add(sig)
        best_plans.append(result)

    # Sort by notes kept (descending), then notes lost (ascending)
    best_plans.sort(key=lambda r: (-r["notes_kept"], r["notes_lost"]))

    for rank, r in enumerate(best_plans[:15], 1):
        plan_str = " + ".join(
            f"drop(ch{a[1]})" if a[0] == "drop"
            else f"merge(ch{a[2]}->ch{a[1]})"
            for a in r["plan"]
        )
        print(f"  #{rank:2d}: {r['pct_kept']:5.1f}% kept "
              f"({r['notes_kept']:4d}/{r['total_notes']:4d} notes, "
              f"lost {r['notes_lost']:3d}"
              f"{', merged ' + str(r['notes_from_merge']) if r['notes_from_merge'] else ''}"
              f")")
        print(f"       {plan_str}")

    # --- Summary ---
    if best_plans:
        best = best_plans[0]
        current_drop = None
        for r in best_plans:
            if all(a[0] == "drop" for a in r["plan"]):
                current_drop = r
                break

        print()
        print("-" * 70)
        print("RECOMMENDATION:")
        if current_drop and best["notes_kept"] > current_drop["notes_kept"]:
            improvement = best["notes_kept"] - current_drop["notes_kept"]
            print(f"  Best merge plan saves {improvement} more notes "
                  f"({best['pct_kept']:.1f}% vs {current_drop['pct_kept']:.1f}%) "
                  f"over best pure-drop.")
        elif current_drop:
            print(f"  Pure drop is optimal or equivalent for this song "
                  f"({current_drop['pct_kept']:.1f}% notes kept).")
        print(f"  CLI: {plan_to_cli(best['plan'])}")
        print(f"  Or:  --merge_channels=auto")


if __name__ == "__main__":
    main()
