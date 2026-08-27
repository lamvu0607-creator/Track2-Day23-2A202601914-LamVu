"""Orchestrator to run Drill 1 (baseline no DR) and Drill 2 (with DR) cleanly on Windows."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)

RUN = ROOT / "run"
REPORTS = ROOT / "reports"
CHAOS = ROOT / "chaos"
STATE = ROOT / "state"

RUN.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)
CHAOS.mkdir(parents=True, exist_ok=True)

BASE_ENV = os.environ.copy()
BASE_ENV["PYTHONUTF8"] = "1"


def stop_all():
    print("Stopping all running background services...")
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
    time.sleep(1)


def seed_state():
    print("Seeding initial state...")
    shutil.rmtree(STATE / "_replica", ignore_errors=True)
    subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "a", "--docs", "200"], check=True, env=BASE_ENV)
    subprocess.run([sys.executable, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"], check=True, env=BASE_ENV)
    (ROOT / "edge" / "active_region").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "edge" / "active_region").write_text("a", encoding="utf-8")


def start_services():
    print("Starting services (region-a, region-b, edge)...")
    for name, port, env_extra, app in [
        ("region-a", 8001, {"REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"}, "serving.app:app"),
        ("region-b", 8002, {"REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"}, "serving.app:app"),
        ("edge", 8080, {"EDGE_TTL_SECONDS": "5"}, "edge.proxy:app"),
    ]:
        env = BASE_ENV.copy()
        env.update(env_extra)
        log_file = (RUN / f"{name}.log").open("w", encoding="utf-8")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        (RUN / f"{name}.pid").write_text(str(proc.pid), encoding="utf-8")

    # Wait for services
    print("Waiting for services to become healthy...")
    for name, port, path in [("region-a", 8001, "/healthz"), ("region-b", 8002, "/healthz"), ("edge", 8080, "/edge/state")]:
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
            raise RuntimeError(f"Service {name} on port {port} failed to start")
    print("All services are UP!")


def run_drill_1():
    print("\n==========================================")
    print("           STARTING DRILL 1 (NO DR)        ")
    print("==========================================")
    stop_all()
    seed_state()
    (CHAOS / "chaos-events.jsonl").unlink(missing_ok=True)
    (REPORTS / "drill-1-nodr.jsonl").unlink(missing_ok=True)
    (REPORTS / "measure-drill-1.json").unlink(missing_ok=True)
    start_services()

    print("Starting loadgen (40s, 2 rps)...")
    loadgen = subprocess.Popen([
        sys.executable, "loadgen/traffic.py", "--duration", "40", "--rps", "2", "--out", "reports/drill-1-nodr.jsonl"
    ], env=BASE_ENV)
    print("Waiting 8 seconds before chaos kill...")
    time.sleep(8)

    print("Triggering chaos kill on region a...")
    subprocess.run([
        sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
    ], check=True, env=BASE_ENV)

    print("Waiting for loadgen to complete...")
    loadgen.wait()

    print("Measuring Drill 1...")
    m1 = subprocess.run([
        sys.executable, "tools/measure_rto.py", "--loadgen", "reports/drill-1-nodr.jsonl", "--target-rto", "300"
    ], capture_output=True, text=True, env=BASE_ENV)
    print(m1.stdout)
    (REPORTS / "measure-drill-1.json").write_text(m1.stdout, encoding="utf-8")


def run_drill_2():
    print("\n==========================================")
    print("          STARTING DRILL 2 (WITH DR)       ")
    print("==========================================")
    stop_all()
    seed_state()
    (REPORTS / "drill-2-withdr.jsonl").unlink(missing_ok=True)
    (REPORTS / "health-events.jsonl").unlink(missing_ok=True)
    (REPORTS / "failover-events.jsonl").unlink(missing_ok=True)
    (REPORTS / "runbook-run.jsonl").unlink(missing_ok=True)
    (REPORTS / "replication.jsonl").unlink(missing_ok=True)
    (REPORTS / "measure-drill-2.json").unlink(missing_ok=True)
    start_services()

    print("Starting continuous ingest & replication...")
    ingest_proc = subprocess.Popen([
        sys.executable, "state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "120"
    ], env=BASE_ENV)
    replicate_proc = subprocess.Popen([
        sys.executable, "state/replicate.py", "--every", "15", "--duration", "120", "--backend", "fs"
    ], env=BASE_ENV)

    print("Waiting 16s for first replication cycle to complete...")
    time.sleep(16)
    manifest = STATE / "_replica" / "dr-artifacts" / "MANIFEST.json"
    if not manifest.exists():
        raise RuntimeError("Replication snapshot failed to produce MANIFEST.json")
    print("First replication snapshot verified!")

    print("Starting loadgen (100s, 2 rps) & health checker (interval=5s, threshold=3)...")
    loadgen_proc = subprocess.Popen([
        sys.executable, "loadgen/traffic.py", "--duration", "100", "--rps", "2", "--out", "reports/drill-2-withdr.jsonl"
    ], env=BASE_ENV)
    health_proc = subprocess.Popen([
        sys.executable, "dr/health_checker.py", "--interval", "5", "--threshold", "3", "--duration", "100", "--out", "reports/health-events.jsonl"
    ], env=BASE_ENV)

    print("Waiting 12s before chaos kill...")
    time.sleep(12)

    print("Triggering chaos kill on region a...")
    subprocess.run([
        sys.executable, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
    ], check=True, env=BASE_ENV)

    print("Waiting for health checker to detect outage (threshold=3, interval=5s -> ~15s)...")
    health_log = REPORTS / "health-events.jsonl"
    detected = False
    for _ in range(40):
        if health_log.exists():
            lines = [l for l in health_log.read_text(encoding="utf-8").splitlines() if l.strip()]
            for line in lines:
                try:
                    ev = json.loads(line)
                    if ev.get("event") == "state_change" and ev.get("to") == "UNHEALTHY" and ev.get("region") == "a":
                        detected = True
                        print(f"Health checker emitted UNHEALTHY for region a: {line}")
                        break
                except Exception:
                    pass
        if detected:
            break
        time.sleep(0.5)

    if not detected:
        print("Warning: health checker did not detect UNHEALTHY yet, proceeding anyway...")

    print("Executing automated DR runbook...")
    subprocess.run([
        sys.executable, "dr/runbook.py", "--primary", "a", "--target", "b", "--backend", "fs", "--auto"
    ], check=True, env=BASE_ENV)

    print("Waiting for loadgen to complete...")
    loadgen_proc.wait()

    # Clean up background procs
    for p in [ingest_proc, replicate_proc, health_proc]:
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            pass

    print("Measuring Drill 2...")
    m2 = subprocess.run([
        sys.executable, "tools/measure_rto.py", "--loadgen", "reports/drill-2-withdr.jsonl", "--target-rto", "300"
    ], capture_output=True, text=True, env=BASE_ENV)
    print(m2.stdout)
    (REPORTS / "measure-drill-2.json").write_text(m2.stdout, encoding="utf-8")


if __name__ == "__main__":
    run_drill_1()
    run_drill_2()
    stop_all()
    print("\nALL DRILLS COMPLETED!")
