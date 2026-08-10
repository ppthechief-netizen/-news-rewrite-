import os
import pathlib
import random
import subprocess
import sys
import time
from typing import Optional

from dotenv import load_dotenv

BASE = pathlib.Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
# Prefer a stable browser cache (avoids Cursor sandbox temp paths).
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(pathlib.Path.home() / "Library" / "Caches" / "ms-playwright"),
)
# Default: poll HK01 every 15 minutes; fetch enough links to catch bursts of new stories.
N = os.getenv("HK01_FETCH_N", "10")
# Modes:
#   rewrite — scrape + OpenAI rewrite into storage/final + rewrites/copy-ready
#   inbox — scrape only into rewrites/inbox (use when OpenAI is geo-blocked; rewrite in Cursor)
MODE = os.getenv("HK01_RUN_MODE", "inbox").strip().lower()
if MODE == "inbox":
    CMD = [sys.executable, "-m", "pipeline.poll_inbox", "--n", str(N)]
else:
    CMD = [sys.executable, "-m", "pipeline.run_once", "latest", "--n", str(N)]


def run_once():
    with open(BASE / "logs" / "runner.log", "a", encoding="utf-8") as log:
        log.write("\n=== run start ===\n")
        env = os.environ.copy()
        p = subprocess.Popen(CMD, cwd=str(BASE), stdout=log, stderr=log, env=env)
        p.wait()
        log.write(f"=== run end exit={p.returncode} ===\n")


def start_copy_ready_notifier() -> Optional[subprocess.Popen]:
    """Background watcher: macOS popup 'HK01 local news rewrite' on new .md files."""
    if os.getenv("HK01_NOTIFY", "1").strip() in {"0", "false", "no"}:
        return None
    log_path = BASE / "logs" / "notify_watch.log"
    log_f = open(log_path, "a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "pipeline.watch_copy_ready_notify"],
        cwd=str(BASE),
        stdout=log_f,
        stderr=log_f,
        env=os.environ.copy(),
        start_new_session=True,
    )


if __name__ == "__main__":
    (BASE / "logs").mkdir(exist_ok=True)
    interval_sec = int(os.getenv("HK01_INTERVAL_SEC", "900"))  # default 15 min
    notifier = start_copy_ready_notifier()
    print(f"HK01 runner: every {interval_sec}s, fetch n={N}, mode={MODE}", flush=True)
    if notifier:
        print("HK01 notify watcher: popup 'HK01 local news rewrite' on new copy-ready files", flush=True)
    while True:
        run_once()
        # jitter 0–30s to be nice to the origin
        time.sleep(interval_sec + random.randint(0, 30))
