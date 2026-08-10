"""macOS popup for HK01 rewrite events."""
from __future__ import annotations

import argparse
import subprocess
import sys

POPUP_TEXT = "HK01 local news rewrite"


def notify_hk01_rewrite(detail: str = "") -> None:
    """Show a macOS alert popup. Text is always POPUP_TEXT."""
    message = detail.strip() or "A new rewrite is ready in rewrites/copy-ready."
    # Escape for AppleScript string literals.
    title = POPUP_TEXT.replace("\\", "\\\\").replace('"', '\\"')
    body = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display alert "{title}" message "{body}" as informational'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 — never break the rewrite pipeline
        print(f"notify failed: {exc}", file=sys.stderr)
        # Banner fallback
        try:
            banner = (
                f'display notification "{body}" with title "{title}" '
                f'sound name "Glass"'
            )
            subprocess.run(["osascript", "-e", banner], check=False, timeout=30)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="HK01 rewrite macOS popup")
    parser.add_argument(
        "--detail",
        default="",
        help="Optional subtitle/message under the popup title",
    )
    args = parser.parse_args()
    notify_hk01_rewrite(args.detail)
    print(POPUP_TEXT)


if __name__ == "__main__":
    main()
