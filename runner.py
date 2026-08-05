import os
import pathlib
import random
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
CMD = [sys.executable, "-m", "pipeline.run_once", "latest", "--n", "3"]


def run_once():
    with open(BASE / "logs" / "runner.log", "a", encoding="utf-8") as log:
        log.write("\n=== run start ===\n")
        p = subprocess.Popen(CMD, cwd=str(BASE), stdout=log, stderr=log)
        p.wait()
        log.write("=== run end ===\n")


if __name__ == "__main__":
    (BASE / "logs").mkdir(exist_ok=True)
    interval_sec = int(os.getenv("HK01_INTERVAL_SEC", "600"))  # default 10 min
    while True:
        run_once()
        # jitter 0–30s to be nice to the origin
        time.sleep(interval_sec + random.randint(0, 30))
