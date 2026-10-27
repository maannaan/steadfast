# Submission checklist

Predixion AI x TalentX Open-Weight Collections Agent Challenge, AWS FDE track.

## Round 1

- [ ] Repository: https://github.com/maannaan/steadfast (Apache-2.0, `LICENSE`)
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
      in `scripts/`.
- [x] Runbook: [docs/RUNBOOK.md](docs/RUNBOOK.md)
- [ ] Demo video (5 min: live load, dashboard, scaling, DLQ replay): [TODO: link]
- [ ] Cost write-up revised with measured numbers: fill in "Round 2 measured" in
      [docs/COST.md](docs/COST.md) from the receipts. [TODO]
- [ ] AWS billing receipts for reimbursement (cap ₹2,500): [TODO: attach PDFs; not
      committed]
- [x] Teardown proof: `results/teardown.log` and `results/teardown-verify.txt`
      (`RESULT: PASS`). Re-run `scripts/teardown.sh` after the demo recording and commit
      the new files.
- [x] ap-south-1 notes (data residency, latency, calling hours):
      [docs/DESIGN.md](docs/DESIGN.md#ap-south-1-notes)
- [ ] One-pager: [TODO: link or file]

## Before sending

- [ ] `make lint && make test` pass, and CI is green on `main`.
- [ ] `git grep` finds no account ID, email address or key.
- [ ] After the final teardown, `scripts/teardown.sh` reports PASS, and the Billing console
      shows no running Steadfast resources.
