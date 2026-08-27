# One-Page Runbook - Primary Region Down

Operational guide for Region A outage containment and failover to Region B. Executable by on-call engineers at 3 AM.

| # | Step | Command | Completion Signal | Owner |
|---|---|---|---|---|
| 1 | Confirm Outage | `python chaos/kill_region.py status` | `a.alive=false` or `a.ready=false` for 3 consecutive probes | On-call SRE |
| 2 | Open Incident + Start RTO Clock | `python dr/runbook.py --primary a --target b --backend fs --auto` | Line `step:2, name:thong_bao_incident` logged in `reports/runbook-run.jsonl` | Incident Commander |
| 3 | Restore State in Secondary Region | `python state/snapshot.py get --region b --backend fs` | Files `state/region-b/vectors.sqlite` and `state/region-b/weights/model.bin` exist | SRE Automation |
| 4 | Scale Pool warm to full | `echo full > state/region-b/pool_state && curl localhost:8002/readyz` | `/readyz` on Region B returns HTTP 200 `{"status":"ready"}` | SRE Automation |
| 5 | DNS / Load Balancer Cutover | `echo b > edge/active_region && curl localhost:8080/edge/state` | Edge proxy returns `{"active_region":"b"}` | SRE Automation |
| 6 | Verify Golden Signals | `python -c "import httpx; [print(httpx.get('http://127.0.0.1:8080/v1/infer', timeout=3).status_code) for _ in range(10)]"` | 10 consecutive requests return 200, p95 latency < 100ms, error rate = 0% | On-call SRE |
| 7 | Measure RTO + Postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output returns `"valid":true`, `"rto_verdict":"PASS"`, RTO <= 300s | Incident Commander |

---

### Rollback Policy (Failback to Region A)

**Prerequisites for Rollback:**
1. Region A has been fully restored and `/healthz` + `/readyz` remain stable and healthy for at least 15 continuous minutes.
2. New data ingested into Region B during failover has been reverse-replicated back to Region A (`state/snapshot.py put --region b` and `state/snapshot.py get --region a`).
3. System traffic is in off-peak hours with no active network anomalies.

**Decision Authority:**
- Rollback execution must be explicitly authorized by the **Incident Commander** or **Lead SRE**.
- Automatic rollback without human verification or circuit breaker is strictly prohibited to prevent continuous flapping between regions.
