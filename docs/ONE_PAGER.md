# Steadfast: Zero Unaccounted Collections Calls
**Predixion AI × TalentX Open-Weight Collections Agent Challenge (AWS FDE Track)**  
*Architecture, Resilience Engineering & Production Deployment on AWS ap-south-1*  
**Repository:** [github.com/maannaan/steadfast](https://github.com/maannaan/steadfast) (Apache-2.0)

---

### Executive Summary
In digital lending operations under Reserve Bank of India (RBI) regulations, an unlogged collections call represents an audit failure and a compliance violation. Yet conversational voice pipelines chaining Speech-to-Text (STT), Large Language Models (LLM), and Text-to-Speech (TTS) inherently face unpredictable latency, high concurrency, and downstream failure rates of 20% or more. 

**Steadfast** is a carrier-grade inference orchestration service built to enforce a singular guarantee: **zero unaccounted calls**. Every borrower interaction ends deterministically in one of two states:
1. **The call log**, with complete turn telemetry and business outcome; or
2. **The dead-letter queue (DLQ)**, with structured failure reasons, ready for automated or manual replay.

No call is ever silently dropped, leaked, or left unresolved.

---

### Key Architectural Pillars

```
+-----------------------------------------------------------------------------------------+
|                                  AWS ap-south-1 (Mumbai)                                |
|                                                                                         |
|   Borrower / PBX           Application Load Balancer (HTTPS + x-steadfast-key)          |
|         │                                       │                                       |
|         ▼                                       ▼                                       |
|   POST /calls ────────► [ Blue / Green Native ECS Fargate Orchestrator ]                |
|                              │ (In-Flight Calls Target Tracking: 2 -> 8 tasks)          |
|                              │                                                          |
|               ┌──────────────┴──────────────┐                                           |
|               ▼                             ▼                                           |
|       [ Circuit Breaker ]           [ SQS DLQ + DynamoDB Index ]                        |
|       (100-attempt window)          (Encrypted at rest, 14-day retention)               |
|               │                                    │                                    |
|               ▼ (ECS Service Connect)              ▼                                    |
|       [ Model Tier on EC2 ]                [ Replay Worker ]                            |
|       - faster-whisper (STT)               (Idempotent by event_id,                     |
|       - Qwen2.5 (LLM)                      gated to 08:00-19:00 IST)                    |
|       - Piper (TTS, CPU/GPU)                       │                                    |
|               │                                    │                                    |
|               ▼                                    ▼                                    |
|   [ DynamoDB Call Log ] ◄──────────────────────────┘                                    |
|   (Persist-before-ack, PITR enabled)                                                    |
+-----------------------------------------------------------------------------------------+
```

1. **Persist-Before-Ack Semantics & Idempotency:**
   Calls are assigned a unique, deterministic `event_id`. HTTP 200 is returned only after state persistence (DynamoDB on AWS, SQLite with `synchronous=FULL` locally). Downstream failures return HTTP 200 with `failed_downstream` alongside the recorded DLQ reference; HTTP 500 is reserved exclusively for unrecordable errors so clients know when to resend.
2. **Resilience & Backoff with Full Jitter:**
   Each call allows up to 3 attempts with exponential backoff and full jitter (base 0.2 s, cap 2.0 s), bounded by a 2.5 s attempt timeout and an 8.0 s total deadline. A rolling 100-attempt per-task circuit breaker trips at 50% failure, failing fast directly into the DLQ to prevent thundering herds on degraded models.
3. **Target-Tracking Autoscaling via Little’s Law:**
   Because voice calls are I/O-bound requests waiting on inference, CPU utilization remains low even under heavy concurrency. Scaling tracks `InFlightCalls` per task (target: 20 calls/task), published as CloudWatch Embedded Metric Format (EMF) logs at zero marginal API cost.
4. **Native ECS Blue/Green Deployments & Multi-AZ Resilience:**
   Zero-downtime upgrades shift production traffic over 2-minute bake periods, preceded by pre-shift verification via dedicated test headers (`X-Deploy-Test: 1`). Rollbacks occur automatically if alarms trip. Orchestrator tasks are balanced across Availability Zones with clean connection draining.
5. **Regulatory Compliance (RBI & DPDP Act 2023):**
   - **Localisation:** 100% of data (DynamoDB, SQS, S3, CloudWatch Logs) resides strictly within `ap-south-1` (Mumbai).
   - **No PII:** Logs and metrics omit borrower phone numbers or sensitive credit identifiers.
   - **Calling Window Enforcement:** Both real-time calls and DLQ replay workers enforce RBI’s permissible recovery calling window (`08:00–19:00 IST`).

---

### Empirical Validation & Test Results

| Test Scenario | Volume / Speed | Injected Conditions | Outcome | Unaccounted |
|---|---|---|---|---|
| **Local Independent** | 6,083 calls (60×) | 20% random downstream 503s | 4,876 1st-try, 1,163 retried, 44 DLQ | **0** |
| **Local Outage Burst** | 6,083 calls (60×) | 15s outage every 60s (4 breaker trips) | 3,161 1st-try, 725 retried, 2,197 DLQ | **0** |
| **AWS Campaign Window** | 6,083 calls (6×) | 6-turn calls, 20% turn faults, autoscaling | Tasks scaled 2 → 8 → 2; DLQ drained clean | **0** |
| **AWS Multi-AZ Drill** | Live campaign | Terminated all tasks in AZ-a mid-flight | AZ-b absorbed load, 0 client 5xx | **0** |
| **AWS Blue/Green Shift** | Live campaign | Valid revision shifted; broken tag rolled back | Green tested, bake passed; rollback automatic | **0** |
| **Open-Weight CPU Tier** | 1,037 calls (1×) | 4× overload on faster-whisper + Qwen + Piper | Fast 429 shedding into DLQ | **0** |

---

### Cost Economics & Infrastructure Efficiency

Measured on AWS `ap-south-1` via Cost Explorer (`scripts/aws_costs.py`):

- **Measured Session Cost:** **$2.70 total** (Fargate compute: $0.88; NAT Gateway: $0.86; EC2 model host: $0.47; ALB: $0.31; VPC/IP: $0.16; DynamoDB/SQS: $0.02; CloudWatch EMF: < $0.01).
- **Compliance with Budget Cap:** Under 10% of the ₹2,500 (~$30.00) challenge ceiling.
- **Unit Economics:** **~$0.000074 per call** (~6.2 paise per 100 calls) at peak autoscaling volume.
- **Teardown Proof:** End-of-session verification script queries AWS APIs directly for all tagged resources: **`RESULT: PASS`**.
