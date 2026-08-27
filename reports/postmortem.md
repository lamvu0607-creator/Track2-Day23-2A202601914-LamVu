# Postmortem - DR Drill Lab 23

Blameless postmortem following DR drill execution.

## 1. Timeline (All lines have real evidence path:line)

| ISO time | Event | Evidence |
|---|---|---|
| 2026-08-27T02:23:08 | Outage start (Region A network blocked) | `chaos/chaos-events.jsonl:2` |
| 2026-08-27T02:23:09 | First user request impacted (HTTP 503 ConnectTimeout) | `reports/drill-2-withdr.jsonl:26` |
| 2026-08-27T02:23:23 | Health checker alerts Region A UNHEALTHY (after 3 consecutive failures) | `reports/health-events.jsonl:2` |
| 2026-08-27T02:23:26 | Operator confirms and activates failover runbook | `reports/runbook-run.jsonl:2` |
| 2026-08-27T02:23:26 | State snapshot restore completed to Region B | `reports/failover-events.jsonl:2` |
| 2026-08-27T02:23:32 | Region B GPU pool warm-up complete and ready | `reports/failover-events.jsonl:4` |
| 2026-08-27T02:23:32 | Edge DNS cutover to Region B completed | `reports/failover-events.jsonl:5` |
| 2026-08-27T02:23:37 | Incident resolved: First successful inference served by Region B | `reports/drill-2-withdr.jsonl:40` |

## 2. RTO / RPO Measured vs Target - Gap Analysis

- **RTO target:** 300.0s (5 min) | **Measured RTO:** `29.0s` | **gap:** `271.0s` faster than target (SLA Met).
- **RPO target:** 300.0s (5 min) | **Measured RPO:** `2.0s` (`1` doc lost) | **gap:** `298.0s` well below maximum threshold.
- **Step with largest duration:** `Health-check detection floor` (15.0s, 51.7% of total RTO). This is due to the anti-flapping design requiring 3 consecutive probe failures (`interval=5s * threshold=3`) before triggering state transition.

## 3. Root Cause (5 Whys)

1. *Why did inference requests fail?* Region A was partitioned and dropped TCP packets (netblock mode).
2. *Why couldn't Region B immediately serve traffic?* Active-Passive architecture: Region B starts in warm standby with empty vector database and unloaded model weights.
3. *Why did detection take 15s?* Health check probes every 5s and requires 3 consecutive failures to avoid flapping.
4. *Why did Region B take 6.2s to become ready after restore?* Model weights had to be verified and worker pool scaled from warm to full.
5. *Why was only 1 document lost?* Continuous replication was running every 15s, so the last snapshot was only 2.0s behind the primary database at failure time.

## 4. Action Items (with owner + deadline)

| # | Action Item | Owner | Deadline | RTO/RPO Reduction |
|---|---|---|---|---|
| 1 | Action item: Tune health check interval from 5s to 3s with exponential backoff | SRE Team | 2026-09-05 | Reduces RTO by 6.0s (floor from 15s to 9s) |
| 2 | Action item: Implement pre-warmed GPU worker standby in secondary region | MLOps | 2026-09-12 | Reduces RTO by 4.0s |
| 3 | Action item: Implement streaming replication instead of periodic batch snapshots | Data Team | 2026-09-20 | Reduces RPO to < 1.0s and 0 docs lost |

## 5. Three Mandatory Questions

1. **What is your `interval * threshold` in seconds, and what percentage of RTO does it represent?**
   - Configured: `5.0s * 3 = 15.0s`.
   - Percentage: `(15.0 / 29.0) * 100% = 51.7%` of measured RTO. It represents the single largest component.

2. **If you lower interval to 1s, how many seconds does RTO decrease, and what is the trade-off (flapping)?**
   - Lowering interval to 1s with threshold=3 reduces detection floor from 15s to 3s (12.0s reduction in RTO).
   - Trade-off: High sensitivity to temporary network hiccups/jitter. Transient packet loss could trigger unintended failover (flapping) between regions, causing service churn and wasted GPU scaling overhead.

3. **If the outage lasts 6 hours and Region A loses data permanently, what does `docs_lost` mean for customers?**
   - `docs_lost = 1 doc` is the data written to Region A between the last replication snapshot and the outage.
   - If Region A is permanently destroyed, this 1 document is lost permanently and client applications must re-ingest or re-submit that unconfirmed transaction.
