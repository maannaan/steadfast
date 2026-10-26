# Steadfast - local stack (Round 1) and AWS deployment (Round 2). Run `make help` for targets.

SPEED ?= 60
START ?= 00:00
HOURS ?= 24
LOAD_SEED ?= 42
RATE ?= 20
LABEL ?= load

COMPOSE := docker compose
LOADGEN := $(COMPOSE) --profile loadgen run --rm loadgen
CLI := $(COMPOSE) exec -T service python -m service.cli
VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help venv up down logs test lint load load-window burst mock-normal kill-mock \
	start-mock outage dlq-list dlq-replay dlq-stats report check-latency clean \
	aws-build aws-deploy aws-smoke aws-load aws-load-window aws-replay aws-results aws-destroy

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

venv: ## Create .venv with dev dependencies (needed for test/lint)
	python3.12 -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements-dev.txt

up: ## Build and start mock + service, wait until healthy
	mkdir -p data results
	$(COMPOSE) up -d --build --wait mock service

down: ## Stop the stack (keeps data/)
	$(COMPOSE) down

logs: ## Follow service and mock logs
	$(COMPOSE) logs -f service mock

test: ## Run the test suite
	$(PY) -m pytest -q

lint: ## Ruff lint + format check
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

load: ## Replay the traffic profile (SPEED, START, HOURS, LOAD_SEED, LABEL), then report
	$(LOADGEN) python -m loadgen.generate --speed $(SPEED) --start $(START) \
		--duration-hours $(HOURS) --seed $(LOAD_SEED)
	$(MAKE) --no-print-directory report LABEL=$(LABEL)

load-window: ## One campaign window (07:45-10:15) at speed 60 = 2.5 real minutes
	$(MAKE) --no-print-directory load SPEED=60 START=07:45 HOURS=2.5 LABEL=independent

burst: ## Mock in burst mode (15s total outage every 60s) during a campaign window
	FAILURE_MODE=burst $(COMPOSE) up -d --wait --no-deps --force-recreate mock
	FAILURE_MODE=burst $(MAKE) --no-print-directory load SPEED=60 START=07:45 HOURS=2.5 LABEL=burst; \
		status=$$?; $(MAKE) --no-print-directory mock-normal; exit $$status

mock-normal: ## Restart the mock in its normal (independent 20%) mode
	$(COMPOSE) up -d --wait --no-deps --force-recreate mock

kill-mock: ## Stop the mock (simulates a full dependency outage)
	$(COMPOSE) stop mock

start-mock: ## Start the mock again
	$(COMPOSE) start mock

outage: ## Kill the mock mid-run, restore it, replay the DLQ
	./scripts/mock_outage.sh

dlq-list: ## List pending DLQ entries
	$(CLI) dlq list --status pending

dlq-replay: ## Replay every DLQ entry not yet replayed (RATE per second)
	$(CLI) dlq replay --all --rate $(RATE)

dlq-stats: ## Call and DLQ totals
	$(CLI) dlq stats

report: ## Summarise the latest run (exit 1 if any call is unaccounted)
	$(LOADGEN) python -m loadgen.report --label $(LABEL)

check-latency: ## Lognormal percentiles for sigma 0.35 / 0.60 / 0.66
	python3 scripts/check_latency.py

# ---------- AWS (Round 2). Needs AWS credentials (e.g. AWS_PROFILE) and terraform. ----------

AWS_ENV = eval "$$(./scripts/aws_env.sh)" &&
AWS_SPEED ?= 6
AWS_START ?= 07:45
AWS_HOURS ?= 0.5
AWS_CONCURRENCY ?= 300

aws-build: ## Build and push orchestrator + mock images to ECR (tag = git SHA)
	./scripts/build_push.sh

aws-deploy: ## Apply Terraform and deploy (asks before each apply)
	./scripts/deploy.sh

aws-smoke: ## Health, readiness and one recorded call through the ALB
	./scripts/aws_smoke.sh

aws-load: ## Loadgen from this machine via the public URL (AWS_SPEED/START/HOURS), then report
	$(AWS_ENV) $(PY) -m loadgen.generate --url "$$STEADFAST_URL" --speed $(AWS_SPEED) \
		--start $(AWS_START) --duration-hours $(AWS_HOURS) --seed $(LOAD_SEED) \
		--concurrency $(AWS_CONCURRENCY) --no-prom-metrics
	$(AWS_ENV) $(PY) -m loadgen.report --url "$$STEADFAST_URL" --label $(LABEL) \
		--no-prom-metrics

aws-load-window: ## Autoscaling demo: 07:45-10:15 at speed 6 (25 min), report, wait for scale-in, export
	./scripts/load_aws.sh

aws-replay: ## Drain the SQS DLQ: run the replay worker until the queue is empty
	./scripts/replay_aws.sh

aws-results: ## Export the last MINUTES (default 60) of CloudWatch metrics to results/
	./scripts/aws_results.sh $(LABEL)

aws-destroy: ## Destroy all AWS resources (end of every session)
	./scripts/aws_destroy.sh

clean: ## Stop everything and delete local data and raw run files
	$(COMPOSE) --profile loadgen down --remove-orphans
	rm -rf data results/run-*.jsonl results/run-*.meta.json
