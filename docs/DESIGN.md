# Design

The one invariant: **zero unaccounted calls**. Every call event ends either in the call log
with its outcome, or in the dead-letter queue (DLQ) with a reason, never nowhere. Every
choice below either protects that invariant or keeps it cheap.

## The call path (both rounds)

- **Persist before acknowledging.**
  - `POST /calls` answers 200 only after the result is written. Locally that means SQLite
    with `synchronous=FULL`, i.e. fsynced; on AWS, DynamoDB.
  - A failed call is also a 200 ("the dependency failed, and here is the record").
  - Only a call that could not be recorded gets a 500, and the client knows to resend.
- **DLQ entry before call-log row on failure.**
  - If the second write fails, the event is still replayable.
  - The reverse order could leave a failed call nobody can replay.
- **Idempotent by `event_id`.**
  - A repeated event returns the stored result without calling the pipeline.
  - Concurrent duplicates share one in-flight task.
  - Replay upserts the same row.
- **One executor for live and replayed calls**, so replay gets the same retries, breaker
  and persistence guarantees.
- **Retry:** 3 attempts, full jitter, a 2.5 s per-attempt timeout and an 8 s call deadline.
  The circuit breaker opens when 50% of a full 100-attempt window fails.

  Why these numbers is in the README's [design decisions](../README.md#design-decisions)
  and in the docstrings of `service/resilience/`.

## Round 2: AWS choices

**Fargate has no GPUs, so the system is split in two tiers.**
- The orchestrator is a small Fargate task that scales with call volume.
- The model tier (`infra/model.tf`) runs on ECS on EC2: an Auto Scaling group with
  min 0 / max 1 behind a capacity provider. Its instance exists only while
  `scripts/model_on.sh` is in effect.
- The tiers talk over ECS Service Connect (`http://model:8080`, `http://mock:8081`), using
  the same HTTP contract as the mock. Swapping is configuration only
  ([MODEL_SWAP.md](MODEL_SWAP.md)).
- They scale for different reasons: calls in flight versus GPU throughput.

**Scale on in-flight calls per task, not CPU.**
- A collections call is a long-held request that mostly waits on the pipeline. CPU stays
  low while a task juggles dozens of calls.
- By Little's Law, in-flight already folds in both arrival rate and call duration.
- Target tracking uses the `Average` of `InFlightCalls`. Every task reports it every
  10 s, so the average equals total in flight ÷ tasks, which falls as tasks are added, as
  target tracking needs.
- Scale-out cooldown is 60 s; scale-in is 300 s, because each removed task costs a 60 s
  connection drain.
- Measured: 2 → 8 → 2 tasks through a full campaign window, with 0 unaccounted calls
  (README results).

**Per-task circuit breakers, not a shared one.**
- A shared breaker (state in DynamoDB or Redis) would add a network hop and a new failure
  mode to every attempt, just to save a few seconds of detection.
- Per-task breakers need no coordination, and they isolate a task whose own network path
  is bad.
- The cost: each task must see its own 50 failed attempts before opening, so detection
  slows as tasks are added.
  - The window counts attempts, not calls, which detects about 3× faster.
  - In the AWS outage drill, 243 of 312 calls were already short-circuited (`circuit_open`)
    rather than retried.
- At production scale, a shared hint (for example a CloudWatch alarm on fleet-wide
  `DownstreamErrors` that every task polls) could open all breakers together.

**SQS standard, not FIFO.**
- Replay is idempotent by `event_id`, so an occasional duplicate delivery is harmless.
- FIFO's throughput limits and 5-minute deduplication window add nothing.
- The queue keeps messages 14 days (the maximum), and a failed replay reappears after
  5 minutes.
- SQS cannot list or look up messages, so a DynamoDB table (`steadfast-dead-letters`)
  indexes every entry by `event_id`. The table is the source of truth for status, and the
  message is a pointer saying "this needs a replay".

**DynamoDB for the call log, not SQLite per task.**
- Behind an ALB, a per-task file would put any lookup on a random task, and rows would
  vanish when tasks stop.
- The table is on-demand (nothing to size, nothing paid when idle), with point-in-time
  recovery because the call log is a compliance record.

**One NAT gateway, not one per AZ.**
- It saves about $40/mo. The trade-off: an AZ-a outage also cuts private egress in AZ-b.
- Gateway endpoints take DynamoDB and S3 (the hot path and image layers) off the NAT
  entirely.
- Production would run one NAT per AZ.

**IAM, with no wildcard actions anywhere.**
- The execution role (image pull and logs, used only by the ECS agent) is separate from
  the task roles (what our code may do):
  - the orchestrator gets item-level DynamoDB access on two tables and one SQS queue;
  - the mock has no task role at all;
  - the model role can read only the weights prefix in S3.
- Two actions accept only `Resource "*"` and have no condition keys. Each is commented
  where it is granted:
  - `ecr:GetAuthorizationToken`, which returns a login token only; what can be pulled or
    pushed is scoped separately;
  - `ecs:DiscoverPollEndpoint`, which returns the ECS agent's endpoint URL.
- `ecs:Poll`, which Service Connect on EC2 needs, is scoped to the cluster's task sets.
- `cloudwatch:PutMetricData` is avoided completely: metrics leave as EMF log lines.
- The model host's instance role is written out explicitly instead of the AWS-managed
  ECS policy, which grants `ecs:Submit*`.
- The ECS task roles' trust policy carries an `aws:SourceAccount` condition
  (confused-deputy guard).

**Access without a domain.**
- The ALB serves HTTPS with a self-signed certificate that clients pin (`--cacert`).
- Every request must carry an `x-steadfast-key` header, checked by an ALB listener rule
  before any task sees the request. The key exists only in the gitignored Terraform state.
- An IP allowlist was tried first, but it locked out operators behind iCloud Private Relay
  and corporate proxies, whose plain-HTTP traffic leaves from rotating addresses.

**Images are built where the bandwidth is.**
- The ~1 GB model image is built by a one-shot EC2 instance in the VPC
  (`infra/build.tf`). The instance pushes to ECR, uploads its log to S3 and terminates.
- Local downloads ran at about 30 kB/s, and the account's CodeBuild quota is 0.
- Every image tag is immutable: a git SHA for the orchestrator and mock, a content hash for
  the model.

**Teardown is verified, not assumed.**
- `scripts/teardown_verify.py` treats the tagging API as an index only, because it lists
  deleted resources for a while.
- It confirms each listed ARN with its own service, then also lists instances, NAT
  gateways, EIPs, volumes, VPCs, load balancers and clusters directly.

## ap-south-1 notes

Regulatory statements below are paraphrased. **Verify the current wording** with
compliance before relying on any of them.

- **Data localisation.**
  - RBI's digital lending rules (Guidelines of September 2022, since consolidated into the
    Digital Lending Directions) require data collected by lending apps and their service
    providers to be stored on servers in India. *(verify current wording)*
  - The DPDP Act 2023 and its Rules add consent, purpose limitation and breach
    notification. Sectoral rules like RBI's are stricter on location. *(verify current
    wording)*
  - Everything holding borrower data here (DynamoDB, SQS, S3, CloudWatch Logs) is in
    ap-south-1 (Mumbai).
  - The only resource outside ap-south-1 is the cost-allocation tag activation (Cost
    Explorer's API exists only in us-east-1), and the AWS Budget is a global service.
    Neither holds borrower data.
  - The optional CloudFront front door terminates TLS at edge locations that may be outside
    India. It is off by default, and production should keep it off or restrict it.
- **Disaster recovery stays in India.**
  - ap-south-2 (Hyderabad) is the in-country DR region: a DynamoDB global-table replica or
    cross-region backups for the call log and DLQ index, and S3 replication for weights.
  - SQS is not replicated. The DLQ index in DynamoDB is enough to rebuild the queue.
- **No PII in logs.**
  - Log lines and metrics carry only `event_id`, outcome, retry count, latency and breaker
    state.
  - The full event payload (campaign, DPD bucket, product) is kept only in the DLQ message
    and its DynamoDB index, both encrypted at rest.
  - Production should use customer-managed KMS keys, and `event_id`s that are opaque, not
    loan numbers.
- **Calling hours (RBI Compliance).**
  - RBI's conduct rules for recovery agents restrict calls to borrowers to roughly
    08:00–19:00 IST.
  - The synthetic profile's 24/7 baseline does not reflect that. Real outbound load would
    be concentrated in about 11 hours, so the model tier could be off overnight: about
    $400/mo for a g5.xlarge instead of $870.
  - **A DLQ replay strictly respects the window.** Replaying a backlog at 21:00 would
    re-dial borrowers outside permitted hours. Steadfast implements `REPLAY_CALLING_WINDOW`
    (default `08:00-19:00`, timezone `Asia/Kolkata`), deferring replays with `replay_deferred`
    logs when outside legal recovery windows.

**Native ECS Blue/Green Deployments (AWS Provider 6.x).**
- Upgraded to Terraform AWS Provider 6.x to leverage native ECS `deployment_configuration`
  with strategy `BLUE_GREEN`.
- Configured dual target groups (`blue` and `green`) behind the ALB, with an advanced
  routing rule matching `x-steadfast-green: 1` to enable synthetic pre-shift verification.
- Enforces an automated 2-minute bake period before 100% production traffic shift, backed
  by CloudWatch alarm rollback (`steadfast-5xx-rate` and `steadfast-latency-p99`).
- ECS ALB management uses a scoped, least-privilege IAM role without wildcard permissions.

**Multi-AZ Resiliency & Zone Rebalancing.**
- `availability_zone_rebalancing = "ENABLED"` guarantees orchestrator tasks remain balanced
  across subnets in AZ-a and AZ-b.
- Validated via `scripts/az_drill.sh`, which halts all tasks in an AZ under live load;
  remaining tasks absorb traffic with zero client 5xx errors and zero unaccounted calls.
- **Latency.** Running in Mumbai keeps the voice loop close to Indian telephony and
  borrowers. Every turn crosses the network several times, so region choice is a latency
  decision as well as a compliance one.
- **Indic language models.** These are candidates; check each licence before an
  Apache-2.0 release.

  | Stage | Candidates | Licence |
  |---|---|---|
  | STT | Whisper large-v3 / large-v3-turbo via faster-whisper (multilingual, includes Hindi) | faster-whisper is MIT; weights to verify |
  | STT | AI4Bharat IndicConformer / IndicWhisper | to verify |
  | LLM | Qwen2.5 (multilingual) | Apache-2.0 |
  | LLM | Sarvam's open Indic models | to verify |
  | LLM | Llama-3.1-8B | Llama community licence, not Apache-2.0 |
  | TTS | AI4Bharat Indic Parler-TTS | to verify |

  Piper's Hindi voices are CC BY-NC-SA (non-commercial), so they are excluded here.
