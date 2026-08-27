"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [Step {n}: {name}]", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        print(f"[AUTO-CONFIRM] {msg} -> Y")
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ["y", "yes"]
    except EOFError:
        return True


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước tự động hoá runbook."""
    t_start = time.time()

    # Bước 1: xac_nhan_outage
    primary_ok, primary_reason = hc.probe(primary, timeout=1.5)
    target_ok, target_reason = hc.probe(target, timeout=1.5)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ok=primary_ok,
        primary_reason=primary_reason,
        target=target,
        target_ok=target_ok,
        target_reason=target_reason,
    )

    # Bước 2: thong_bao_incident
    # Tìm t_outage gần nhất từ chaos-events.jsonl nếu có
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_log.exists():
        kills = [
            json.loads(line)
            for line in chaos_log.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("action") == "kill"
        ]
        if kills:
            t_outage = kills[-1].get("ts")

    operator_ts = time.time()
    notification_delay_s = round(operator_ts - t_outage, 2) if t_outage else 0.0

    if not confirm(auto, f"Confirm failover from region-{primary} to region-{target}?"):
        step(2, "thong_bao_incident", confirmed=False, status="aborted_by_operator")
        return {"ok": False, "error": "aborted_by_operator"}

    step(
        2,
        "thong_bao_incident",
        confirmed=True,
        t_outage=t_outage,
        operator_ts=operator_ts,
        notification_delay_s=notification_delay_s,
    )

    # Bước 3: scale_gpu_pool (gọi failover.failover MỘT LẦN DUY NHẤT)
    fo_result = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", fo_ok=fo_result.get("ok"), fo_target=target)

    if not fo_result.get("ok"):
        return {"ok": False, "step": 3, "fo_result": fo_result}

    # Bước 4: verify_state_replica (ĐỌC từ dict fo_result)
    step(
        4,
        "verify_state_replica",
        target=target,
        rpo_seconds=fo_result.get("rpo_seconds"),
        docs_lost=fo_result.get("docs_lost"),
        embed_model_version=fo_result.get("embed_model_version"),
    )

    # Bước 5: dns_cutover (ĐỌC lại kết quả active_region)
    active_file = pathlib.Path("edge/active_region")
    active_region = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else ""
    step(5, "dns_cutover", active_region=active_region, cutover_ok=(active_region == target))

    # Bước 6: verify_golden_signals (10 request thật)
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t_req = time.time()
            try:
                r = client.get(f"{URL[target]}/v1/infer", params={"q": f"test signal {i}"})
                lat_ms = (time.time() - t_req) * 1000.0
                latencies.append(lat_ms)
                if r.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1

    latencies.sort()
    # Tính p95 latency
    p95_idx = int(len(latencies) * 0.95)
    p95_lat = round(latencies[min(p95_idx, len(latencies) - 1)], 1) if latencies else 0.0
    error_rate = round(errors / 10.0, 2)
    step(
        6,
        "verify_golden_signals",
        requests_count=10,
        errors=errors,
        error_rate=error_rate,
        p95_latency_ms=p95_lat,
    )

    # Bước 7: post_incident
    elapsed_s = round(time.time() - t_start, 2)
    measure_cmd = "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed_s, measure_cmd=measure_cmd)

    return {
        "ok": True,
        "primary": primary,
        "target": target,
        "elapsed_s": elapsed_s,
        "fo_result": fo_result,
        "p95_latency_ms": p95_lat,
        "error_rate": error_rate,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
