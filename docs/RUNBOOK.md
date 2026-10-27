# Runbook: Collections Voice-Agent Inference Service (Steadfast)

This follows Predixion's template (Appendix I), filled in with this deployment's real
names. Everything is in **ap-south-1**, and every resource carries the tag
`Project=steadfast`.

## Where to look

| What | Name |
|---|---|
| Dashboard | CloudWatch → Dashboards → `steadfast` (`terraform -chdir=infra output -raw dashboard_url`) |
| Alarms (to SNS topic `steadfast-alerts`) | `steadfast-latency-p99`, `steadfast-dlq-not-empty`, `steadfast-5xx-rate` |
| App metrics | namespace `CollectionsChallenge`, dimension `ServiceName=steadfast-orchestrator`: `Calls`, `Retries`, `DownstreamErrors`, `DLQEnqueued`, `LatencyMs`, `InFlightCalls`, `CircuitOpen` (the last two also per `TaskId`) |
| Logs | `/ecs/steadfast/orchestrator`, `/ecs/steadfast/mock`, `/ecs/steadfast/model`, `/ecs/steadfast/replay` |
| Scaling and task events | `/aws/events/steadfast/ecs` (EventBridge rule `steadfast-ecs-events`) |
| ECS | cluster `steadfast`; services `steadfast-orchestrator`, `steadfast-mock`, `steadfast-model`, `steadfast-replay` |
| DLQ | SQS queue `terraform -chdir=infra output -raw dlq_queue_name`, index table `steadfast-dead-letters` |
| Call log | DynamoDB `steadfast-call-log` (point-in-time recovery on) |

Operator shell setup (once per terminal). It sets the URL, the access key and the pinned
certificate that the CLI and the scripts use:

```bash
export AWS_PROFILE=<your profile> AWS_REGION=ap-south-1
eval "$(scripts/aws_env.sh)"
.venv/bin/python -m service.cli dlq stats        # calls received, DLQ pending/replayed
```

## Trigger

**p99 latency above 3× baseline during a live campaign window**, i.e. the alarm
`steadfast-latency-p99`. It watches ALB `TargetResponseTime` p99 above
3 × `baseline_p99_ms` for 3 of 5 minutes. Two related alarms open the same runbook:
- `steadfast-5xx-rate`: more than 2% of requests answered 5xx, i.e. calls we could not
  record;
- `steadfast-dlq-not-empty`: dead-lettered calls waiting for 5 minutes.

Known gap: with the 6-turn demo settings, the baseline is 30 s, so 3× is 90 s. That is above
the ALB's 60 s idle timeout, so the latency alarm cannot fire in that configuration. Calls
that slow surface as ALB 504s and trip `steadfast-5xx-rate` instead. For single-turn calls
(model tier), set `baseline_p99_ms` to the measured p99 (about 6000 on the CPU tier).

## First 5 minutes

1. **Is autoscaling keeping up with the ramp?** On the dashboard, compare "In-flight
   calls per task" with the target of 20, and "Orchestrator tasks: running vs desired".
   - If per-task in-flight is far above 20 while desired is rising, scale-out is lagging.
   - If desired is pinned at 10, the service is at its maximum.

   Healthy reference from the 6× load test: per-task in-flight peaked at 65 for one minute,
   and tasks went 2 → 8 within 5 minutes.
2. **Is it downstream or us?** Check the "Calls, retries and pipeline errors" widget:
   `DownstreamErrors` and `Retries` against `Calls`. The normal level is about 20% of attempts failing (the
   injected rate).
   - Much higher means the dependency is the cause.
   - Normal errors with high latency points at our tier (scaling, CPU, connections).
3. **Is the circuit breaker open?** Check the "Tasks whose circuit breaker was open"
   widget (`CircuitOpen`).
   - Open is good: the service is degrading gracefully, failing fast into the DLQ.
   - Closed while errors are near 100% is bad: we are retrying into a dead dependency.

   Logs Insights on `/ecs/steadfast/orchestrator`:

   ```
   filter event = "breaker_transition"
   | fields @timestamp, @logStream, @message
   | sort @timestamp desc
   | limit 50
   ```

   And the outcome mix per minute:

   ```
   filter event = "call"
   | stats count() as calls, sum(retries) as retries by bin(1m), outcome, dlq_reason
   ```

## If downstream is the cause

- **Confirm the breaker tripped.** Every task keeps its own breaker over its last 100
  attempts and opens at 50% failures.
  - If it has not opened, check whether the failure rate is really above 50% (`call`
    events with `dlq_reason = "retries_exhausted"` and few `circuit_open`).
  - If it is below 50%, retries are absorbing it, which is correct behaviour.
- **Check that the DLQ is capturing failures.** The dashboard's "Dead-lettered calls and
  DLQ depth" widget and the CLI (`dlq list --status pending`) should show the failed calls,
  each with a reason.
  - Persistence failures (`filter event = "persistence_failed"`) and ALB 5xx are the only
    signs that something was *not* captured. Those clients got a 500 and must resend.
- **Model tier specifics** (`/ecs/steadfast/model`):
  - 429 `overloaded` means the tier is at capacity. Add instances or switch to GPU.
  - 503 `warming_up` after a restart is expected for a few minutes.
  - Per-stage latency:

    ```
    filter event = "model_turn"
    | stats pct(latency_ms, 99) as attempt_p99, pct(stt_ms, 99) as stt_p99,
            pct(llm_ms, 99) as llm_p99, pct(tts_ms, 99) as tts_p99 by bin(5m)
    ```

## If autoscaling is the cause

- **Task count vs desired.** `aws ecs describe-services --cluster steadfast --services
  steadfast-orchestrator --query 'services[0].[desiredCount,runningCount,pendingCount]'`.
  If pending tasks never start, look at `/aws/events/steadfast/ecs` for the stopped reason:
  - Fargate vCPU quota: the account has 8. Ten 0.5-vCPU orchestrators plus the mock and
    the replay worker use 6.
  - Subnet IP exhaustion: the private subnets are /24 per AZ, far more than 10 tasks need.
  - Image pull errors.
- **Scaling history and its reasons:**
  `aws application-autoscaling describe-scaling-activities --service-namespace ecs --resource-id service/steadfast/steadfast-orchestrator`.
- **Cooldowns.** Scale-out cooldown is 60 s and scale-in is 300 s (`infra/autoscaling.tf`).
  - If the ramp outruns scale-out, lower `inflight_target_per_task`.
  - For known campaign start times, set `enable_scheduled_prescale = true`. It raises the
    floor to 5 tasks at 07:45 IST and lowers it again at 10:15. Only the morning window is
    scheduled today; add a pair for 13:45–16:15 when the afternoon campaign runs.

## Escalation

| After | Who | Action |
|---|---|---|
| 0 min | On-call engineer (SNS `steadfast-alerts`; in production, the paging tool) | Runs this runbook |
| 15 min unresolved | Platform lead | Joins; decides on rollback, a model-tier change or more capacity |
| Breaker open for more than 5 min, or DLQ growing with no end in sight | Campaign owner | Asked to **pause dialling**. Calls that cannot be served should not be placed; the DLQ is for recovery, not for queuing a whole campaign |

[TODO: names or rotation for each role, kept outside this public repo]

## Post-incident

1. **Replay the DLQ once the downstream has recovered**, at a controlled rate:
   `make aws-replay`. It runs one worker at 5 replays/s by default, waits until the queue
   is empty and scales the worker back to 0. Each replay goes through the normal retry and
   breaker path, so a dependency that is still sick is not flooded.
2. **Confirm unaccounted = 0.**
   - Every `event_id` the dialler sent must be in the call log
     (`POST /calls/lookup`, or `loadgen.report` for a test run).
   - `dlq stats` must show `dlq_pending = 0` and `dlq_replay_failed = 0`.
3. **Note actual vs expected latency and failure numbers**, and update the thresholds if
   reality moved:
   - `baseline_p99_ms`
   - the breaker settings (`BREAKER_*`)
   - `inflight_target_per_task`
4. **Write a short RCA:** timeline, cause, what the alarms showed and when, calls
   dead-lettered and replayed, and what changes.

## Procedures

**Deploy or update** (builds images tagged with the git SHA, then applies):

```bash
make aws-deploy        # = scripts/deploy.sh; asks before each terraform apply
make aws-smoke         # access control, health, one call recorded and looked up
```

A bad image never replaces the running one. The ECS deployment circuit breaker rolls back
automatically.

**Roll back to a previous image** (tags are immutable git SHAs, so a rollback is a redeploy
of an older tag):

```bash
aws ecr describe-images --repository-name steadfast-orchestrator \
  --query 'sort_by(imageDetails,&imagePushedAt)[*].imageTags[0]' --output text
IMAGE_TAG=<previous tag> scripts/deploy.sh   # reuses the image already in ECR
```

**Model tier on and off.** It costs nothing while off: the ASG goes to 0 instances.

```bash
scripts/model_on.sh cpu    # or gpu (needs G-instance quota); builds/uploads only if changed
scripts/model_off.sh       # orchestrator back to the mock, instance terminated
```

**Replay the DLQ:** `make aws-replay` (see Post-incident).

**Outage drill** (the dependency disappears entirely, then recovers):

```bash
aws ecs update-service --cluster steadfast --service steadfast-mock --desired-count 0
# ... traffic is dead-lettered with reason circuit_open / retries_exhausted ...
aws ecs update-service --cluster steadfast --service steadfast-mock --desired-count 1
make aws-replay
```

**Export the evidence before teardown:** `make aws-results LABEL=<name>` writes
`results/aws-<name>-*.json`.

**Teardown (end of every session):**

```bash
scripts/teardown.sh   # model off, terraform destroy, then an independent check
```

It writes `results/teardown.log` and `results/teardown-verify.txt`, with the account ID
redacted. The check asks AWS directly for anything still tagged `Project=steadfast`, and
for instances, NAT gateways, EIPs, volumes, VPCs, load balancers and clusters. It must say
`RESULT: PASS`.

**Demo time compression.** Load tests replay the 24-hour profile faster than real time:
- `SPEED=6` makes 1 simulated hour 10 real minutes; the 07:45–10:15 window takes 25
  minutes.
- The model test ran at `SPEED=1` (real time), because one model instance serves about 13
  calls per minute.
