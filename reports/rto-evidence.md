# RTO/RPO Evidence - Lab 23

Report on measured RTO and RPO from multi-region DR event logs.

## 1. Drill 1 - Baseline (No DR)

| Metric | Value | Measurement | Evidence |
|---|---|---|---|
| t_outage | `2026-08-27T02:22:03` | chaos kill | `chaos/chaos-events.jsonl:1` |
| First failed request | `+0.0s` | first `ok:false` line after t_outage | `reports/drill-1-nodr.jsonl:17` |
| Subsequent successful request | none | no `ok:true` line after t_outage | `reports/measure-drill-1.json:10` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json:25` |

## 2. Drill 2 - With DR

| Milestone | +seconds from t_outage | Measurement | Evidence |
|---|---|---|---|
| t_outage (point 0) | 0.0s | `action:kill` | `chaos/chaos-events.jsonl:2` |
| User sees first error | 0.5s | first `ok:false` line | `reports/drill-2-withdr.jsonl:26` |
| Health check detection | 14.8s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore complete | 17.6s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Target region ready | 23.8s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 23.8s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **Measured RTO** | 29.0s | first `ok:true` line served by region b | `reports/drill-2-withdr.jsonl:40` |

| Metric | Measured | Target (Slide 1) | Verdict |
|---|---|---|---|
| RTO - Inference API | `29.0s` | 300.0s (5 min) | PASS |
| RPO - Vector DB | `2.0s` / `1` doc | 300.0s (5 min) | PASS |

## 3. RTO Component Breakdown

| Component | Seconds | Source | Mitigation / Optimization |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s * threshold` (5s * 3) in `reports/health-events.jsonl:2` | Reduce interval (e.g. 2s) or threshold (e.g. 2), trade-off: increased flapping risk during transient network jitter |
| Snapshot restore | 0.1s | 2_restore to 3_scale in `reports/failover-events.jsonl:2` | Optimize storage I/O, incremental snapshot replication |
| GPU pool warm-up | 6.2s | `waited_s` in `4_wait_ready` in `reports/failover-events.jsonl:4` | Maintain pre-warmed standby pool with weights preloaded in memory |
| DNS/LB TTL cache | 5.2s | t_recovered (29.0s) - t_cutover (23.8s) in `reports/drill-2-withdr.jsonl:40` | Lower proxy / DNS TTL from 5s to 1s |
| Runbook dispatch overhead | 2.5s | Operator notification delay and dispatch in `reports/runbook-run.jsonl:2` | Full automation with automated circuit-breaker |
