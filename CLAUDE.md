# Project rules for Steadfast

You are helping me build "Steadfast", my submission for the Predixion AI x TalentX
Open-Weight Collections Agent Challenge (AWS Forward Deployment Engineer track).
Predixion runs AI outbound collections calls for Indian digital lenders. In lending,
a call that drops without a record is a compliance gap. Steadfast's design goal is:
ZERO UNACCOUNTED CALLS — every call event ends either in the call log as a result,
or in the dead-letter queue (DLQ), never nowhere.

Non-negotiable rules for this repo:

1. No secrets, credentials, AWS account IDs, or personal data anywhere in the repo.
   All config comes from environment variables (documented in .env.example) or
   Terraform variables. .env, *.tfstate*, .terraform/ are gitignored.
2. Everything must run from the README alone. No undocumented manual steps.
3. Everything will be published under Apache-2.0. Only use dependencies and models
   whose licences allow that. Add a LICENSE file (Apache-2.0).
4. Python 3.12, FastAPI, httpx (async), pydantic v2 + pydantic-settings, SQLite,
   pytest + pytest-asyncio, ruff. Type hints everywhere. Small, readable modules.
5. IAM in Terraform: no wildcard ACTIONS (no ecs:*, iam:*, s3:*, sqs:*). Scope
   resources to exact ARNs. Where AWS requires Resource "*" (cloudwatch:PutMetricData,
   ecr:GetAuthorizationToken), add a condition if one exists and a code comment
   explaining why.
6. Prefer clarity over cleverness. I must be able to explain every design choice in
   an interview, so add short docstrings that explain WHY, not just what.
7. Never claim something works without a test or a run that proves it.
8. Work in the phases I give you. At the end of each phase: list files changed,
   commands to run, and what I should verify. Do not jump ahead.
