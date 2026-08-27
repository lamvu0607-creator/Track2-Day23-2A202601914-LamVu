"""Full automated script for Drill 1 (No DR)."""
import os
import pathlib
import subprocess
import sys
import time
import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)

RUN = ROOT / "run"
REPORTS = ROOT / "reports"
CHAOS = ROOT / "chaos"
RUN.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

# 1. Stop existing
def stop_all():
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

stop_all()

# 2. Seed
print("Seeding vectors...")
subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "a", "--docs", "200"], check=True)
subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"], check=True)
(ROOT / "edge" / "active_region").write_text("a", encoding="utf-8")

# 3. Clean old logs
(CHAOS / "chaos-events.jsonl").unlink(missing_ok=True)
(REPORTS / "drill-1-nodr.jsonl").unlink(missing_ok=True)
(REPORTS / "measure-drill-1.json").unlink(missing_ok=True)

# 4. Start services
procs = []
for name, port, env_extra in [
    ("region-a", 8001, {"REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"}),
    ("region-b", 8002, {"REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"}),
    ("edge", 8080, {"EDGE_TTL_SECONDS": "5"}),
]:
    env = os.environ.copy()
    env.update(env_extra)
    env["PYTHONUTF8"] = "1"
    app_module = "edge.proxy:app" if name == "edge" else "serving.app:app"
    log_f = (RUN / f"{name}.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app_module, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    (RUN / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")
    procs.append((name, port, proc))

print("Waiting for services to become healthy...")
time.sleep(3)
for name, port, path in [
    ("region-a", 8001, "/healthz"),
    ("region-b", 8002, "/healthz"),
    ("edge", 8080, "/edge/state"),
]:
    up = False
    for _ in range(30):
        try:
            r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
            if r.status_code == 200:
                up = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not up:
        print(f"FAILED to start {name} on port {port}")
        stop_all()
        sys.exit(1)
    print(f"  {name} on port {port}: UP")

# 5. Start traffic generator
print("Starting traffic generator (40s, 2 rps)...")
loadgen_proc = subprocess.Popen([
    sys.executable, "loadgen/traffic.py", "--duration", "40", "--rps", "2", "--out", "reports/drill-1-nodr.jsonl"
])

time.sleep(8)
print("Triggering chaos kill on region a...")
subprocess.run([
    sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
], check=True)

print("Waiting for load generator to finish...")
loadgen_proc.wait()

print("Measuring Drill 1...")
res = subprocess.run([
    sys.executable, "tools/measure_rto.py", "--loadgen", "reports/drill-1-nodr.jsonl", "--target-rto", "300"
], capture_output=True, text=True, encoding="utf-8")

print(res.stdout)
(REPORTS / "measure-drill-1.json").write_text(res.stdout, encoding="utf-8")

stop_all()
print("Drill 1 complete!")
