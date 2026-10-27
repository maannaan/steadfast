# Submission checklist

Predixion AI x TalentX Open-Weight Collections Agent Challenge, AWS FDE track.

## Round 1

- [x] Repository: https://github.com/maannaan/steadfast (Apache-2.0, `LICENSE`)
- [x] Code and README: [README.md](README.md) (quick start, failure demos, design
      decisions, results)
- [x] Half-page cost write-up, two Fargate configurations and a recommendation:
      [docs/COST.md](docs/COST.md)
- [x] One paragraph on swapping in a real open-weight model:
      [docs/MODEL_SWAP.md](docs/MODEL_SWAP.md)
- [x] Hard gates:
  - no secrets in the repo (CI runs gitleaks over the full history);
  - runs from the README alone (`make up`, `make test`, `make load-window`).

## Round 2

- [x] Infrastructure as code: `infra/` (Terraform). Deployment and teardown are scripted
      in `scripts/`. Native ECS Blue/Green deployment (`infra/orchestrator.tf`) on AWS
      Provider 6.x with automated 2-minute bake and health-check rollback.
- [x] Runbook: [docs/RUNBOOK.md](docs/RUNBOOK.md) (procedures, dashboards, alarms, blue/green
      deployments, multi-AZ failure drill, escalation matrix).
- [x] Demo video (5 min: live load, dashboard, scaling, DLQ replay):
      File generated: `demo/out/steadfast-demo.mp4` (Duration: <= 5:00, 1080p, open-weight
      Piper TTS narration, burned-in captions, Playwright terminal replays & CloudWatch
      console capture).
      Video link: https://youtu.be/T3inVZCFdRs
- [x] Cost write-up revised with measured numbers: "Round 2 measured" in
      [docs/COST.md](docs/COST.md) populated with measured receipts from Cost Explorer
      (`results/costs-demo-2026-10-25.json`: **$2.70 total spent**, < 10% of budget cap).
- [x] AWS billing receipts for reimbursement (cap ₹2,500):
      Cost Explorer daily itemized JSON receipts committed under
      `results/costs-demo-2026-10-25.json` ($2.70 total). Full AWS invoice PDF downloadable
      from AWS Billing console upon early November invoice closure.
- [x] Teardown proof: `results/teardown.log` and `results/teardown-verify.txt`
      (**`RESULT: PASS`**). Programmatically verified by querying AWS APIs directly for all
      tagged resources, instances, NAT gateways, EIPs, volumes, VPCs, and clusters.
- [x] ap-south-1 notes (data residency in Mumbai, voice latency, RBI recovery calling hours
      gated to 08:00–19:00 IST): [docs/DESIGN.md](docs/DESIGN.md#ap-south-1-notes).
- [x] One-pager: [docs/ONE_PAGER.md](docs/ONE_PAGER.md) and compiled PDF
      [docs/ONE_PAGER.pdf](docs/ONE_PAGER.pdf).

## Before sending

- [x] `make lint && make test` pass.
- [x] `git grep` finds no account ID, email address or key.
- [x] After the final teardown, `scripts/teardown.sh` reports PASS, and zero billable
      Steadfast resources remain in ap-south-1.
