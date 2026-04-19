#!/usr/bin/env python3
"""Regression test: convert all example files and verify output structure.

Usage:
    python tools/regression_test.py           # run all tests
    python tools/regression_test.py --keep    # don't delete temp output files

Each example dir must have a convert.bat whose 'python' line is parsed for
the source file and CLI flags.  Output goes to a temp .fur file, which is
run through verify_fur.py.  Exit code 0 = all passed.
"""

import subprocess, sys, re, shutil, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONVERTER = ROOT / "convert_mod.py"
VERIFIER  = ROOT / "tools" / "verify_fur.py"
EXAMPLES  = ROOT / "examples"

keep_temps = "--keep" in sys.argv

# Ensure subprocesses can print Unicode
_env = os.environ.copy()
_env["PYTHONIOENCODING"] = "utf-8"


def parse_convert_bat(bat_path: Path):
    """Extract source file and CLI flags from a convert.bat."""
    for line in bat_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.lower().startswith("python") and "convert_mod.py" in line:
            # Remove the python prefix and script path
            m = re.search(r'convert_mod\.py\s+(.*)', line)
            if m:
                return m.group(1).rstrip()
    return None


def run_test(example_dir: Path):
    """Run conversion + verification for one example. Returns (name, ok, msg)."""
    name = example_dir.name
    bat = example_dir / "convert.bat"
    if not bat.exists():
        return name, None, "SKIP (no convert.bat)"

    args_str = parse_convert_bat(bat)
    if args_str is None:
        return name, None, "SKIP (no convert_mod.py line in .bat)"

    # Parse args: first positional is source, rest are flags
    parts = args_str.split()
    source = None
    flags = []
    positional = []
    for p in parts:
        if p.startswith("--"):
            flags.append(p)
        elif p.lower() != "pause":
            positional.append(p)

    source = positional[0] if positional else None
    if source is None:
        return name, False, "FAIL: no source file in convert.bat"

    source_path = example_dir / source
    if not source_path.exists():
        return name, False, f"FAIL: source file not found: {source}"

    # Output to temp file
    out_path = example_dir / "_regression_test.fur"

    cmd = [sys.executable, str(CONVERTER), str(source_path), str(out_path)] + flags
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(example_dir), env=_env)
    if result.returncode != 0:
        return name, False, f"FAIL: converter exit {result.returncode}\n{result.stderr}"

    if not out_path.exists():
        return name, False, "FAIL: no output .fur produced"

    # Verify structure
    vresult = subprocess.run(
        [sys.executable, str(VERIFIER), str(out_path)],
        capture_output=True, text=True, env=_env)
    passed = "ALL CHECKS PASSED" in vresult.stdout

    if not keep_temps and out_path.exists():
        out_path.unlink()
    # Also clean up samples zip if created
    for z in example_dir.glob("*_samples.zip"):
        if z.name.startswith("_regression"):
            z.unlink(missing_ok=True)

    if not passed:
        return name, False, f"FAIL: verify_fur\n{vresult.stdout}"

    # Extract summary from converter output
    summary = ""
    for line in result.stdout.splitlines():
        if line.startswith("\u2713") or line.startswith("  Instruments:") or line.startswith("  Arpeggio:"):
            summary += "  " + line.strip() + "\n"
    return name, True, summary.rstrip() or "OK"


def main():
    print(f"Regression test: {EXAMPLES}")
    print("=" * 60)

    results = []
    for d in sorted(EXAMPLES.iterdir()):
        if d.is_dir():
            name, ok, msg = run_test(d)
            results.append((name, ok, msg))
            status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
            print(f"  [{status}] {name}")
            if ok is False:
                for line in msg.splitlines():
                    print(f"         {line}")
            elif ok and msg:
                for line in msg.splitlines():
                    print(f"         {line}")

    print("=" * 60)
    total = len([r for r in results if r[1] is not None])
    passed = len([r for r in results if r[1] is True])
    failed = len([r for r in results if r[1] is False])
    skipped = len([r for r in results if r[1] is None])
    print(f"Total: {total} tests, {passed} passed, {failed} failed, {skipped} skipped")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
