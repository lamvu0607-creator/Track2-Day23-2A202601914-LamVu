"""Script to execute Drill 1 (No DR)."""
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)

REPORTS = ROOT / "reports"
CHAOS = ROOT / "chaos"
REPORTS.mkdir(parents=True, exist_ok=True)

# Clean previous event logs if needed
(CHAOS / "chaos-events.jsonl").unlink(missing_ok=True)
(REPORTS / "drill-1-nodr.jsonl").unlink(missing_ok=True)
(REPORTS / "measure-drill-1.json").unlink(missing_ok=True)

print("Starting traffic generator (40s, 2 rps)...")
loadgen_proc = subprocess.Popen([
    sys.executable, "loadgen/traffic.py", "--duration", "40", "--rps", "2", "--out", "reports/drill-1-nodr.jsonl"
])

time.sleep(8)
print("Triggering chaos kill on region a...")
subprocess.run([
    sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
], check=True)

print("Waiting for traffic generator to finish...")
loadgen_proc.wait()

print("Measuring Drill 1...")
res = subprocess.run([
    sys.executable, "tools/measure_rto.py", "--loadgen", "reports/drill-1-nodr.jsonl", "--target-rto", "300"
], capture_output=True, text=True)

print(res.stdout)
(REPORTS / "measure-drill-1.json").write_text(res.stdout, encoding="utf-8")
