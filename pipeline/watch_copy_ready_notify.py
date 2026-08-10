"""Watch rewrites/copy-ready for new .md rewrites and show a macOS popup.

Usage:
  python -m pipeline.watch_copy_ready_notify
"""
from __future__ import annotations

import argparse
import pathlib
import time

from utils.macos_notify import POPUP_TEXT, notify_hk01_rewrite

BASE = pathlib.Path(__file__).resolve().parent.parent
COPY_READY = BASE / "rewrites" / "copy-ready"
SKIP = {"INDEX.md", "LATEST.md"}


def snapshot() -> set[str]:
    if not COPY_READY.exists():
        return set()
    return {
        p.name
        for p in COPY_READY.glob("*.md")
        if p.name not in SKIP and p.is_file()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Notify on new copy-ready rewrites")
    parser.add_argument("--poll-sec", type=float, default=5.0)
    args = parser.parse_args()
    COPY_READY.mkdir(parents=True, exist_ok=True)
    seen = snapshot()
    print(f"Watching {COPY_READY} for new rewrites (known={len(seen)})", flush=True)
    while True:
        time.sleep(args.poll_sec)
        current = snapshot()
        new = sorted(current - seen)
        if not new:
            continue
        seen = current
        detail = f"{len(new)} new: {', '.join(new[:3])}"
        if len(new) > 3:
            detail += f" (+{len(new) - 3} more)"
        print(f"{POPUP_TEXT} | {detail}", flush=True)
        notify_hk01_rewrite(detail)


if __name__ == "__main__":
    main()
