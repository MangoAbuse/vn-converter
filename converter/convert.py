"""Convert a visual novel installed on this PC into a bundle for the web player.

    python convert.py saya --game "C:\\Games\\Saya no Uta"
    python convert.py lb   --game "C:\\Games\\Little Busters English Edition"
    python convert.py we   --game "C:\\Games\\Wonderful Everyday Down the Rabbit-Hole"

--game is the folder that holds the game's program file. The bundle (.zip) is written
to ..\\out and intermediate files to ..\\work. Nothing is uploaded anywhere; everything
stays on this PC. See README.md for the exact releases that are supported.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("game_id", choices=["saya", "lb", "we"], help="which game to convert")
    parser.add_argument("--game", required=True, type=Path, help="folder the game is installed in")
    parser.add_argument("--work", type=Path, help="folder for intermediate files (default: ..\\work\\<game>)")
    parser.add_argument("--out", type=Path, default=ROOT / "out", help="folder for the finished bundle")
    parser.add_argument("--scripts-only", action="store_true",
                        help="translate the scripts and report, without converting media (lb and we)")
    args = parser.parse_args()

    if not args.game.is_dir():
        sys.exit(f"Game folder not found: {args.game}")
    work = args.work or ROOT / "work" / args.game_id

    started = time.time()
    if args.game_id == "saya":
        import saya
        saya.convert(args.game, work, args.out)
    elif args.game_id == "we":
        import wonderful
        wonderful.convert(args.game, work, args.out, scripts_only=args.scripts_only)
    else:
        import littlebusters
        littlebusters.convert(args.game, work, args.out, scripts_only=args.scripts_only)
    print(f"\nDone in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()
