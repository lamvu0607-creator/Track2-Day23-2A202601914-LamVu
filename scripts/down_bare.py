"""Cross-platform script to stop bare stack."""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN = ROOT / "run"

if RUN.exists():
    for f in RUN.glob("*.pid"):
        try:
            pid = int(f.read_text().strip())
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
        f.unlink(missing_ok=True)

print("all stopped")
