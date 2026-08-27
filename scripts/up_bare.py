"""Cross-platform script to start bare stack."""
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
RUN.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)


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


def start_service(name, port, env_extra, app_module):
    env = os.environ.copy()
    env.update(env_extra)
    env["PYTHONUTF8"] = "1"
    log_file = (RUN / f"{name}.log").open("w", encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        app_module,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    (RUN / f"{name}.pid").write_text(str(proc.pid))
    print(f"{name} pid={proc.pid} port={port}")
    return proc


def main():
    stop_all()
    start_service(
        "region-a",
        8001,
        {"REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"},
        "serving.app:app",
    )
    start_service(
        "region-b",
        8002,
        {"REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"},
        "serving.app:app",
    )
    start_service(
        "edge",
        8080,
        {"EDGE_TTL_SECONDS": "5"},
        "edge.proxy:app",
    )

    print("Checking services up (up to 10s)...")
    for name, port, path in [
        ("region-a", 8001, "/healthz"),
        ("region-b", 8002, "/healthz"),
        ("edge", 8080, "/edge/state"),
    ]:
        up = False
        for _ in range(20):
            try:
                r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=0.5)
                if r.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if up:
            print(f"  {name} (port {port}): UP")
        else:
            print(f"  {name} (port {port}): NOT RESPONDING")
            sys.exit(1)

    r = httpx.get("http://127.0.0.1:8080/edge/state")
    print(r.json())


if __name__ == "__main__":
    main()
