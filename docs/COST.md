# Cost

## Round 1 estimate: Fargate, two scaling configurations

**Prices.** These are the ap-south-1 on-demand rates from the AWS Price List API, checked on
2026-10-26:

| Item | Price |
|---|---|
| Fargate (Linux/x86) vCPU | $0.04256 per vCPU-hour |
| Fargate memory | $0.004655 per GB-hour |
| ALB | $0.0239 per hour, plus LCUs at $0.008 each |
| NAT gateway | $0.056 per hour, plus $0.056 per GB processed |
| Public IPv4 address | $0.005 per hour |

The Fargate rates are about 8% below Appendix D's approximations (~$0.046 and ~$0.0051).
A month is 30 days, as in Appendix D.

**Profile (Appendix C).** The baseline is 10 calls/min for 20 h/day. Two campaign windows
add 4 h/day at a ramp to 50 calls/min and a hold there. That comes to about 23,400 calls a
day.

| | Config A | Config B |
|---|---|---|
| Task size | 1 vCPU / 2 GB ($0.0519/h) | 0.5 vCPU / 1 GB ($0.0259/h) |
| Scaling | 1 → 5 tasks (average 3 at peak) | 1 → 10 tasks (average 6 at peak) |
| Compute, minimum 1 task | 32 task-h/day = **$49.8/mo** | 44 task-h/day = **$34.2/mo** |
| Compute, minimum 2 tasks (HA) | 52 task-h/day = **$80.9/mo** | 64 task-h/day = **$49.8/mo** |

At Appendix D's prices the same arithmetic gives A $54 and B $38, or $88 and $55 with a
2-task minimum, which matches the reference.

**Fixed overhead does not scale with traffic.**
- ALB: about $17.2/mo, plus roughly $1–6/mo of LCUs at this volume.
- NAT gateway: $40.3/mo, plus data. Data is small here because DynamoDB and S3 traffic
  goes through free gateway endpoints.
- Public IPv4: 3 addresses (2 for the ALB, 1 for the NAT) at $10.8/mo. Appendix D omits
  this.
- Total: about **$70/mo**, which is more than either configuration's compute.

DynamoDB, SQS and CloudWatch are usage-priced and are not estimated here. The Round 2
receipts below show what they actually cost.

**Recommendation: Config B (0.5 vCPU / 1 GB), minimum 2, maximum 10, scaling on calls in
flight per task.** That is about $50 compute + $70 overhead ≈ **$120/mo**, against about
$151 for A at the same 2-task minimum. The reasons:

1. **Peak compute costs the same for both.** 3 big tasks and 6 small ones are the same
   vCPU-hours ($0.156/h). The configurations differ only off-peak.
2. **Small tasks halve the floor, and the floor is 20 of every 24 hours.** They also scale
   in finer steps: in the Round 2 autoscaling run (Config B task size), the autoscaler went
   2 → 5 → 6 → 8 tasks and back down one task at a time. With A, every step would cost
   2 vCPU.
3. **Two tasks is an availability floor, not a load floor.** One task per AZ survives an
   AZ loss. With A that floor costs 2 vCPU around the clock; with B it costs 1.
4. **Scale on in-flight calls, not CPU.** By Little's Law (in flight = arrival rate ×
   duration), the mock at peak holds only 0.83 calls/s × ~0.37 s ≈ **0.3 calls in
   flight**. Real ~75 s collections calls hold 0.83 × 75 ≈ **62**. Those calls mostly
   wait on the model, so CPU stays low while the in-flight count grows. At our target of
   20 per task, the real peak needs about 4 B-tasks.

   Little's Law held on AWS. In the autoscaling run, 6-turn calls of ~27.5 s arrived at
   5/s (the profile replayed at 6× speed), which predicts ~137 in flight; the dashboard
   peaked at 152 (`results/aws-aws-window-20261026T165755Z.json`).

**What actually dominates the bill is the model, not Fargate.** One always-on g5.xlarge
($1.208/h) costs **$870/mo**, about 7× the whole Fargate, ALB and NAT stack. The
synthetic baseline runs 24/7, so in this profile it can never be switched off. Real
collections calls are limited to about 08:00–19:00 ([DESIGN.md](DESIGN.md)), which would
allow ~$400/mo. Either way, the real lever is right-sizing the GPU tier: calls per GPU, and
whether STT, LLM and TTS share one instance. The CPU tier
we measured (c7i.xlarge, $0.1785/h ≈ $129/mo) serves about 13 single-turn calls/min. The
50/min peak would need 4 of them, so CPU only fits as a fallback.

**Fargate Spot fits the replay worker only.** Replay is idempotent by `event_id`, and an
interrupted message reappears in SQS after 5 minutes, so an interruption costs nothing.
Live calls should not sit on capacity that can be reclaimed mid-campaign. [verify the
current Spot discount for ap-south-1 before relying on it]

## Round 2 measured

The AWS account ran the stack in short sessions on 2026-10-26 and destroyed it at the end
(`results/teardown-verify.txt`: PASS). Billing data lags by up to 24 hours, so these
numbers come from the receipts once they are final.

| Line item | Billed (USD) | Note |
|---|---|---|
| Fargate (orchestrator, mock, replay worker) | [TODO: value] | |
| EC2 (model host c7i.xlarge, image builder c7i.large) | [TODO: value] | |
| EBS (model host, builder) | [TODO: value] | |
| NAT gateway (hours + GB) | [TODO: value] | |
| ALB (hours + LCU) | [TODO: value] | |
| Public IPv4 | [TODO: value] | |
| DynamoDB, SQS, S3 | [TODO: value] | |
| CloudWatch (logs, custom metrics, alarms, dashboard) | [TODO: value] | |
| ECR storage, data transfer, other | [TODO: value] | |
| **Total** | **[TODO: value]** | Appendix H cap ₹2,500 (~$30) |

Measured cost per call: [TODO: total for the load-test hours ÷ calls sent; the
autoscaling run sent 6,083 calls in 25 minutes].

What changed from the estimate: [TODO: compare after the receipts arrive, especially
CloudWatch, where per-task metric dimensions create new metrics for every task the
autoscaler starts].
